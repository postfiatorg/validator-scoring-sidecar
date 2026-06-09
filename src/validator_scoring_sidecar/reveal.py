"""Validator reveal submission for the Dynamic UNL sidecar (milestone M2.5.4).

After a round is committed, the validator opens its sealed commitment by
publishing the committed output fingerprints and salt on PFTL — the second half
of commit-reveal. The reveal is rebuilt verbatim from local state so it always
opens the validator's own on-chain commitment; authorship is bound by a
validator master-key signature inside the memo, and the transaction is paid for
and sent by the same funded operator relay wallet used for the commit.

A reveal is published only inside the announced reveal window (enforced against
the validated-ledger close time) and only when the locally stored outputs and
salt still reproduce the committed commitment. Refusing to open a commitment the
local state can no longer reproduce is a corruption guard, independent of whether
the round agreed with the foundation — a divergent result is still revealed. A
window that closes unrevealed is a chain-participation miss, recorded without
failing the round's score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from validator_scoring_sidecar.chain import (
    ChainWatcherError,
    PftlInsufficientFundsError,
    PftlRpcClient,
    find_authored_memo_tx_hash,
)
from validator_scoring_sidecar.commit import DEFAULT_ACCOUNT_TX_SCAN_LIMIT, Signer
from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.state import RoundStateRecord, SidecarState
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_SELECTED_UNL,
    HASH_VALIDATOR_SCORES,
)

REVEAL_STATUS_SUBMITTED = "revealed"
REVEAL_STATUS_ALREADY_REVEALED = "already_revealed"
REVEAL_STATUS_NOT_COMMITTED = "not_committed"
REVEAL_STATUS_WINDOW_NOT_OPEN = "reveal_window_not_open"
REVEAL_STATUS_WINDOW_MISSED = "reveal_window_missed"
REVEAL_STATUS_SKIPPED_LOW_BALANCE = "skipped_low_balance"


class RevealError(ChainWatcherError):
    """Raised when a reveal cannot be built or submitted (non-window failure)."""


@dataclass(frozen=True)
class RevealResult:
    """Outcome of a reveal attempt for one round."""

    status: str
    round_number: int
    reveal_tx_hash: str | None = None


@dataclass(frozen=True)
class _CommittedRound:
    """The validated, fully-typed reveal inputs read back from local state.

    Loading through this view narrows the nullable round-state columns to the
    non-optional values the reveal needs and proves the stored outputs and salt
    reproduce the committed commitment before anything is broadcast.
    """

    round_number: int
    input_package_hash: str
    validator_master_key: str
    salt: str
    output_hashes: commit_reveal.OutputHashes
    reveal_opens_at: datetime
    reveal_closes_at: datetime


def submit_reveal(
    config: SidecarConfig,
    metadata: RoundMetadata,
    *,
    rpc_client: PftlRpcClient,
    signer: Signer,
    state: SidecarState,
    foundation_publisher_address: str,
    account_tx_limit: int = DEFAULT_ACCOUNT_TX_SCAN_LIMIT,
) -> RevealResult:
    """Build, sign, and submit the validator's reveal for a committed round.

    Returns a ``RevealResult`` describing the outcome. Raises ``RevealError`` on a
    configuration, integrity, or signing failure that must not be silently
    skipped.
    """

    wallet_seed = config.validator_wallet_seed
    if not wallet_seed:
        raise RevealError(
            "no operator wallet seed configured; set "
            "POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED to submit reveals"
        )

    existing = state.get_round(config.network, metadata.round_id)
    if existing is None or not existing.commit_tx_hash:
        return RevealResult(REVEAL_STATUS_NOT_COMMITTED, metadata.round_number)
    if existing.reveal_tx_hash:
        return RevealResult(
            REVEAL_STATUS_ALREADY_REVEALED,
            existing.round_number,
            existing.reveal_tx_hash,
        )

    committed = _load_committed_round(config.network, existing)

    close_time = rpc_client.latest_validated_ledger_close_time()
    if close_time < committed.reveal_opens_at:
        return RevealResult(REVEAL_STATUS_WINDOW_NOT_OPEN, committed.round_number)

    if signer.master_key != committed.validator_master_key:
        raise RevealError(
            "configured validator key does not match the key that committed this "
            "round; refusing to reveal"
        )

    # Scan the chain before declaring a miss: a reveal may already be on-chain
    # from a prior run that crashed before persisting its hash, in which case the
    # round was revealed, not missed.
    onchain_hash = find_authored_memo_tx_hash(
        rpc_client,
        account=foundation_publisher_address,
        memo_type=commit_reveal.VALIDATOR_REVEAL_TYPE,
        validate=commit_reveal.validate_reveal_payload,
        network=config.network,
        round_number=committed.round_number,
        input_package_hash=committed.input_package_hash,
        validator_master_key=committed.validator_master_key,
        limit=account_tx_limit,
    )
    if onchain_hash is not None:
        state.record_reveal(config.network, metadata, reveal_tx_hash=onchain_hash)
        return RevealResult(
            REVEAL_STATUS_ALREADY_REVEALED, committed.round_number, onchain_hash
        )

    if close_time >= committed.reveal_closes_at:
        state.record_reveal_miss(
            config.network,
            metadata,
            error_category=FailureCategory.REVEAL_WINDOW_MISSED.value,
        )
        return RevealResult(REVEAL_STATUS_WINDOW_MISSED, committed.round_number)

    signing_bytes = commit_reveal.build_reveal_signing_bytes(
        protocol_version=commit_reveal.PROTOCOL_VERSION,
        network=config.network,
        round_number=committed.round_number,
        validator_master_key=committed.validator_master_key,
        input_package_hash=committed.input_package_hash,
        output_hashes=committed.output_hashes,
        salt=committed.salt,
    )
    signature = signer.sign(signing_bytes)
    if not commit_reveal.verify_validator_master_signature(
        validator_master_key=committed.validator_master_key,
        message=signing_bytes,
        signature=signature,
    ):
        raise RevealError("reveal signature failed local verification; not submitting")

    reveal_payload = commit_reveal.build_reveal_payload(
        protocol_version=commit_reveal.PROTOCOL_VERSION,
        network=config.network,
        round_number=committed.round_number,
        validator_master_key=committed.validator_master_key,
        input_package_hash=committed.input_package_hash,
        output_hashes=committed.output_hashes,
        salt=committed.salt,
        signature=signature,
    )
    memo_data = commit_reveal.canonical_json_bytes(reveal_payload).decode("utf-8")

    try:
        tx_hash = rpc_client.submit_memo(
            wallet_seed=wallet_seed,
            destination=foundation_publisher_address,
            memo_type=commit_reveal.VALIDATOR_REVEAL_TYPE,
            memo_data=memo_data,
        )
    except PftlInsufficientFundsError:
        # Transient: the window may still be open on a later pass once funded, so
        # leave the round COMMITTED rather than recording a terminal miss.
        return RevealResult(REVEAL_STATUS_SKIPPED_LOW_BALANCE, committed.round_number)

    state.record_reveal(config.network, metadata, reveal_tx_hash=tx_hash)
    return RevealResult(REVEAL_STATUS_SUBMITTED, committed.round_number, tx_hash)


def _load_committed_round(
    network: str, existing: RoundStateRecord
) -> _CommittedRound:
    """Validate and narrow a committed round's stored reveal inputs.

    Raises ``RevealError`` if a required column is missing or if the stored
    outputs and salt do not reproduce the committed commitment (local-state
    corruption); the latter would yield a reveal that fails to open the commit.
    """

    output_hashes = commit_reveal.OutputHashes(
        model_response_hash=_require(HASH_MODEL_RESPONSE, existing.model_response_hash),
        validator_scores_hash=_require(
            HASH_VALIDATOR_SCORES, existing.validator_scores_hash
        ),
        selected_unl_hash=_require(HASH_SELECTED_UNL, existing.selected_unl_hash),
    )
    master_key = _require("validator master key", existing.validator_master_key)
    salt = _require("salt", existing.salt)
    # Consumed only by the integrity guard below, so it is not carried on the
    # returned _CommittedRound.
    commitment_hash = _require("commitment hash", existing.commitment_hash)
    reveal_opens_at = _parse_window("reveal_opens_at", existing.reveal_opens_at)
    reveal_closes_at = _parse_window("reveal_closes_at", existing.reveal_closes_at)

    try:
        recomputed = commit_reveal.compute_commitment_hash(
            protocol_version=commit_reveal.PROTOCOL_VERSION,
            network=network,
            round_number=existing.round_number,
            validator_master_key=master_key,
            input_package_hash=existing.input_package_hash,
            output_hashes=output_hashes,
            salt=salt,
        )
    except commit_reveal.CommitRevealValidationError as exc:
        raise RevealError(
            f"committed round state is invalid; refusing to reveal: {exc}"
        ) from exc
    if recomputed != commitment_hash:
        raise RevealError(
            "stored outputs and salt do not reproduce the committed commitment; "
            "refusing to reveal (possible local-state corruption)"
        )

    return _CommittedRound(
        round_number=existing.round_number,
        input_package_hash=existing.input_package_hash,
        validator_master_key=master_key,
        salt=salt,
        output_hashes=output_hashes,
        reveal_opens_at=reveal_opens_at,
        reveal_closes_at=reveal_closes_at,
    )


def _require(name: str, value: str | None) -> str:
    if not value:
        raise RevealError(
            f"committed round is missing {name}; cannot rebuild the reveal"
        )
    return value


def _parse_window(name: str, value: str | None) -> datetime:
    if not value:
        raise RevealError(
            f"committed round is missing {name}; cannot enforce the reveal window"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RevealError(f"committed round has an invalid {name}: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RevealError(f"committed round {name} is not timezone-aware: {value!r}")
    return parsed
