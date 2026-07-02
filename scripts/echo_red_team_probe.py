"""Echo red-team harness for Phase 2 hash-withholding campaigns.

This script intentionally does not run model scoring. It checks whether the
foundation's final verification hashes are available during a live commit
window. A 404 is the expected result. If hashes are available and
``--submit-commit`` is passed, the script submits a commit built from those
leaked hashes to prove an echo participant cannot become valid under the fixed
protocol.

Exit codes:

- 0: output hashes were not available (expected).
- 1: output hashes were available; with ``--submit-commit`` a commit was
  attempted/submitted.
- 2: harness/configuration/runtime error.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass

from validator_scoring_sidecar.chain import PftlRpcError, XrplPftlRpcClient
from validator_scoring_sidecar.commit import CommitError, ValidatorKeysSigner
from validator_scoring_sidecar.config import ConfigError, load_config
from validator_scoring_sidecar.score import FOUNDATION_VERIFICATION_HASHES_PATH
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.scoring_client import (
    ScoringClient,
    ScoringClientError,
    ScoringHTTPError,
)
from validator_scoring_sidecar.state import STATE_DB_FILENAME
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_SELECTED_UNL,
    HASH_VALIDATOR_SCORES,
)

HTTP_NOT_FOUND = 404
EXIT_OK = 0
EXIT_LEAK = 1
EXIT_ERROR = 2
SALT = "e" * 64
DESCRIPTION = "Probe for mid-window foundation output leaks and optionally echo-commit them."


@dataclass(frozen=True)
class RoundWindow:
    round_id: int
    round_number: int
    scoring_status: str
    input_package_cid: str
    input_package_hash: str
    input_frozen_at: str
    commit_opens_at: str
    commit_closes_at: str
    reveal_opens_at: str
    reveal_closes_at: str


def _load_round_window(data_dir, network: str, round_number: int) -> RoundWindow:
    db_path = data_dir / STATE_DB_FILENAME
    if not db_path.exists():
        raise RuntimeError(
            f"sidecar state database not found at {db_path}; run participate once "
            "so announcement windows are recorded"
        )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT round_id, round_number, scoring_status, input_package_cid,
                   input_package_hash, input_frozen_at, commit_opens_at,
                   commit_closes_at, reveal_opens_at, reveal_closes_at
            FROM sidecar_rounds
            WHERE network = ? AND round_number = ?
            ORDER BY round_id DESC
            LIMIT 1
            """,
            (network, round_number),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise RuntimeError(
            f"round {round_number} is not present in local sidecar state for {network}"
        )
    missing = [
        name
        for name in (
            "commit_opens_at",
            "commit_closes_at",
            "reveal_opens_at",
            "reveal_closes_at",
        )
        if not row[name]
    ]
    if missing:
        raise RuntimeError(
            f"round {round_number} has no recorded announcement window fields: "
            f"{', '.join(missing)}"
        )

    return RoundWindow(
        round_id=int(row["round_id"]),
        round_number=int(row["round_number"]),
        scoring_status=str(row["scoring_status"]),
        input_package_cid=str(row["input_package_cid"]),
        input_package_hash=str(row["input_package_hash"]),
        input_frozen_at=str(row["input_frozen_at"]),
        commit_opens_at=str(row["commit_opens_at"]),
        commit_closes_at=str(row["commit_closes_at"]),
        reveal_opens_at=str(row["reveal_opens_at"]),
        reveal_closes_at=str(row["reveal_closes_at"]),
    )


def _extract_output_hashes(payload) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise RuntimeError("verification hash payload is not a JSON object")
    hashes: dict[str, str] = {}
    for key in (HASH_MODEL_RESPONSE, HASH_VALIDATOR_SCORES, HASH_SELECTED_UNL):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"verification hash payload is missing {key}")
        hashes[key] = value.strip()
    commit_reveal.validate_output_hashes(hashes)
    return hashes


def _build_announcement(config, record: RoundWindow):
    return commit_reveal.build_round_announcement(
        network=config.network,
        round_number=record.round_number,
        input_package_cid=record.input_package_cid,
        input_package_hash=record.input_package_hash,
        commit_opens_at=record.commit_opens_at,
        commit_closes_at=record.commit_closes_at,
        reveal_opens_at=record.reveal_opens_at,
        reveal_closes_at=record.reveal_closes_at,
    )


