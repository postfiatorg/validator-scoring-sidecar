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

Each pass is idempotent and restart-safe. Reveal and commit are both driven from
local state, so neither depends on a round being scored this pass nor on the
chain cursor position; the reveal phase runs first so a commit-phase issue never
blocks a pending reveal. The announcement walk only records each round's commit
and reveal windows and then advances the cursor unconditionally, and the commit
is replayed from local state for any scored round whose window is open and not
yet committed — so a round scored on a later pass than its announcement (for
example after a transient scoring failure) still commits while its window is open
rather than being forfeited. Per-round logic failures are recorded without
halting the pass. The unattended loop is the operator's scheduler invoking this
pass at the chain-poll cadence.

The chain phases are independent of foundation-service availability: a commit or
reveal is a pure PFTL transaction built from persisted local state, so a
foundation-API outage must never forfeit one. Foundation-side failures
(unreachable service, undownloadable or unverifiable input package) are
therefore contained — scoring is skipped for the pass (``scoring_unavailable``),
the announcement walk halts without advancing the cursor so the interrupted work
retries on a later pass, and publisher discovery falls back to the address cached
from an earlier successful pass — while the reveal and commit phases still run.
Everything else keeps failing the pass loudly: PFTL RPC failures and lock errors
(without the ledger no chain phase can proceed), local-state and package-cache
disk errors (a host that cannot write state cannot participate), and
runtime-provisioning or configuration errors raised while scoring (an operator
problem to surface, not to soften). The pass retries cleanly on the next tick
rather than advancing past unfinished work.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
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
    CommitError,
    Signer,
    submit_commit,
)
from validator_scoring_sidecar.config import (
    ENV_VALIDATOR_KEYS_PATH,
    ENV_VALIDATOR_WALLET_SEED,
    SidecarConfig,
)
from validator_scoring_sidecar.deployment import (
    DEPLOYMENT_MODE_LOCAL,
    NoEligibleRoundError,
    deploy_modal_endpoint,
    load_round_manifest,
    select_latest_deployable_round,
)
from validator_scoring_sidecar.modal_deployer import (
    ENV_MODAL_TOKEN_ID,
    ENV_MODAL_TOKEN_SECRET,
    RealModalDeployer,
)
from validator_scoring_sidecar.input_package import (
    SOURCE_AUTO,
    FetchedInputPackage,
    InputPackageDownloadError,
    InputPackageVerificationError,
    PackageSource,
    fetch_input_package,
)
from validator_scoring_sidecar.reveal import RevealError, submit_reveal
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.score import (
    FOUNDATION_VERIFICATION_HASHES_PATH,
    RuntimeProvisioner,
    provision_runtime_if_needed,
    score_round,
)
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.scoring_client import (
    ScoringClient,
    ScoringClientError,
    ScoringHTTPError,
)
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
SCORE_STATUS_SCORING_UNAVAILABLE = "scoring_unavailable"
ANNOUNCE_STATUS_WINDOWS_RECORDED = "windows_recorded"
ADVANCE_STATUS_ANNOUNCEMENT_ERROR = "announcement_error"
ADVANCE_STATUS_FOUNDATION_UNAVAILABLE = "foundation_unavailable"
ADVANCE_STATUS_ERROR = "error"

# Foundation-side failures: the scoring service API is unreachable, or its
# frozen input package cannot be downloaded or does not verify against its
# published hash. Contained so the chain phases still run; the skipped work
# retries on a later pass. Local faults are deliberately NOT in this set —
# InputPackageCacheError (disk) and SidecarStateError stay strict, because a
# host that cannot write local state cannot run the chain phases either.
_FOUNDATION_AVAILABILITY_ERRORS = (
    ScoringClientError,
    InputPackageDownloadError,
    InputPackageVerificationError,
)

# Foundation round status carried on a round first learned about from its
# on-chain announcement (emitted at INPUT_FROZEN); used only when the
# announcement walk inserts a round the score path has not recorded yet.
ROUND_STATUS_INPUT_FROZEN = "INPUT_FROZEN"

WARM_STATUS_READY = "ready"
WARM_STATUS_SKIPPED_NO_CREDENTIALS = "skipped_no_credentials"
WARM_STATUS_SKIPPED_LOCAL = "skipped_local"
WARM_STATUS_ROUND_NOT_DEPLOYABLE = "round_not_deployable"


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
    announcements: list[dict[str, Any]]
    protocol_violations: list[dict[str, Any]]
    score_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "score_status": self.score_status,
            "score_error": self.score_error,
            "round_id": self.round_id,
            "round_number": self.round_number,
            "commits": list(self.commits),
            "reveals": list(self.reveals),
            "announcements": list(self.announcements),
            "protocol_violations": list(self.protocol_violations),
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


