"""Command line interface for validator scoring sidecar operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from validator_scoring_sidecar.config import ConfigError, load_config
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

EXIT_OK = 0
EXIT_OPERATOR_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_NETWORK_ERROR = 3


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
    fetch.add_argument(
        "--base-url",
        help="Scoring service base URL. Overrides POSTFIAT_SCORING_BASE_URL.",
    )
    fetch.add_argument(
        "--data-dir",
        help="Local sidecar data directory. Overrides POSTFIAT_SIDECAR_DATA_DIR.",
    )
    fetch.add_argument(
        "--ipfs-gateway-url",
        help=(
            "IPFS gateway URL prefix. Overrides "
            "POSTFIAT_SIDECAR_IPFS_GATEWAY_URL."
        ),
    )
    fetch.add_argument(
        "--network",
        help="Network label. Overrides POSTFIAT_SIDECAR_NETWORK.",
    )
    fetch.add_argument(
        "--timeout",
        type=float,
        help="HTTP request timeout in seconds. Overrides POSTFIAT_SIDECAR_TIMEOUT_SECONDS.",
    )
    fetch.add_argument(
        "--force",
        action="store_true",
        help="Refetch and replace an existing verified package cache.",
    )
    fetch.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    fetch.set_defaults(handler=fetch_input_package)
    return parser


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


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        _argparse_error("must be an integer")
    if parsed <= 0:
        _argparse_error("must be greater than zero")
    return parsed


def _argparse_error(message: str) -> NoReturn:
    raise argparse.ArgumentTypeError(message)


def _print_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