def _submit_echo_commit(config, record: RoundWindow, hashes: dict[str, str]) -> str:
    if not config.foundation_publisher_address:
        raise RuntimeError(
            "foundation publisher address is required; set "
            "POSTFIAT_SIDECAR_FOUNDATION_PUBLISHER_ADDRESS or run with a network "
            "config that exposes it"
        )
    if not config.validator_wallet_seed:
        raise RuntimeError(
            "operator relay wallet seed is required in "
            "POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED"
        )
    if not config.validator_keys_path:
        raise RuntimeError(
            "validator key path is required in POSTFIAT_SIDECAR_VALIDATOR_KEYS_PATH"
        )

    announcement = _build_announcement(config, record)
    signer = ValidatorKeysSigner(validator_keys_path=config.validator_keys_path)
    master_key = signer.master_key
    output_hashes = commit_reveal.OutputHashes(
        model_response_hash=hashes[HASH_MODEL_RESPONSE],
        validator_scores_hash=hashes[HASH_VALIDATOR_SCORES],
        selected_unl_hash=hashes[HASH_SELECTED_UNL],
    )
    commitment_hash = commit_reveal.compute_commitment_hash(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=master_key,
        input_package_hash=announcement.input_package_hash,
        output_hashes=output_hashes,
        salt=SALT,
    )
    signing_bytes = commit_reveal.build_commit_signing_bytes(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=master_key,
        input_package_hash=announcement.input_package_hash,
        commitment_hash=commitment_hash,
    )
    signature = signer.sign(signing_bytes)
    if not commit_reveal.verify_validator_master_signature(
        validator_master_key=master_key,
        message=signing_bytes,
        signature=signature,
    ):
        raise RuntimeError("echo commit signature failed local verification")

    payload = commit_reveal.build_commit_payload(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=master_key,
        input_package_hash=announcement.input_package_hash,
        commitment_hash=commitment_hash,
        signature=signature,
    )
    memo_data = commit_reveal.canonical_json_bytes(payload).decode("utf-8")
    rpc_client = XrplPftlRpcClient(config.pftl_rpc_url)
    close_time = rpc_client.latest_validated_ledger_close_time()
    if close_time < announcement.commit_opens_at:
        raise RuntimeError(
            f"commit window is not open yet: {close_time.isoformat()} < "
            f"{announcement.commit_opens_at.isoformat()}"
        )
    if close_time >= announcement.commit_closes_at:
        raise RuntimeError(
            f"commit window is already closed: {close_time.isoformat()} >= "
            f"{announcement.commit_closes_at.isoformat()}"
        )
    return rpc_client.submit_memo(
        wallet_seed=config.validator_wallet_seed,
        destination=config.foundation_publisher_address,
        memo_type=commit_reveal.VALIDATOR_COMMIT_TYPE,
        memo_data=memo_data,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--round-number", type=int, required=True)
    parser.add_argument("--network", choices=("devnet", "testnet"))
    parser.add_argument("--base-url")
    parser.add_argument("--data-dir")
    parser.add_argument("--pftl-rpc-url")
    parser.add_argument("--foundation-publisher-address")
    parser.add_argument(
        "--submit-commit",
        action="store_true",
        help="If hashes leak, submit an echo commit using env-provided wallet/key config.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(
            base_url=args.base_url,
            data_dir=args.data_dir,
            network=args.network,
            pftl_rpc_url=args.pftl_rpc_url,
            foundation_publisher_address=args.foundation_publisher_address,
            environ=os.environ,
        )
        record = _load_round_window(config.data_dir, config.network, args.round_number)
        client = ScoringClient(config)
        try:
            payload = client.fetch_final_bundle_file(
                record.round_number,
                FOUNDATION_VERIFICATION_HASHES_PATH,
            )
        except ScoringHTTPError as exc:
            if exc.status_code == HTTP_NOT_FOUND:
                print(
                    "OK: output hashes are withheld "
                    f"for round {record.round_number} ({exc.url})"
                )
                return EXIT_OK
            raise
        finally:
            client.close()

        hashes = _extract_output_hashes(payload)
        print(
            "LEAK: output hashes were available before the campaign expected "
            f"them for round {record.round_number}"
        )
        for name in (HASH_MODEL_RESPONSE, HASH_VALIDATOR_SCORES, HASH_SELECTED_UNL):
            print(f"{name}={hashes[name]}")
        if args.submit_commit:
            tx_hash = _submit_echo_commit(config, record, hashes)
            print(f"submitted echo commit tx={tx_hash}")
        else:
            print("echo commit not submitted; pass --submit-commit to submit one")
        return EXIT_LEAK
    except (
        CommitError,
        ConfigError,
        PftlRpcError,
        ScoringClientError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
