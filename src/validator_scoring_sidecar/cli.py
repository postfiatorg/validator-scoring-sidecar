"""Command line interface for validator scoring sidecar operations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from validator_scoring_sidecar.config import ConfigError, load_config
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

    inspect = subparsers.add_parser(
        "inspect-round",
        help="Inspect frozen input metadata for a public scoring round.",
    )
    inspect.add_argument(
        "--round-id",
        type=_positive_int,
        required=True,
        help="Scoring service database round ID to inspect.",
    )
    inspect.add_argument(
        "--base-url",
        help="Scoring service base URL. Overrides POSTFIAT_SCORING_BASE_URL.",
    )
    inspect.add_argument(
        "--data-dir",
        help="Local sidecar data directory. Overrides POSTFIAT_SIDECAR_DATA_DIR.",
    )
    inspect.add_argument(
        "--network",
        help="Network label. Overrides POSTFIAT_SIDECAR_NETWORK.",
    )
    inspect.add_argument(
        "--timeout",
        type=float,
        help="HTTP request timeout in seconds. Overrides POSTFIAT_SIDECAR_TIMEOUT_SECONDS.",
    )
    inspect.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    inspect.set_defaults(handler=inspect_round)
    return parser


def inspect_round(args: argparse.Namespace) -> int:
    config = load_config(
        base_url=args.base_url,
        data_dir=args.data_dir,
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
    except MissingFrozenInputMetadata as exc:
        _print_error(str(exc))
        return EXIT_OPERATOR_ERROR
    except RoundMetadataError as exc:
        _print_error(f"Malformed round metadata: {exc}")
        return EXIT_NETWORK_ERROR
    except ScoringClientError as exc:
        _print_error(str(exc))
        return EXIT_NETWORK_ERROR
    finally:
        client.close()

    if args.json:
        print(json.dumps(metadata.as_dict(), indent=2, sort_keys=True))
    else:
        print(_format_human(metadata))

    return EXIT_OK


def _format_human(metadata: RoundMetadata) -> str:
    final_bundle = metadata.final_bundle_cid or "(not published yet)"
    return "\n".join(
        [
            f"Round ID: {metadata.round_id}",
            f"Round number: {metadata.round_number}",
            f"Status: {metadata.status}",
            f"Input package CID: {metadata.input_package_cid}",
            f"Input package hash: {metadata.input_package_hash}",
            f"Input frozen at: {metadata.input_frozen_at}",
            f"Final bundle CID: {final_bundle}",
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

