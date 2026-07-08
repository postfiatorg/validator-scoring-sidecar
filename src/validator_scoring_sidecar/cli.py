"""Command line interface for validator scoring sidecar operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from validator_scoring_sidecar.chain import (
    ChainWatcherError,
    FoundationConfig,
    XrplPftlRpcClient,
    resolve_foundation_publisher_address,
)
from validator_scoring_sidecar.commit import ValidatorKeysSigner
from validator_scoring_sidecar.config import ConfigError, load_config
from validator_scoring_sidecar.deployment import (
    DEFAULT_LOCAL_PORT,
    DeploymentError,
    DeploymentRecord,
    deploy_modal_endpoint,
    deployment_record_path,
    load_round_manifest,
    read_manifest_file,
    select_latest_deployable_round,
    start_local_sglang_endpoint,
)
from validator_scoring_sidecar.local_runtime import RealLocalSglangStarter, detect_gpu
from validator_scoring_sidecar.modal_deployer import RealModalDeployer
from validator_scoring_sidecar.input_package import (
    PACKAGE_SOURCE_CHOICES,
    FetchedInputPackage,
    InputPackageCacheError,
    InputPackageDownloadError,
    InputPackageVerificationError,
    fetch_input_package as fetch_verified_input_package,
)
from validator_scoring_sidecar.round_metadata import (
    MissingFrozenInputMetadata,
    RoundMetadata,
    RoundMetadataError,
)
from validator_scoring_sidecar.scoring_client import (
    ScoringClient,
    ScoringClientError,
)
from validator_scoring_sidecar.state import SidecarStateError
from validator_scoring_sidecar.score import (
    SCORE_STATUS_ALREADY_SCORED,
    SCORE_STATUS_COMPARISON_PENDING,
    SCORE_STATUS_DIVERGENT,
    SCORE_STATUS_SCORED,
    SCORE_STATUS_SKIPPED,
    ScoreResult,
    score_round,
)
from validator_scoring_sidecar.preflight import (
    CHECK_REPRODUCTION,
    CheckResult,
    run_preflight,
)
from validator_scoring_sidecar.participate import (
    ParticipateResult,
    ParticipationConfigError,
    WarmRuntimeResult,
    participate,
    require_participation_config,
    warm_modal_runtime,
)
from validator_scoring_sidecar.sync import (
    DEFAULT_SYNC_ROUND_LIMIT,
    MAX_SYNC_ROUND_LIMIT,
    SyncLockError,
    SyncResult,
    SyncSetupError,
    sync_input_package,
)
from validator_scoring_sidecar.verification import VerificationError

EXIT_OK = 0
EXIT_OPERATOR_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_NETWORK_ERROR = 3
EXIT_LOCKED = 4


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.handler(args)
    except ConfigError as exc:
        _print_error(f"Configuration error: {exc}")
        return EXIT_USAGE_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validator-scoring-sidecar",
        description="Post Fiat Dynamic UNL validator scoring sidecar tooling.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch-input-package",
        help="Fetch, verify, and cache a frozen input package for a public round.",
    )
    fetch.add_argument(
        "--round-id",
        type=_positive_int,
        required=True,
        help="Scoring service database round ID to fetch.",
    )
    fetch.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source. Defaults to auto.",
    )
    _add_common_config_arguments(fetch)
    fetch.add_argument(
        "--force",
        action="store_true",
        help="Refetch and replace an existing verified package cache.",
    )
    _add_json_argument(fetch)
    fetch.set_defaults(handler=fetch_input_package)

    sync = subparsers.add_parser(
        "sync",
        help="Discover and verify the newest unhandled frozen input package.",
    )
    sync.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source. Defaults to auto.",
    )
    sync.add_argument(
        "--round-limit",
        type=_round_limit,
        default=DEFAULT_SYNC_ROUND_LIMIT,
        help=(
            "Number of recent rounds to inspect for eligible frozen input "
            f"metadata. Defaults to {DEFAULT_SYNC_ROUND_LIMIT}."
        ),
    )
    _add_common_config_arguments(sync)
    _add_json_argument(sync)
    sync.set_defaults(handler=sync_input_packages)

    deploy = subparsers.add_parser(
        "deploy-modal",
        help="Deploy a manifest-pinned Modal inference endpoint and record it.",
    )
    deploy_source = deploy.add_mutually_exclusive_group(required=False)
    deploy_source.add_argument(
        "--round-id",
        type=_positive_int,
        help=(
            "Scoring service round ID whose frozen manifest to deploy from. "
            "Defaults to the latest eligible round when omitted."
        ),
    )
    deploy_source.add_argument(
        "--manifest",
        help="Path to an execution_manifest.json to deploy from directly.",
    )
    deploy.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source for round fetches. Defaults to auto.",
    )
    deploy.add_argument(
        "--round-limit",
        type=_round_limit,
        default=DEFAULT_SYNC_ROUND_LIMIT,
        help=(
            "Recent rounds to scan when neither --round-id nor --manifest is "
            f"given. Defaults to {DEFAULT_SYNC_ROUND_LIMIT}."
        ),
    )
    deploy.add_argument(
        "--app-name",
        help="Modal app name to deploy under. Defaults to a per-network name.",
    )
    _add_common_config_arguments(deploy)
    _add_json_argument(deploy)
    deploy.set_defaults(handler=deploy_modal_command)

    start = subparsers.add_parser(
        "start-sglang",
        help="Start a manifest-pinned local SGLang endpoint and record it.",
    )
    start_source = start.add_mutually_exclusive_group(required=False)
    start_source.add_argument(
        "--round-id",
        type=_positive_int,
        help=(
            "Scoring service round ID whose frozen manifest to start from. "
            "Defaults to the latest eligible round when omitted."
        ),
    )
    start_source.add_argument(
        "--manifest",
        help="Path to an execution_manifest.json to start from directly.",
    )
    start.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source for round fetches. Defaults to auto.",
    )
    start.add_argument(
        "--round-limit",
        type=_round_limit,
        default=DEFAULT_SYNC_ROUND_LIMIT,
        help=(
            "Recent rounds to scan when neither --round-id nor --manifest is "
            f"given. Defaults to {DEFAULT_SYNC_ROUND_LIMIT}."
        ),
    )
    start.add_argument(
        "--port",
        type=_positive_int,
        default=DEFAULT_LOCAL_PORT,
        help=f"Local port to serve SGLang on. Defaults to {DEFAULT_LOCAL_PORT}.",
    )
    _add_common_config_arguments(start)
    _add_json_argument(start)
    start.set_defaults(handler=start_sglang_command)

    score = subparsers.add_parser(
        "score",
        help="Score a round end to end and record the verification outcome.",
    )
    score.add_argument(
        "--round-id",
        type=_positive_int,
        help="Round to score. Defaults to the latest eligible round when omitted.",
    )
    score.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source for round fetches. Defaults to auto.",
    )
    score.add_argument(
        "--round-limit",
        type=_round_limit,
        default=DEFAULT_SYNC_ROUND_LIMIT,
        help=(
            "Recent rounds to scan when no --round-id is given. Defaults to "
            f"{DEFAULT_SYNC_ROUND_LIMIT}."
        ),
    )
    _add_common_config_arguments(score)
    _add_json_argument(score)
    score.set_defaults(handler=score_command)

    warm = subparsers.add_parser(
        "warm-runtime",
        help=(
            "Provision the manifest-pinned Modal inference endpoint before the "
            "participation loop starts. No-op without Modal credentials."
        ),
    )
    warm.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source for round fetches. Defaults to auto.",
    )
    warm.add_argument(
        "--round-limit",
        type=_round_limit,
        default=DEFAULT_SYNC_ROUND_LIMIT,
        help=(
            "Recent rounds to scan for the latest eligible round. Defaults to "
            f"{DEFAULT_SYNC_ROUND_LIMIT}."
        ),
    )
    _add_common_config_arguments(warm)
    _add_json_argument(warm)
    warm.set_defaults(handler=warm_runtime_command)

    participate_parser = subparsers.add_parser(
        "participate",
        help=(
            "Run one unattended participation pass: score the latest round and "
            "advance its on-chain commit and reveal."
        ),
    )
    participate_parser.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source for round fetches. Defaults to auto.",
    )
    participate_parser.add_argument(
        "--round-limit",
        type=_round_limit,
        default=DEFAULT_SYNC_ROUND_LIMIT,
        help=(
            "Recent rounds to scan for the latest eligible round. Defaults to "
            f"{DEFAULT_SYNC_ROUND_LIMIT}."
        ),
    )
    _add_common_config_arguments(participate_parser)
    participate_parser.add_argument(
        "--pftl-rpc-url",
        help="PFTL JSON-RPC URL. Overrides POSTFIAT_SIDECAR_PFTL_RPC_URL.",
    )
    participate_parser.add_argument(
        "--foundation-publisher-address",
        help=(
            "Foundation publisher r-address. Overrides "
            "POSTFIAT_SIDECAR_FOUNDATION_PUBLISHER_ADDRESS and config discovery."
        ),
    )
    _add_json_argument(participate_parser)
    participate_parser.set_defaults(handler=participate_command)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help=(
            "Confirm a participation deployment is ready to commit and reveal and "
            "print a single READY / NOT READY verdict."
        ),
    )
    preflight_parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip the round reproduction (the GPU step) and run config checks only.",
    )
    preflight_parser.add_argument(
        "--source",
        choices=PACKAGE_SOURCE_CHOICES,
        default="auto",
        help="Package retrieval source for the reproduction fetch. Defaults to auto.",
    )
    preflight_parser.add_argument(
        "--round-limit",
        type=_round_limit,
        default=DEFAULT_SYNC_ROUND_LIMIT,
        help=(
            "Recent rounds to scan for the latest round to reproduce. Defaults to "
            f"{DEFAULT_SYNC_ROUND_LIMIT}."
        ),
    )
    _add_common_config_arguments(preflight_parser)
    preflight_parser.add_argument(
        "--pftl-rpc-url",
        help="PFTL JSON-RPC URL. Overrides POSTFIAT_SIDECAR_PFTL_RPC_URL.",
    )
    preflight_parser.add_argument(
        "--foundation-publisher-address",
        help=(
            "Foundation publisher r-address. Overrides "
            "POSTFIAT_SIDECAR_FOUNDATION_PUBLISHER_ADDRESS and config discovery."
        ),
    )
    _add_json_argument(preflight_parser)
    preflight_parser.set_defaults(handler=preflight_command)
    return parser


def _add_common_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        help="Scoring service base URL. Overrides POSTFIAT_SCORING_BASE_URL.",
    )
    parser.add_argument(
        "--data-dir",
        help="Local sidecar data directory. Overrides POSTFIAT_SIDECAR_DATA_DIR.",
    )
    parser.add_argument(
        "--ipfs-gateway-url",
        help=(
            "IPFS gateway URL prefix. Overrides "
            "POSTFIAT_SIDECAR_IPFS_GATEWAY_URL."
        ),
    )
    parser.add_argument(
        "--network",
        help="Network label. Overrides POSTFIAT_SIDECAR_NETWORK.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help=(
            "HTTP request timeout in seconds. Overrides "
            "POSTFIAT_SIDECAR_TIMEOUT_SECONDS."
        ),
    )


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )


def fetch_input_package(args: argparse.Namespace) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
        ipfs_gateway_url=args.ipfs_gateway_url,
        network=args.network,
        timeout_seconds=args.timeout,
    )
    client = ScoringClient(config)
    try:
        payload = client.fetch_round(args.round_id)
        metadata = RoundMetadata.from_api_payload(
            payload,
            requested_round_id=args.round_id,
        )
        fetched_package = fetch_verified_input_package(
            metadata,
            config,
            client,
            source=args.source,
            force=args.force,
        )
    except MissingFrozenInputMetadata as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except RoundMetadataError as exc:
        _print_error(f"Malformed round metadata: {exc}")
        return EXIT_NETWORK_ERROR
    except (InputPackageVerificationError, InputPackageCacheError) as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except (InputPackageDownloadError, ScoringClientError) as exc:
        _print_error(str(exc))
        return EXIT_NETWORK_ERROR
    finally:
        client.close()

    if args.json:
        print(json.dumps(fetched_package.as_dict(), indent=2, sort_keys=True))
    else:
        print(_format_fetched_input_package(fetched_package))

    return EXIT_OK


def sync_input_packages(args: argparse.Namespace) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
        ipfs_gateway_url=args.ipfs_gateway_url,
        network=args.network,
        timeout_seconds=args.timeout,
    )
    client = ScoringClient(config)
    try:
        result = sync_input_package(
            config,
            client,
            source=args.source,
            round_limit=args.round_limit,
        )
    except SyncLockError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "locked",
                        "network": config.network,
                        "lock_path": str(exc.lock_path),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            _print_error(str(exc))
        return EXIT_LOCKED
    except SyncSetupError as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except MissingFrozenInputMetadata as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except RoundMetadataError as exc:
        _print_error(f"Malformed round metadata: {exc}")
        return EXIT_NETWORK_ERROR
    except (
        InputPackageVerificationError,
        InputPackageCacheError,
        SidecarStateError,
    ) as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except (InputPackageDownloadError, ScoringClientError) as exc:
        _print_error(str(exc))
        return EXIT_NETWORK_ERROR
    finally:
        client.close()

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(_format_sync_result(result))

    return EXIT_OK


def deploy_modal_command(args: argparse.Namespace) -> int:
    return _run_runtime_command(
        args,
        lambda manifest, config: deploy_modal_endpoint(
            manifest,
            config,
            deployer=RealModalDeployer(),
            app_name=args.app_name or config.modal_app_name,
        ),
    )


def start_sglang_command(args: argparse.Namespace) -> int:
    return _run_runtime_command(
        args,
        lambda manifest, config: start_local_sglang_endpoint(
            manifest,
            config,
            starter=RealLocalSglangStarter(),
            gpu_detector=detect_gpu,
            port=args.port,
        ),
    )


def _run_runtime_command(
    args: argparse.Namespace,
    produce_record: Callable[..., DeploymentRecord],
) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
        ipfs_gateway_url=args.ipfs_gateway_url,
        network=args.network,
        timeout_seconds=args.timeout,
    )
    try:
        manifest = _load_deploy_manifest(args, config)
        record = produce_record(manifest, config)
    except MissingFrozenInputMetadata as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except RoundMetadataError as exc:
        _print_error(f"Malformed round metadata: {exc}")
        return EXIT_NETWORK_ERROR
    except (InputPackageVerificationError, InputPackageCacheError) as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except (InputPackageDownloadError, ScoringClientError) as exc:
        _print_error(str(exc))
        return EXIT_NETWORK_ERROR
    except DeploymentError as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR

    if args.json:
        print(json.dumps(record.as_dict(), indent=2, sort_keys=True))
    else:
        print(_format_deployment_record(record, config))

    return EXIT_OK


def score_command(args: argparse.Namespace) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
        ipfs_gateway_url=args.ipfs_gateway_url,
        network=args.network,
        timeout_seconds=args.timeout,
    )
    client = ScoringClient(config)
    try:
        result = score_round(
            config,
            client,
            round_id=args.round_id,
            source=args.source,
            round_limit=args.round_limit,
        )
    except SyncLockError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "locked",
                        "network": config.network,
                        "lock_path": str(exc.lock_path),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            _print_error(str(exc))
        return EXIT_LOCKED
    except SyncSetupError as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except MissingFrozenInputMetadata as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except RoundMetadataError as exc:
        _print_error(f"Malformed round metadata: {exc}")
        return EXIT_NETWORK_ERROR
    except (
        InputPackageVerificationError,
        InputPackageCacheError,
        SidecarStateError,
        VerificationError,
    ) as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except (InputPackageDownloadError, ScoringClientError) as exc:
        _print_error(str(exc))
        return EXIT_NETWORK_ERROR
    except DeploymentError as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    finally:
        client.close()

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(_format_score_result(result))

    return EXIT_OK


def _format_score_result(result: ScoreResult) -> str:
    lines = [
        f"Score status: {result.status}",
        f"Network: {result.network}",
        f"Round ID: {result.round_id}",
        f"Round number: {result.round_number}",
        f"Sidecar state: {result.sidecar_state}",
    ]
    if result.backend_mode is not None:
        lines.append(f"Backend mode: {result.backend_mode}")
    lines.append(f"Compared: {result.compared}")
    if result.compared:
        lines.append(f"Matched levels: {', '.join(result.matched_levels) or 'none'}")
    if result.error_category is not None:
        lines.append(f"Outcome category: {result.error_category}")
    return "\n".join(lines)


def warm_runtime_command(args: argparse.Namespace) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
        ipfs_gateway_url=args.ipfs_gateway_url,
        network=args.network,
        timeout_seconds=args.timeout,
    )
    client = ScoringClient(config)
    try:
        result = warm_modal_runtime(
            config,
            client,
            source=args.source,
            round_limit=args.round_limit,
        )
    except MissingFrozenInputMetadata as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except RoundMetadataError as exc:
        _print_error(f"Malformed round metadata: {exc}")
        return EXIT_NETWORK_ERROR
    except (InputPackageVerificationError, InputPackageCacheError) as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except DeploymentError as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except (InputPackageDownloadError, ScoringClientError) as exc:
        _print_error(str(exc))
        return EXIT_NETWORK_ERROR
    finally:
        client.close()

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(_format_warm_runtime_result(result))

    return EXIT_OK


def _format_warm_runtime_result(result: WarmRuntimeResult) -> str:
    lines = [f"Runtime warm-up: {result.status}"]
    if result.endpoint_url is not None:
        lines.append(f"Endpoint URL: {result.endpoint_url}")
    return "\n".join(lines)


def participate_command(args: argparse.Namespace) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
        ipfs_gateway_url=args.ipfs_gateway_url,
        network=args.network,
        timeout_seconds=args.timeout,
        pftl_rpc_url=args.pftl_rpc_url,
        foundation_publisher_address=args.foundation_publisher_address,
    )
    try:
        _wallet_seed, keys_path = require_participation_config(config)
    except ParticipationConfigError as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR

    client = ScoringClient(config)
    rpc_client = XrplPftlRpcClient(config.pftl_rpc_url)
    signer = ValidatorKeysSigner(validator_keys_path=keys_path)
    try:
        result = participate(
            config,
            client,
            rpc_client=rpc_client,
            signer=signer,
            source=args.source,
            round_limit=args.round_limit,
        )
    except SyncLockError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "locked",
                        "network": config.network,
                        "lock_path": str(exc.lock_path),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            _print_error(str(exc))
        return EXIT_LOCKED
    except (ParticipationConfigError, SyncSetupError) as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except MissingFrozenInputMetadata as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except RoundMetadataError as exc:
        _print_error(f"Malformed round metadata: {exc}")
        return EXIT_NETWORK_ERROR
    except (
        InputPackageVerificationError,
        InputPackageCacheError,
        SidecarStateError,
        VerificationError,
    ) as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except DeploymentError as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except (InputPackageDownloadError, ScoringClientError, ChainWatcherError) as exc:
        _print_error(str(exc))
        return EXIT_NETWORK_ERROR
    finally:
        client.close()

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(_format_participate_result(result))

    return EXIT_OK


def preflight_command(args: argparse.Namespace) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
        ipfs_gateway_url=args.ipfs_gateway_url,
        network=args.network,
        timeout_seconds=args.timeout,
        pftl_rpc_url=args.pftl_rpc_url,
        foundation_publisher_address=args.foundation_publisher_address,
    )
    client = ScoringClient(config)
    rpc_client = XrplPftlRpcClient(config.pftl_rpc_url)

    def resolve_publisher() -> str:
        foundation_config = None
        if not config.foundation_publisher_address:
            foundation_config = FoundationConfig.from_api_payload(client.fetch_config())
        return resolve_foundation_publisher_address(config, foundation_config)

    run_reproduction: Callable[[], CheckResult] | None = None
    if not args.quick:

        def run_reproduction() -> CheckResult:
            try:
                result = score_round(
                    config,
                    client,
                    round_id=None,
                    source=args.source,
                    round_limit=args.round_limit,
                )
            except Exception as exc:  # noqa: BLE001 - any failure is a failed check
                return CheckResult(
                    CHECK_REPRODUCTION, False, f"reproduction failed: {exc}"
                )
            return _reproduction_result_to_check(result)

    try:
        report = run_preflight(
            config,
            rpc_client=rpc_client,
            resolve_publisher=resolve_publisher,
            run_reproduction=run_reproduction,
        )
    finally:
        client.close()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(report.render())
    return EXIT_OK if report.ready else EXIT_OPERATOR_ERROR


def _reproduction_result_to_check(result: ScoreResult) -> CheckResult:
    """Map a reproduction ``ScoreResult`` to a readiness check.

    Reproduction proves the operator's own runtime can score a round. A genuine
    divergence or a scoring failure is the only real problem; a not-yet-published
    foundation comparison (the round is still in its commit/reveal window) and a
    non-scorable latest round (an override/skipped round) are expected states
    that must not read as NOT READY. Note that an ``already_scored`` round is
    served from cached state and does not re-exercise the backend this run.
    """
    round_number = result.round_number
    if result.status == SCORE_STATUS_DIVERGENT:
        return CheckResult(
            CHECK_REPRODUCTION,
            False,
            f"round {round_number} reproduced but diverged from the foundation",
        )
    if (
        result.status in (SCORE_STATUS_SCORED, SCORE_STATUS_ALREADY_SCORED)
        and result.matched_levels
    ):
        return CheckResult(
            CHECK_REPRODUCTION,
            True,
            f"round {round_number} reproduced; matched "
            f"{', '.join(result.matched_levels)}",
        )
    if result.status == SCORE_STATUS_COMPARISON_PENDING:
        return CheckResult(
            CHECK_REPRODUCTION,
            True,
            f"round {round_number} reproduced on your runtime; foundation "
            "comparison not yet available (round still in progress)",
        )
    if result.status == SCORE_STATUS_SKIPPED:
        return CheckResult(
            CHECK_REPRODUCTION,
            True,
            f"round {round_number} was not scorable (override/skipped); inference "
            "will be exercised on the next normal round",
        )
    return CheckResult(
        CHECK_REPRODUCTION,
        False,
        f"round {round_number} reproduction did not succeed (status {result.status})",
    )


def _format_participate_result(result: ParticipateResult) -> str:
    lines = [
        f"Participate status: score={result.score_status}",
        f"Network: {result.network}",
        f"Round ID: {result.round_id}",
        f"Round number: {result.round_number}",
        f"Announcements: {_format_advance(result.announcements)}",
        f"Commits: {_format_advance(result.commits)}",
        f"Reveals: {_format_advance(result.reveals)}",
    ]
    if result.score_error:
        lines.insert(1, f"Scoring unavailable: {result.score_error}")
    return "\n".join(lines)


def _format_advance(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "none"
    return ", ".join(
        f"round {entry.get('round_number', '?')}: {entry['status']}"
        for entry in entries
    )


def _load_deploy_manifest(args: argparse.Namespace, config) -> dict:
    if args.manifest is not None:
        return read_manifest_file(Path(args.manifest))

    client = ScoringClient(config)
    try:
        if args.round_id is not None:
            payload = client.fetch_round(args.round_id)
            metadata = RoundMetadata.from_api_payload(
                payload,
                requested_round_id=args.round_id,
            )
        else:
            metadata = select_latest_deployable_round(
                client.fetch_rounds(limit=args.round_limit)
            )
        fetched_package = fetch_verified_input_package(
            metadata,
            config,
            client,
            source=args.source,
            force=False,
        )
    finally:
        client.close()
    return load_round_manifest(fetched_package.local_path)


def _format_deployment_record(record: DeploymentRecord, config) -> str:
    return "\n".join(
        [
            f"Mode: {record.mode}",
            f"Endpoint URL: {record.endpoint_url}",
            f"Image: {record.image}",
            f"GPU class: {record.gpu_class}",
            f"Tensor parallelism: {record.tensor_parallelism}",
            f"Served model: {record.served_model_name}",
            f"Model revision: {record.model_revision}",
            f"Deployed at: {record.deployed_at}",
            f"Record path: {deployment_record_path(config)}",
        ]
    )


def _format_fetched_input_package(fetched_package: FetchedInputPackage) -> str:
    cache_status = "reused" if fetched_package.cached else "fetched"
    return "\n".join(
        [
            f"Round ID: {fetched_package.round_id}",
            f"Round number: {fetched_package.round_number}",
            f"Network: {fetched_package.network}",
            f"Input package CID: {fetched_package.input_package_cid}",
            f"Input package hash: {fetched_package.input_package_hash}",
            f"Input frozen at: {fetched_package.input_frozen_at}",
            f"Source: {fetched_package.source}",
            f"Cache status: {cache_status}",
            f"Verified files: {fetched_package.verified_file_count}",
            f"Local path: {fetched_package.local_path}",
        ]
    )


def _format_sync_result(result: SyncResult) -> str:
    if result.package is None:
        return "\n".join(
            [
                "Sync status: no eligible round",
                f"Network: {result.network}",
                f"Scanned rounds: {result.scanned_rounds}",
            ]
        )
    return "\n".join(
        [
            "Sync status: input package ready",
            f"Action: {result.action}",
            f"Scanned rounds: {result.scanned_rounds}",
            _format_fetched_input_package(result.package),
        ]
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        _argparse_error("must be an integer")
    if parsed <= 0:
        _argparse_error("must be greater than zero")
    return parsed


def _round_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_SYNC_ROUND_LIMIT:
        _argparse_error(f"must be less than or equal to {MAX_SYNC_ROUND_LIMIT}")
    return parsed


def _argparse_error(message: str) -> NoReturn:
    raise argparse.ArgumentTypeError(message)


def _print_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
