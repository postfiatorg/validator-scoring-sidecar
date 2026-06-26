"""Unattended on-chain participation pass (milestone M2.5.5).

Composes the M2.5.1–2.5.4 pieces into one pass an operator can run unattended:
score the latest eligible round through the existing API-driven path, observe the
foundation's on-chain announcement for that round's commit/reveal windows, then
submit the validator's commit inside the commit window and its reveal inside the
reveal window. Round discovery and scoring stay API-driven; the announcement
supplies only the windows and the ledger-anchored trust signal.

For Modal-backed operators the pass also owns the inference runtime: with Modal
account credentials configured, a missing or manifest-stale Modal deployment is
redeployed from the round's pinned manifest before scoring, so foundation
runtime upgrades do not stall unattended operation. A local-mode runtime is
never touched — the sidecar does not manage hardware it does not own.

Participation is all-or-nothing: it refuses to start unless a funded operator
relay wallet, validator-keys signing access, a reachable PFTL RPC, and a
discoverable foundation publisher address are all present — so it never spends on
inference it cannot follow through on chain.

Each pass is idempotent and restart-safe. The reveal phase is driven from local
state and runs first, independent of whether there is a new round to score, so a
round committed on an earlier pass still reveals in its window even when nothing
new is eligible. The commit phase only acts on the round scored this pass and
advances the chain cursor past an announcement only once it is terminally handled
(committed, already committed, window closed, or a round we will not commit) —
never past an announcement whose commit is still pending or whose handling hit a
transient error, so nothing is silently skipped. Per-round logic failures are
recorded without halting the pass; infrastructure failures (a failed account_tx
poll, a transient RPC or download error, a lock error) propagate so the pass
retries cleanly rather than advancing past unfinished work. The unattended loop
is the operator's scheduler invoking this pass at the chain-poll cadence.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from validator_scoring_sidecar.chain import (
    AnnouncementError,
    ChainWatcherError,
    FoundationConfig,
    PftlAccountWatcher,
    PftlRpcClient,
    PftlRpcError,
    VerifiedAnnouncement,
    decode_and_verify_announcement,
    resolve_foundation_publisher_address,
)
from validator_scoring_sidecar.commit import (
    COMMIT_STATUS_WINDOW_NOT_OPEN,
    Signer,
    submit_commit,
)
from validator_scoring_sidecar.config import (
    ENV_VALIDATOR_KEYS_PATH,
    ENV_VALIDATOR_WALLET_SEED,
    SidecarConfig,
)
from validator_scoring_sidecar.deployment import (
    NoEligibleRoundError,
    deploy_modal_endpoint,
)
from validator_scoring_sidecar.modal_deployer import (
    ENV_MODAL_TOKEN_ID,
    ENV_MODAL_TOKEN_SECRET,
    RealModalDeployer,
)
from validator_scoring_sidecar.input_package import (
    SOURCE_AUTO,
    PackageSource,
    fetch_input_package,
)
from validator_scoring_sidecar.reveal import RevealError, submit_reveal
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.score import RuntimeProvisioner, score_round
from validator_scoring_sidecar.scoring_client import ScoringClient, ScoringClientError
from validator_scoring_sidecar.state import (
    SCORED_OR_FURTHER_STATES,
    RoundStateRecord,
    SidecarState,
)
from validator_scoring_sidecar.sync import DEFAULT_SYNC_ROUND_LIMIT, SidecarLock
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_SELECTED_UNL,
    HASH_VALIDATOR_SCORES,
)

SCORE_STATUS_NO_ELIGIBLE_ROUND = "no_eligible_round"
ADVANCE_STATUS_ROUND_NOT_SCORED = "round_not_scored"
ADVANCE_STATUS_ANNOUNCEMENT_ERROR = "announcement_error"
ADVANCE_STATUS_ERROR = "error"


class ParticipationConfigError(RuntimeError):
    """Raised when the prerequisites for on-chain participation are not met."""


@dataclass(frozen=True)
class ParticipateResult:
    """Summary of one participation pass."""

    network: str
    score_status: str
    round_id: int
    round_number: int
    commits: list[dict[str, Any]]
    reveals: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "score_status": self.score_status,
            "round_id": self.round_id,
            "round_number": self.round_number,
            "commits": list(self.commits),
            "reveals": list(self.reveals),
        }


def require_participation_config(config: SidecarConfig) -> tuple[str, str]:
    """Return the validated (wallet seed, validator-keys path), or fail fast.

    Guards the secret-bearing prerequisites that must be present before any
    scoring or inference spend. Publisher discovery and RPC reachability need I/O
    and are checked in ``participate`` itself; these two gate the command up front
    so an operator who has not fully configured participation is told immediately
    rather than after paying for a round.
    """

    wallet_seed = config.validator_wallet_seed
    if not wallet_seed:
        raise ParticipationConfigError(
            f"participation requires {ENV_VALIDATOR_WALLET_SEED}; set it or use "
            "the verify-only `sync` / `score` commands"
        )
    keys_path = config.validator_keys_path
    if not keys_path:
        raise ParticipationConfigError(
            f"participation requires {ENV_VALIDATOR_KEYS_PATH}; set it or use "
            "the verify-only `sync` / `score` commands"
        )
    return wallet_seed, keys_path


def modal_runtime_provisioner(
    config: SidecarConfig,
    *,
    environ: Mapping[str, str] | None = None,
    deployer_factory: Callable[[], Any] = RealModalDeployer,
) -> RuntimeProvisioner | None:
    """Build the Modal auto-provisioner, or ``None`` without Modal credentials.

    This is the participation half of the auto-provisioning gate: absent the
    Modal account tokens the loop never attempts a deployment, so local-runtime
    operators and unconfigured setups keep today's behavior exactly. The score
    path enforces the other half — a local-mode deployment record is never
    replaced regardless of credentials.
    """

    env = os.environ if environ is None else environ
    if not (env.get(ENV_MODAL_TOKEN_ID) and env.get(ENV_MODAL_TOKEN_SECRET)):
        return None

    def provision(manifest: dict[str, Any]) -> dict[str, Any]:
        record = deploy_modal_endpoint(
            manifest,
            config,
            deployer=deployer_factory(),
            app_name=config.modal_app_name,
        )
        return record.as_dict()

    return provision


def participate(
    config: SidecarConfig,
    client: ScoringClient,
    *,
    rpc_client: PftlRpcClient,
    signer: Signer,
    source: PackageSource = SOURCE_AUTO,
    round_limit: int = DEFAULT_SYNC_ROUND_LIMIT,
    score_runner: Callable[..., Any] = score_round,
    announcement_decoder: Callable[
        ..., VerifiedAnnouncement | None
    ] = decode_and_verify_announcement,
    package_fetcher=fetch_input_package,
    runtime_provisioner: RuntimeProvisioner | None = None,
) -> ParticipateResult:
    """Run one unattended participation pass for the latest eligible round."""

    require_participation_config(config)
    publisher = _resolve_publisher(config, client)
    _probe_rpc(config, rpc_client)

    provisioner = (
        runtime_provisioner
        if runtime_provisioner is not None
        else modal_runtime_provisioner(config)
    )
    try:
        score = score_runner(
            config,
            client,
            source=source,
            round_limit=round_limit,
            runtime_provisioner=provisioner,
        )
        score_status = score.status
        active_round_id: int | None = score.round_id
        active_round_number = score.round_number
    except NoEligibleRoundError:
        # No round to score this pass; the reveal phase is still driven from local
        # state, so a committed round in its reveal window must still be handled.
        score_status = SCORE_STATUS_NO_ELIGIBLE_ROUND
        active_round_id = None
        active_round_number = 0

    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        # Reveals first: state-driven and independent of the commit phase, so a
        # commit-phase failure never blocks a pending reveal.
        reveals = _advance_reveals(
            config,
            state,
            rpc_client=rpc_client,
            signer=signer,
            publisher=publisher,
        )
        commits = (
            _advance_commits(
                config,
                client,
                state,
                active_round_id,
                active_round_number,
                rpc_client=rpc_client,
                signer=signer,
                publisher=publisher,
                announcement_decoder=announcement_decoder,
                package_fetcher=package_fetcher,
                round_limit=round_limit,
            )
            if active_round_id is not None
            else []
        )

    return ParticipateResult(
        network=config.network,
        score_status=score_status,
        round_id=active_round_id or 0,
        round_number=active_round_number,
        commits=commits,
        reveals=reveals,
    )


def _advance_commits(
    config: SidecarConfig,
    client: ScoringClient,
    state: SidecarState,
    active_round_id: int,
    active_round_number: int,
    *,
    rpc_client: PftlRpcClient,
    signer: Signer,
    publisher: str,
    announcement_decoder: Callable[..., VerifiedAnnouncement | None],
    package_fetcher,
    round_limit: int,
) -> list[dict[str, Any]]:
    """Commit the round scored this pass when its announcement is in the watcher's
    feed and its commit window is open.

    The chain cursor advances past a transaction only once it is terminally
    handled, so an announcement whose commit is still pending (window not yet
    open) or whose handling hit a transient error is reprocessed on a later pass
    rather than lost. A round newer than the one scored this pass is left in place
    for a future pass to score and commit.
    """

    watcher = PftlAccountWatcher(
        rpc_client=rpc_client,
        state=state,
        network=config.network,
        publisher_address=publisher,
    )
    results: list[dict[str, Any]] = []
    for transaction in watcher.poll():
        try:
            verified = announcement_decoder(
                transaction,
                config,
                client,
                package_fetcher=package_fetcher,
                round_limit=round_limit,
            )
        except AnnouncementError as exc:
            # A malformed announcement from the trusted sender will not improve;
            # record it and move past so it cannot wedge the cursor. Transient
            # decode failures (download/verify/RPC) are not caught here, so they
            # propagate and the announcement is retried on a later pass.
            results.append(
                {
                    "tx_hash": transaction.tx_hash,
                    "status": ADVANCE_STATUS_ANNOUNCEMENT_ERROR,
                    "error": str(exc),
                }
            )
            watcher.advance_cursor(transaction)
            continue

        if verified is None:
            # Not a round announcement (e.g. a foundation VL receipt).
            watcher.advance_cursor(transaction)
            continue
        if verified.package.round_id != active_round_id:
            if verified.announcement.round_number > active_round_number:
                # A round newer than the one scored this pass; leave it for a
                # later pass to score and commit rather than skipping it.
                break
            # An older round this validator will not commit.
            watcher.advance_cursor(transaction)
            continue

        record = state.get_round(config.network, active_round_id)
        result, advance = _commit_active_round(
            config,
            record,
            verified,
            rpc_client=rpc_client,
            signer=signer,
            state=state,
            publisher=publisher,
        )
        results.append(result)
        if advance:
            watcher.advance_cursor(transaction)
        else:
            # Commit window not yet open: hold the cursor and retry next pass.
            break
    return results


def _commit_active_round(
    config: SidecarConfig,
    record: RoundStateRecord | None,
    verified: VerifiedAnnouncement,
    *,
    rpc_client: PftlRpcClient,
    signer: Signer,
    state: SidecarState,
    publisher: str,
) -> tuple[dict[str, Any], bool]:
    """Attempt the commit for the active round and report whether the cursor may
    advance past its announcement."""

    round_number = verified.announcement.round_number
    output_hashes = _committable_output_hashes(record)
    if record is None or output_hashes is None:
        # Scoring failed or was skipped, or the round has no frozen previous UNL,
        # so it will not be committed; advancing past it is terminal.
        return (
            {"round_number": round_number, "status": ADVANCE_STATUS_ROUND_NOT_SCORED},
            True,
        )
    commit = submit_commit(
        verified.announcement,
        output_hashes,
        config,
        _metadata_from_record(record),
        rpc_client=rpc_client,
        signer=signer,
        state=state,
        foundation_publisher_address=publisher,
    )
    result = {
        "round_number": commit.round_number,
        "status": commit.status,
        "tx_hash": commit.commit_tx_hash,
    }
    return result, commit.status != COMMIT_STATUS_WINDOW_NOT_OPEN


def _advance_reveals(
    config: SidecarConfig,
    state: SidecarState,
    *,
    rpc_client: PftlRpcClient,
    signer: Signer,
    publisher: str,
) -> list[dict[str, Any]]:
    """Reveal every committed round still awaiting its reveal.

    Reveal happens passes after commit, once the chain cursor has moved on, so it
    is driven from local state rather than from the announcement. ``submit_reveal``
    self-gates on the reveal window, so calling it before the window opens is a
    no-op and after it closes records the miss. A per-round ``RevealError``
    isolates that round and is recorded without halting the pass; transient RPC
    failures propagate so the pass retries.
    """

    results: list[dict[str, Any]] = []
    for record in state.get_rounds_pending_reveal(config.network):
        metadata = _metadata_from_record(record)
        try:
            reveal = submit_reveal(
                config,
                metadata,
                rpc_client=rpc_client,
                signer=signer,
                state=state,
                foundation_publisher_address=publisher,
            )
        except RevealError as exc:
            results.append(
                {
                    "round_number": record.round_number,
                    "status": ADVANCE_STATUS_ERROR,
                    "error": str(exc),
                }
            )
            continue
        results.append(
            {
                "round_number": reveal.round_number,
                "status": reveal.status,
                "tx_hash": reveal.reveal_tx_hash,
            }
        )
    return results


def _committable_output_hashes(
    record: RoundStateRecord | None,
) -> dict[str, str] | None:
    if record is None or record.sidecar_state not in SCORED_OR_FURTHER_STATES:
        return None
    model = record.model_response_hash
    scores = record.validator_scores_hash
    unl = record.selected_unl_hash
    if not (model and scores and unl):
        return None
    return {
        HASH_MODEL_RESPONSE: model,
        HASH_VALIDATOR_SCORES: scores,
        HASH_SELECTED_UNL: unl,
    }


def _metadata_from_record(record: RoundStateRecord) -> RoundMetadata:
    return RoundMetadata(
        round_id=record.round_id,
        round_number=record.round_number,
        status=record.scoring_status,
        input_package_cid=record.input_package_cid,
        input_package_hash=record.input_package_hash,
        input_frozen_at=record.input_frozen_at,
        final_bundle_cid=None,
    )


def _resolve_publisher(config: SidecarConfig, client: ScoringClient) -> str:
    foundation_config: FoundationConfig | None = None
    if not config.foundation_publisher_address:
        try:
            foundation_config = FoundationConfig.from_api_payload(client.fetch_config())
        except ScoringClientError as exc:
            raise ParticipationConfigError(
                f"could not fetch foundation config for publisher discovery: {exc}"
            ) from exc
    try:
        return resolve_foundation_publisher_address(config, foundation_config)
    except ChainWatcherError as exc:
        raise ParticipationConfigError(str(exc)) from exc


def _probe_rpc(config: SidecarConfig, rpc_client: PftlRpcClient) -> None:
    try:
        rpc_client.latest_validated_ledger_close_time()
    except PftlRpcError as exc:
        raise ParticipationConfigError(
            f"PFTL RPC at {config.pftl_rpc_url} is not reachable: {exc}"
        ) from exc
