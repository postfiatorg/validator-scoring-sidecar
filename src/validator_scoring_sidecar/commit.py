"""Validator commit submission for the Dynamic UNL sidecar (milestone M2.5.3).

After a round is scored and its announcement decoded, the validator publishes a
salted commitment to its three output fingerprints on PFTL — the sealed-envelope
first half of commit-reveal. Authorship is bound by a validator master-key
signature inside the memo; the transaction is paid for and sent by a separate
funded operator relay wallet (sender != identity, by design).

The commitment hides the validator's result behind a random per-round salt until
the reveal step (M2.5.4). The salt and the commit/reveal windows are persisted so
the reveal can reopen the envelope.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from validator_scoring_sidecar.chain import (
    ChainWatcherError,
    PftlInsufficientFundsError,
    PftlRpcClient,
    find_authored_memo_tx_hash,
)
from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.state import (
    CommitOutcome,
    SidecarState,
)
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_SELECTED_UNL,
    HASH_VALIDATOR_SCORES,
)

SALT_BYTES = 32
DEFAULT_ACCOUNT_TX_SCAN_LIMIT = 200

COMMIT_STATUS_SUBMITTED = "committed"
COMMIT_STATUS_ALREADY_COMMITTED = "already_committed"
COMMIT_STATUS_WINDOW_NOT_OPEN = "commit_window_not_open"
COMMIT_STATUS_WINDOW_CLOSED = "commit_window_closed"
COMMIT_STATUS_SKIPPED_LOW_BALANCE = "skipped_low_balance"

_REQUIRED_OUTPUT_HASHES = (
    HASH_MODEL_RESPONSE,
    HASH_VALIDATOR_SCORES,
    HASH_SELECTED_UNL,
)


class CommitError(ChainWatcherError):
    """Raised when a commit cannot be built or submitted (non-window failure)."""


class Signer(Protocol):
    """Signs canonical payload bytes with the validator master key.

    ``master_key`` is the validator's ``nH...`` master public key; ``sign``
    returns the hex signature over ``message`` that verifies against it.
    """

    @property
    def master_key(self) -> str: ...

    def sign(self, message: bytes) -> str: ...


class ValidatorKeysSigner:
    """Signer backed by the postfiatd ``validator-keys`` tool.

    Reads the validator master key from ``validator-keys.json`` and shells out to
    ``validator-keys sign`` for each payload. The key file and binary must be
    available to the sidecar; the seed and signatures are never logged. The exact
    file field and CLI output format are pinned to the postfiatd tool — unit tests
    inject a real-crypto fake signer instead of invoking it.
    """

    def __init__(self, *, validator_keys_path: str, binary: str = "validator-keys"):
        self._path = validator_keys_path
        self._binary = binary
        self._master_key: str | None = None

    @property
    def master_key(self) -> str:
        if self._master_key is None:
            try:
                data = json.loads(Path(self._path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CommitError(
                    f"could not read validator-keys file {self._path}: {exc}"
                ) from exc
            key = data.get("public_key") if isinstance(data, dict) else None
            if not isinstance(key, str) or not key.strip():
                raise CommitError(
                    f"validator-keys file {self._path} has no public_key"
                )
            self._master_key = key.strip()
        return self._master_key

    def sign(self, message: bytes) -> str:
        try:
            completed = subprocess.run(
                [self._binary, "sign", message.decode("utf-8")],
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CommitError(f"validator-keys sign failed: {exc}") from exc
        tokens = completed.stdout.split()
        if not tokens:
            raise CommitError("validator-keys sign produced no signature")
        return tokens[-1]


@dataclass(frozen=True)
class CommitResult:
    """Outcome of a commit attempt for one round."""

    status: str
    round_number: int
    commit_tx_hash: str | None = None


def submit_commit(
    announcement: commit_reveal.RoundAnnouncement,
    output_hashes: dict[str, str],
    config: SidecarConfig,
    metadata: RoundMetadata,
    *,
    rpc_client: PftlRpcClient,
    signer: Signer,
    state: SidecarState,
    foundation_publisher_address: str,
    account_tx_limit: int = DEFAULT_ACCOUNT_TX_SCAN_LIMIT,
    salt: str | None = None,
) -> CommitResult:
    """Build, sign, and submit the validator's commit for a round.

    Returns a ``CommitResult`` describing the outcome. Raises ``CommitError`` on
    a configuration or signing failure that should not be silently skipped.
    """

    wallet_seed = config.validator_wallet_seed
    if not wallet_seed:
        raise CommitError(
            "no operator wallet seed configured; set "
            "POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED to submit commits"
        )

    existing = state.get_round(config.network, metadata.round_id)
    if existing is not None and existing.commit_tx_hash:
        return CommitResult(
            COMMIT_STATUS_ALREADY_COMMITTED,
            announcement.round_number,
            existing.commit_tx_hash,
        )

    missing = [name for name in _REQUIRED_OUTPUT_HASHES if not output_hashes.get(name)]
    if missing:
        raise CommitError(
            f"missing output hashes required to commit: {', '.join(missing)}"
        )

    master_key = signer.master_key

    close_time = rpc_client.latest_validated_ledger_close_time()
    if close_time < announcement.commit_opens_at:
        return CommitResult(COMMIT_STATUS_WINDOW_NOT_OPEN, announcement.round_number)
    if close_time >= announcement.commit_closes_at:
        return CommitResult(COMMIT_STATUS_WINDOW_CLOSED, announcement.round_number)

    onchain_hash = find_authored_memo_tx_hash(
        rpc_client,
        account=foundation_publisher_address,
        memo_type=commit_reveal.VALIDATOR_COMMIT_TYPE,
        validate=commit_reveal.validate_commit_payload,
        network=announcement.network,
        round_number=announcement.round_number,
        input_package_hash=announcement.input_package_hash,
        validator_master_key=master_key,
        limit=account_tx_limit,
    )
    if onchain_hash is not None:
        return CommitResult(
            COMMIT_STATUS_ALREADY_COMMITTED, announcement.round_number, onchain_hash
        )

    salt = salt or os.urandom(SALT_BYTES).hex()
    output_hashes_obj = commit_reveal.OutputHashes(
        model_response_hash=output_hashes[HASH_MODEL_RESPONSE],
        validator_scores_hash=output_hashes[HASH_VALIDATOR_SCORES],
        selected_unl_hash=output_hashes[HASH_SELECTED_UNL],
    )
    commitment_hash = commit_reveal.compute_commitment_hash(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=master_key,
        input_package_hash=announcement.input_package_hash,
        output_hashes=output_hashes_obj,
        salt=salt,
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
        raise CommitError(
            "commit signature failed local verification; not submitting"
        )

    commit_payload = commit_reveal.build_commit_payload(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=master_key,
        input_package_hash=announcement.input_package_hash,
        commitment_hash=commitment_hash,
        signature=signature,
    )
    memo_data = commit_reveal.canonical_json_bytes(commit_payload).decode("utf-8")

    try:
        tx_hash = rpc_client.submit_memo(
            wallet_seed=wallet_seed,
            destination=foundation_publisher_address,
            memo_type=commit_reveal.VALIDATOR_COMMIT_TYPE,
            memo_data=memo_data,
        )
    except PftlInsufficientFundsError:
        state.record_commit_skip(
            config.network,
            metadata,
            error_category=FailureCategory.SKIPPED_OPERATOR_OPT_OUT.value,
            error_details={"reason": "low_balance"},
        )
        return CommitResult(
            COMMIT_STATUS_SKIPPED_LOW_BALANCE, announcement.round_number
        )

    state.record_commit(
        config.network,
        metadata,
        CommitOutcome(
            validator_master_key=master_key,
            salt=salt,
            commit_tx_hash=tx_hash,
            commitment_hash=commitment_hash,
            commit_opens_at=announcement.commit_opens_at.isoformat(),
            commit_closes_at=announcement.commit_closes_at.isoformat(),
            reveal_opens_at=announcement.reveal_opens_at.isoformat(),
            reveal_closes_at=announcement.reveal_closes_at.isoformat(),
        ),
    )
    return CommitResult(COMMIT_STATUS_SUBMITTED, announcement.round_number, tx_hash)