@dataclass(frozen=True)
class WarmRuntimeResult:
    """Outcome of the startup runtime warm-up (see ``warm_modal_runtime``)."""

    status: str
    endpoint_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "endpoint_url": self.endpoint_url}


def warm_modal_runtime(
    config: SidecarConfig,
    client: ScoringClient,
    *,
    round_limit: int = DEFAULT_SYNC_ROUND_LIMIT,
    source: PackageSource = SOURCE_AUTO,
    package_fetcher=fetch_input_package,
    provisioner_factory: Callable[
        [SidecarConfig], RuntimeProvisioner | None
    ] = modal_runtime_provisioner,
) -> WarmRuntimeResult:
    """Provision the manifest-pinned Modal endpoint once at startup, before the
    first participation round.

    Moves the one-time Modal deploy (image build and kernel compilation) and GPU
    cold start out of the first round's window. Absent Modal credentials it is a
    no-op — local-SGLang and unconfigured operators keep today's behaviour — and
    it reuses the score path's runtime-resolution decision
    (``provision_runtime_if_needed``) so a valid, manifest-current endpoint is
    not redeployed and a local-mode record is never replaced.

    Configuration and infrastructure failures (a missing manifest, an
    unreachable scoring service, no eligible round, a Modal deploy error) are not
    swallowed here — they propagate to the caller, which decides the exit code.
    The container entrypoint runs this non-fatally, so a failed warm-up never
    blocks the participation loop, which still provisions the endpoint on demand.
    """

    provisioner = provisioner_factory(config)
    if provisioner is None:
        return WarmRuntimeResult(status=WARM_STATUS_SKIPPED_NO_CREDENTIALS)

    metadata = select_latest_deployable_round(client.fetch_rounds(limit=round_limit))
    fetched = package_fetcher(metadata, config, client, source=source, force=False)
    manifest = load_round_manifest(fetched.local_path)
    record = provision_runtime_if_needed(config, manifest, metadata, provisioner)

    if not record:
        return WarmRuntimeResult(status=WARM_STATUS_ROUND_NOT_DEPLOYABLE)
    if record.get("mode") == DEPLOYMENT_MODE_LOCAL:
        return WarmRuntimeResult(status=WARM_STATUS_SKIPPED_LOCAL)
    return WarmRuntimeResult(
        status=WARM_STATUS_READY,
        endpoint_url=record.get("endpoint_url"),
    )


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
    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        publisher = _resolve_publisher(config, client, state)
    _probe_rpc(config, rpc_client)

    provisioner = (
        runtime_provisioner
        if runtime_provisioner is not None
        else modal_runtime_provisioner(config)
    )
    score_error: str | None = None
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
    except _FOUNDATION_AVAILABILITY_ERRORS as exc:
        # The foundation scoring service (or the package retrieval path) is
        # unreachable. Scoring retries on a later pass; the chain phases below
        # are pure PFTL work driven from local state and must still run — a
        # foundation outage must never forfeit a pending commit or reveal.
        score_status = SCORE_STATUS_SCORING_UNAVAILABLE
        score_error = str(exc)
        active_round_id = None
        active_round_number = 0

    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        # Reveals and commits are both driven from local state, so neither
        # depends on a round being scored this pass nor on the chain cursor.
        # Reveals run first so a commit-phase issue never blocks a pending
        # reveal. The announcement walk only records each round's windows and
        # advances the cursor; the commit itself is replayed from local state,
        # so a round scored on a later pass than its announcement is not lost.
        reveals = _advance_reveals(
            config,
            state,
            rpc_client=rpc_client,
            signer=signer,
            publisher=publisher,
        )
        announcements = _record_announcement_windows(
            config,
            client,
            state,
            rpc_client=rpc_client,
            publisher=publisher,
            announcement_decoder=announcement_decoder,
            package_fetcher=package_fetcher,
            round_limit=round_limit,
        )
        protocol_violations = _probe_output_withholding(
            config,
            client,
            state,
            rpc_client=rpc_client,
        )
        commits = _advance_commits(
            config,
            state,
            rpc_client=rpc_client,
            signer=signer,
            publisher=publisher,
        )

    return ParticipateResult(
        network=config.network,
        score_status=score_status,
        round_id=active_round_id or 0,
        round_number=active_round_number,
        commits=commits,
        reveals=reveals,
        announcements=announcements,
        protocol_violations=protocol_violations,
        score_error=score_error,
    )


def _record_announcement_windows(
    config: SidecarConfig,
    client: ScoringClient,
    state: SidecarState,
    *,
    rpc_client: PftlRpcClient,
    publisher: str,
    announcement_decoder: Callable[..., VerifiedAnnouncement | None],
    package_fetcher,
    round_limit: int,
) -> list[dict[str, Any]]:
    """Persist the commit/reveal windows of every announced round in the feed.

    The commit itself is replayed from local state (``_advance_commits``), so the
    cursor can advance past an announcement as soon as its windows are recorded —
    even if the round is not yet scored. A round scored on a later pass still
    commits, because its windows are already persisted, so a transient scoring
    failure no longer forfeits the round. A malformed announcement from the
    trusted sender will not improve, so it is recorded and skipped. A
    foundation-availability failure while content-binding an announcement halts
    the walk without advancing the cursor — the interrupted announcement retries
    on a later pass, and the chain phases of this pass still run. RPC failures
    propagate so the pass retries them.
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
            results.append(
                {
                    "tx_hash": transaction.tx_hash,
                    "status": ADVANCE_STATUS_ANNOUNCEMENT_ERROR,
                    "error": str(exc),
                }
            )
            watcher.advance_cursor(transaction)
            continue
        except _FOUNDATION_AVAILABILITY_ERRORS as exc:
            # The foundation service or package retrieval is unreachable, so the
            # announcement cannot be content-bound this pass. Halt the walk
            # without advancing the cursor — this transaction and everything
            # after it re-surfaces on a later pass — and let the chain phases
            # of this pass proceed.
            results.append(
                {
                    "tx_hash": transaction.tx_hash,
                    "status": ADVANCE_STATUS_FOUNDATION_UNAVAILABLE,
                    "error": str(exc),
                }
            )
            break

        if verified is not None:
            announcement = verified.announcement
            state.record_announcement_windows(
                config.network,
                _metadata_from_package(verified.package),
                commit_opens_at=announcement.commit_opens_at.isoformat(),
                commit_closes_at=announcement.commit_closes_at.isoformat(),
                reveal_opens_at=announcement.reveal_opens_at.isoformat(),
                reveal_closes_at=announcement.reveal_closes_at.isoformat(),
            )
            results.append(
                {
                    "round_number": announcement.round_number,
                    "status": ANNOUNCE_STATUS_WINDOWS_RECORDED,
                }
            )
        # Whether or not it was a round announcement, the cursor advances: the
        # commit no longer depends on re-seeing the announcement.
        watcher.advance_cursor(transaction)
    return results


def _advance_commits(
    config: SidecarConfig,
    state: SidecarState,
    *,
    rpc_client: PftlRpcClient,
    signer: Signer,
    publisher: str,
) -> list[dict[str, Any]]:
    """Commit every scored round whose announced commit window is recorded and
    that has not yet committed.

    State-driven, mirroring ``_advance_reveals``: each round is committed from
    its persisted output fingerprints and announced windows, independent of where
    the chain cursor sits, so a round scored on a later pass than its
    announcement is not forfeited. ``submit_commit`` self-gates on the commit
    window (a no-op before it opens, a closed-window outcome after) and on
    idempotency (local state plus an on-chain check). A per-round ``CommitError``
    isolates that round and is recorded without halting the pass; transient RPC
    failures propagate so the pass retries.
    """

    results: list[dict[str, Any]] = []
    for record in state.get_rounds_pending_commit(config.network):
        # get_rounds_pending_commit already guarantees the three fingerprints;
        # this narrows the type and guards defensively rather than skipping work.
        output_hashes = _committable_output_hashes(record)
        if output_hashes is None:
            continue
        try:
            commit = submit_commit(
                _announcement_from_record(record),
                output_hashes,
                config,
                _metadata_from_record(record),
                rpc_client=rpc_client,
                signer=signer,
                state=state,
                foundation_publisher_address=publisher,
            )
        except (CommitError, commit_reveal.CommitRevealValidationError) as exc:
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
                "round_number": commit.round_number,
                "status": commit.status,
                "tx_hash": commit.commit_tx_hash,
            }
        )
    return results


def _parse_iso_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _probe_output_withholding(
    config: SidecarConfig,
    client: ScoringClient,
    state: SidecarState,
    *,
    rpc_client: PftlRpcClient,
) -> list[dict[str, Any]]:
    """Report early public availability of foundation output hashes."""

    try:
        now = rpc_client.latest_validated_ledger_close_time()
    except PftlRpcError as exc:
        return [{"status": ADVANCE_STATUS_ERROR, "error": str(exc)}]

    results: list[dict[str, Any]] = []
    for record in state.get_rounds_pending_commit(config.network):
        if not (record.commit_opens_at and record.commit_closes_at):
            continue
        try:
            opens = _parse_iso_time(record.commit_opens_at)
            closes = _parse_iso_time(record.commit_closes_at)
        except ValueError as exc:
            results.append(
                {
                    "round_number": record.round_number,
                    "status": ADVANCE_STATUS_ERROR,
                    "error": str(exc),
                }
            )
            continue
        if not (opens <= now < closes):
            continue
        try:
            payload = client.fetch_final_bundle_file(
                record.round_number,
                FOUNDATION_VERIFICATION_HASHES_PATH,
            )
        except ScoringHTTPError as exc:
            if exc.status_code == 404:
                continue
            results.append(
                {
                    "round_number": record.round_number,
                    "status": ADVANCE_STATUS_ERROR,
                    "error": str(exc),
                }
            )
            continue
        except ScoringClientError as exc:
            results.append(
                {
                    "round_number": record.round_number,
                    "status": ADVANCE_STATUS_ERROR,
                    "error": str(exc),
                }
            )
            continue
        if isinstance(payload, dict):
            results.append(
                {
                    "round_number": record.round_number,
                    "status": "protocol_violation",
                    "path": FOUNDATION_VERIFICATION_HASHES_PATH,
                }
            )
    return results


def _announcement_from_record(
    record: RoundStateRecord,
) -> commit_reveal.RoundAnnouncement:
    """Rebuild the round announcement from persisted state, so ``submit_commit``
    is reused unchanged for the state-driven commit."""

    return commit_reveal.build_round_announcement(
        network=record.network,
        round_number=record.round_number,
        input_package_cid=record.input_package_cid,
        input_package_hash=record.input_package_hash,
        commit_opens_at=record.commit_opens_at,
        commit_closes_at=record.commit_closes_at,
        reveal_opens_at=record.reveal_opens_at,
        reveal_closes_at=record.reveal_closes_at,
    )


def _metadata_from_package(package: FetchedInputPackage) -> RoundMetadata:
    return RoundMetadata(
        round_id=package.round_id,
        round_number=package.round_number,
        status=ROUND_STATUS_INPUT_FROZEN,
        input_package_cid=package.input_package_cid,
        input_package_hash=package.input_package_hash,
        input_frozen_at=package.input_frozen_at,
        final_bundle_cid=None,
    )


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


def _resolve_publisher(
    config: SidecarConfig,
    client: ScoringClient,
    state: SidecarState,
) -> str:
    """Effective publisher address: explicit override, fetched config, then the
    locally cached address from an earlier successful discovery.

    A successful discovery is written through to the cache so a later pass can
    run its chain phases while the foundation config endpoint is unreachable.
    The cache backstops only a fetch *failure*: a config endpoint that answers
    without a publisher address is a foundation-side contract change, and
    silently reusing a cached address could send memos to a retired account, so
    that case stays strict.
    """

    if config.foundation_publisher_address:
        return config.foundation_publisher_address
    try:
        foundation_config = FoundationConfig.from_api_payload(client.fetch_config())
    except ScoringClientError as exc:
        cached = state.get_cached_publisher_address(config.network)
        if cached:
            return cached
        raise ParticipationConfigError(
            "could not fetch foundation config for publisher discovery and no "
            f"previously discovered address is cached: {exc}"
        ) from exc
    try:
        publisher = resolve_foundation_publisher_address(config, foundation_config)
    except ChainWatcherError as exc:
        raise ParticipationConfigError(str(exc)) from exc
    state.cache_publisher_address(config.network, publisher)
    return publisher


def _probe_rpc(config: SidecarConfig, rpc_client: PftlRpcClient) -> None:
    try:
        rpc_client.latest_validated_ledger_close_time()
    except PftlRpcError as exc:
        raise ParticipationConfigError(
            f"PFTL RPC at {config.pftl_rpc_url} is not reachable: {exc}"
        ) from exc
