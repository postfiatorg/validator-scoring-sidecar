"""End-to-end round scoring: verify a round and record the outcome.

Drives one scoring round through the pieces built in earlier milestones —
verified-input fetch, the manifest-compatibility gate, the configured inference
backend, output fingerprinting, and foundation comparison — and persists a
classified outcome to local SQLite state.

Two paths handle the timing reality that the foundation publishes its
fingerprints only in its final bundle, produced after the sidecar scores from
frozen inputs:

- Full score: fetch/verify the input, check the manifest against the local
  deployment record, run the backend, fingerprint, and compare if the
  foundation's hashes are already available; otherwise record the round as
  ``SCORED`` with the comparison pending.
- Deferred comparison: a round already ``SCORED`` with a pending comparison is
  completed from its persisted hashes once the final bundle exists, without
  re-running inference.

A full score may also provision its own runtime: when the caller supplies a
``runtime_provisioner`` (the participate loop, Modal mode), a missing or
manifest-stale Modal deployment record is replaced by a fresh deployment before
inference — see ``_resolve_runtime`` for the gating rules.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.deployment import (
    DEPLOYMENT_MODE_LOCAL,
    DEPLOYMENT_MODE_MODAL,
    DeploymentError,
    ManifestRuntimeError,
    build_deployment_record,
    deployment_record_path,
    extract_runtime_spec,
    load_round_manifest,
    select_latest_deployable_round,
)
from validator_scoring_sidecar.failure import Failure, FailureCategory
from validator_scoring_sidecar.inference import (
    FAILURE_REASON_CONFIGURATION,
    FAILURE_REASON_KEY,
    InferenceBackend,
    InferenceConfigError,
    InferenceError,
    LocalSglangBackend,
    ModalBackend,
    ModelRequestError,
    load_model_request,
)
from validator_scoring_sidecar.input_package import SOURCE_AUTO, fetch_input_package
from validator_scoring_sidecar.manifest import check_compatibility, selector_parameters
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.scoring_client import ScoringClient, ScoringClientError
from validator_scoring_sidecar.state import (
    SCORED_OR_FURTHER_STATES,
    STATE_SCORED,
    STATE_SCORING_FAILED,
    STATE_SKIPPED,
    RoundStateRecord,
    ScoreOutcome,
    SidecarState,
    SidecarStateError,
)
from validator_scoring_sidecar.sync import (
    DEFAULT_SYNC_ROUND_LIMIT,
    SidecarLock,
    SyncLockError,
)
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_VALIDATOR_SCORES,
    HASH_SELECTED_UNL,
    compare_hashes,
    load_previous_unl,
    load_validator_map,
    persist_verification_hashes,
    read_verification_hashes,
    verify_round,
)

FOUNDATION_VERIFICATION_HASHES_PATH = "outputs/verification_hashes.json"

# A runtime provisioner deploys the manifest-pinned Modal endpoint and returns
# the resulting deployment record. Supplied by the participate loop when the
# operator has configured Modal account credentials; never used to replace a
# local-mode record.
RuntimeProvisioner = Callable[[dict[str, Any]], dict[str, Any]]

# Placeholder values for the provisioning pre-check record; neither field is
# read by the compatibility checks the pre-check exists to evaluate.
_PRECHECK_ENDPOINT_URL = "https://provisioning-precheck.invalid"
_PRECHECK_DEPLOYED_AT = "1970-01-01T00:00:00+00:00"

SCORE_STATUS_SCORED = "scored"
SCORE_STATUS_DIVERGENT = "divergent"
SCORE_STATUS_COMPARISON_PENDING = "comparison_pending"
SCORE_STATUS_ALREADY_SCORED = "already_scored"
SCORE_STATUS_SCORING_FAILED = "scoring_failed"
SCORE_STATUS_SKIPPED = "skipped"

_SKIP_CATEGORIES = frozenset(
    {
        FailureCategory.SKIPPED_OVERRIDE,
        FailureCategory.SKIPPED_OPERATOR_OPT_OUT,
    }
)

INFERENCE_DEADLINE_SAFETY_MARGIN_SECONDS = 20.0
MIN_INFERENCE_READ_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of one ``score`` invocation for a single round."""

    status: str
    network: str
    round_id: int
    round_number: int
    sidecar_state: str
    backend_mode: str | None
    compared: bool
    matched_levels: list[str]
    error_category: str | None
    error_details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "network": self.network,
            "round_id": self.round_id,
            "round_number": self.round_number,
            "sidecar_state": self.sidecar_state,
            "backend_mode": self.backend_mode,
            "compared": self.compared,
            "matched_levels": list(self.matched_levels),
            "error_category": self.error_category,
            "error_details": (
                dict(self.error_details) if self.error_details is not None else None
            ),
        }


def score_round(
    config: SidecarConfig,
    client: ScoringClient,
    *,
    round_id: int | None = None,
    source: str = SOURCE_AUTO,
    round_limit: int = DEFAULT_SYNC_ROUND_LIMIT,
    backend_factory=None,
    foundation_hash_fetcher=None,
    package_fetcher=fetch_input_package,
    runtime_provisioner: RuntimeProvisioner | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> ScoreResult:
    """Score one round and persist the outcome."""

    factory = backend_factory
    fetch_foundation = foundation_hash_fetcher or _fetch_foundation_hashes
    now = now_fn or _utcnow
    metadata = _resolve_round(client, round_id, round_limit)
    deferred_existing: RoundStateRecord | None = None
    inference_deadline: datetime | None = None
    pending_call_id: str | None = None

    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        existing = state.get_round(config.network, metadata.round_id)
        inference_deadline = _inference_deadline(existing)
        # A persisted in-flight Modal call is resumable only for the same
        # frozen inputs; anything else must submit fresh.
        if existing is not None and existing.matches_frozen_input(metadata):
            pending_call_id = existing.inference_call_id

        if (
            existing is not None
            and existing.sidecar_state in SCORED_OR_FURTHER_STATES
            and existing.matches_frozen_input(metadata)
        ):
            if existing.comparison_levels_matched is not None:
                return ScoreResult(
                    status=SCORE_STATUS_ALREADY_SCORED,
                    network=config.network,
                    round_id=metadata.round_id,
                    round_number=metadata.round_number,
                    sidecar_state=existing.sidecar_state,
                    backend_mode=existing.backend_mode,
                    compared=True,
                    matched_levels=_split_levels(existing.comparison_levels_matched),
                    error_category=existing.error_category,
                    error_details=_parse_persisted_details(existing.error_details),
                )
            deferred_existing = existing

    if deferred_existing is not None:
        deferred = _attempt_deferred_comparison(
            config, client, metadata, deferred_existing, fetch_foundation
        )
        if deferred is not None:
            return deferred
        # Persisted hashes missing despite SCORED state — fall back to a full
        # re-score below.

    return _full_score(
        config,
        client,
        metadata,
        source=source,
        package_fetcher=package_fetcher,
        factory=factory,
        fetch_foundation=fetch_foundation,
        runtime_provisioner=runtime_provisioner,
        inference_deadline=inference_deadline,
        pending_call_id=pending_call_id,
        now_fn=now,
    )


def _resolve_round(
    client: ScoringClient,
    round_id: int | None,
    round_limit: int,
) -> RoundMetadata:
    if round_id is not None:
        payload = client.fetch_round(round_id)
        return RoundMetadata.from_api_payload(payload, requested_round_id=round_id)
    return select_latest_deployable_round(client.fetch_rounds(limit=round_limit))


def _attempt_deferred_comparison(
    config: SidecarConfig,
    client: ScoringClient,
    metadata: RoundMetadata,
    existing: RoundStateRecord,
    fetch_foundation,
) -> ScoreResult | None:
    persisted = read_verification_hashes(config, metadata.input_package_hash)
    if persisted is None:
        return None

    foundation = fetch_foundation(client, metadata, config)
    if foundation is None:
        return _pending_result(
            config.network, metadata, existing.backend_mode, existing.sidecar_state
        )

    verification = compare_hashes(metadata.input_package_hash, persisted, foundation)
    # Preserve the round's lifecycle state: a deferred foundation comparison is an
    # orthogonal annotation and must not downgrade a COMMITTED/REVEALED round.
    outcome = _scored_outcome(
        existing.backend_mode,
        persisted,
        verification,
        sidecar_state=existing.sidecar_state,
    )
    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        state.record_score(config.network, metadata, outcome)
    return _scored_result(config.network, metadata, outcome, verification.compared)


def _full_score(
    config: SidecarConfig,
    client: ScoringClient,
    metadata: RoundMetadata,
    *,
    source: str,
    package_fetcher,
    factory: Callable[[dict[str, Any]], InferenceBackend] | None,
    fetch_foundation,
    runtime_provisioner: RuntimeProvisioner | None = None,
    inference_deadline: datetime | None = None,
    pending_call_id: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> ScoreResult:
    now = now_fn or _utcnow
    fetched = package_fetcher(metadata, config, client, source=source, force=False)
    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        state.record_input_verified(config.network, metadata, fetched)

    manifest = load_round_manifest(fetched.local_path)
    deployment_record, compat = _resolve_runtime(
        config, manifest, metadata, runtime_provisioner
    )
    if not compat.passed:
        outcome = _outcome_from_compat_failure(compat.failure)
        with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
            state.record_score(config.network, metadata, outcome)
        return _outcome_result(config.network, metadata, outcome)

    backend: InferenceBackend | None = None
    try:
        model_request = load_model_request(fetched.local_path)
        timeout_seconds = _effective_inference_timeout_seconds(
            inference_deadline, now, config.inference_timeout_seconds
        )
        if timeout_seconds is None:
            return _record_failure(
                config,
                metadata,
                compat.effective_mode,
                FailureCategory.INFERENCE_TIMEOUT,
                {
                    FAILURE_REASON_KEY: "round_deadline_elapsed",
                    "deadline": (
                        inference_deadline.isoformat()
                        if inference_deadline is not None
                        else None
                    ),
                },
            )

        def persist_call_id(call_id: str) -> None:
            # Best-effort durability: a failed write must not kill the
            # in-flight generation it was meant to make resumable.
            try:
                with SidecarLock(config.data_dir), SidecarState(
                    config.data_dir
                ) as state:
                    state.record_inference_call(
                        config.network, metadata, inference_call_id=call_id
                    )
            except (SyncLockError, SidecarStateError):
                pass

        backend = _build_backend(
            factory,
            deployment_record,
            timeout_seconds,
            pending_call_id=pending_call_id,
            on_call_submitted=persist_call_id,
        )
        inference = backend.run(model_request)
    except ModelRequestError as exc:
        return _record_failure(
            config,
            metadata,
            compat.effective_mode,
            FailureCategory.INFERENCE_ERROR,
            {"message": str(exc)},
        )
    except InferenceConfigError as exc:
        return _record_failure(
            config,
            metadata,
            compat.effective_mode,
            FailureCategory.RUNTIME_UNAVAILABLE,
            {"message": str(exc), FAILURE_REASON_KEY: FAILURE_REASON_CONFIGURATION},
        )
    except InferenceError as exc:
        # The failure message is the operator's diagnosis (endpoint, underlying
        # transport error); persist it with the structured details.
        return _record_failure(
            config,
            metadata,
            compat.effective_mode,
            exc.category,
            {"message": str(exc), **exc.failure.details},
        )
    finally:
        if backend is not None:
            backend.close()

    validator_map = load_validator_map(fetched.local_path)
    # The selected_unl level is reproducible only when the foundation froze the
    # previous UNL into the package. Older packages lack it, so fall back to
    # the model-response and validator-scores levels.
    previous_unl = None
    selector_params = None
    if (fetched.local_path / "inputs" / "previous_unl.json").exists():
        previous_unl = load_previous_unl(fetched.local_path)
        selector_params = selector_parameters(manifest)
    foundation = fetch_foundation(client, metadata, config)
    verification = verify_round(
        inference.content,
        validator_map,
        input_package_hash=metadata.input_package_hash,
        foundation_hashes=foundation,
        previous_unl=previous_unl,
        selector_parameters=selector_params,
    )
    persist_verification_hashes(config, metadata.input_package_hash, verification.hashes)
    outcome = _scored_outcome(compat.effective_mode, verification.hashes, verification)
    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        state.record_score(config.network, metadata, outcome)
    return _scored_result(config.network, metadata, outcome, verification.compared)


def _inference_deadline(existing: RoundStateRecord | None) -> datetime | None:
    """The round's commit-window close, known only from the on-chain
    announcement windows persisted in local state."""

    if existing is None:
        return None
    return _parse_datetime(existing.commit_closes_at)


def _effective_inference_timeout_seconds(
    deadline: datetime | None,
    now_fn: Callable[[], datetime],
    max_timeout_seconds: float,
) -> float | None:
    """Resolve the read timeout for one inference request.

    ``max_timeout_seconds`` is the operator-configured upper bound. The
    commit-deadline cap always takes precedence: when a round's commit window is
    close, the timeout is shortened to what remains (minus a safety margin), and
    scoring is skipped entirely (``None``) when too little time is left.
    """
    if deadline is None:
        return max_timeout_seconds

    remaining = (
        deadline - now_fn()
    ).total_seconds() - INFERENCE_DEADLINE_SAFETY_MARGIN_SECONDS
    if remaining < MIN_INFERENCE_READ_TIMEOUT_SECONDS:
        return None
    return min(max_timeout_seconds, remaining)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _record_failure(
    config: SidecarConfig,
    metadata: RoundMetadata,
    backend_mode: str | None,
    category: FailureCategory,
    details: dict[str, Any] | None,
) -> ScoreResult:
    outcome = ScoreOutcome(
        sidecar_state=STATE_SCORING_FAILED,
        backend_mode=backend_mode,
        error_category=category.value,
        error_details=details,
    )
    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        state.record_score(config.network, metadata, outcome)
    return _outcome_result(config.network, metadata, outcome)


def _build_backend(
    factory: Callable[[dict[str, Any]], InferenceBackend] | None,
    deployment_record: dict[str, Any],
    timeout_seconds: float,
    *,
    pending_call_id: str | None = None,
    on_call_submitted: Callable[[str], None] | None = None,
) -> InferenceBackend:
    if factory is not None:
        return factory(deployment_record)
    return _default_backend_factory(
        deployment_record,
        timeout_seconds=timeout_seconds,
        pending_call_id=pending_call_id,
        on_call_submitted=on_call_submitted,
    )


def _default_backend_factory(
    deployment_record: dict[str, Any],
    *,
    timeout_seconds: float,
    pending_call_id: str | None = None,
    on_call_submitted: Callable[[str], None] | None = None,
) -> InferenceBackend:
    mode = deployment_record.get("mode")
    endpoint_url = deployment_record.get("endpoint_url")
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise DeploymentError(
            "deployment record is missing a valid endpoint_url; redeploy"
        )
    if mode == DEPLOYMENT_MODE_MODAL:
        return ModalBackend.from_environment(
            endpoint_url,
            timeout_seconds=timeout_seconds,
            submit_url=_optional_record_url(deployment_record, "submit_url"),
            result_url=_optional_record_url(deployment_record, "result_url"),
            pending_call_id=pending_call_id,
            on_call_submitted=on_call_submitted,
        )
    if mode == DEPLOYMENT_MODE_LOCAL:
        return LocalSglangBackend.from_environment(
            endpoint_url, timeout_seconds=timeout_seconds
        )
    raise DeploymentError(
        f"deployment record mode {mode!r} is not a supported inference backend"
    )


def _optional_record_url(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _fetch_foundation_hashes(
    client: ScoringClient,
    metadata: RoundMetadata,
    config: SidecarConfig,
) -> dict[str, Any] | None:
    try:
        payload = client.fetch_final_bundle_file(
            metadata.round_number, FOUNDATION_VERIFICATION_HASHES_PATH
        )
        if isinstance(payload, dict):
            return payload
    except ScoringClientError:
        pass
    if metadata.final_bundle_cid and config.ipfs_gateway_url:
        try:
            payload = client.fetch_ipfs_package_file(
                config.ipfs_gateway_url,
                metadata.final_bundle_cid,
                FOUNDATION_VERIFICATION_HASHES_PATH,
            )
            if isinstance(payload, dict):
                return payload
        except ScoringClientError:
            pass
    return None


def provision_runtime_if_needed(
    config: SidecarConfig,
    manifest: dict[str, Any],
    metadata: RoundMetadata,
    provisioner: RuntimeProvisioner | None,
) -> dict[str, Any]:
    """Resolve — and, if needed, deploy — the inference runtime, returning its
    deployment record.

    A thin public wrapper over the score path's ``_resolve_runtime`` decision so
    startup warm-up reuses the exact same rules the participate loop uses: the
    provisioner deploys only when the recorded runtime is missing or
    manifest-stale, a valid current record is reused unchanged, and a local-mode
    record is never replaced. The returned dict is empty only when no deployment
    record exists and the round itself is not deployable (e.g. a dry-run or
    wrong-network round); an existing record is always returned as-is.
    """

    return _resolve_runtime(config, manifest, metadata, provisioner)[0]


def _resolve_runtime(
    config: SidecarConfig,
    manifest: dict[str, Any],
    metadata: RoundMetadata,
    provisioner: RuntimeProvisioner | None,
) -> tuple[dict[str, Any], Any]:
    """Return the deployment record to score with and its compatibility result.

    With no provisioner this is the manual contract: the record must exist and
    is checked as-is. With a provisioner (the participate loop, Modal mode),
    a missing record or a stale Modal record is replaced by a fresh deployment —
    but only when the pre-check proves a fresh deployment would actually pass,
    so unfixable failures (unsupported schema, vendored-code drift, dry-run
    rounds) never trigger a deploy or a deploy loop. A local-mode record is
    never replaced: the sidecar does not manage hardware it does not own.
    """

    try:
        record = _load_deployment_record(config)
    except DeploymentError:
        record = None

    compat = None
    if record is not None:
        compat = check_compatibility(
            manifest,
            record,
            sidecar_network=config.network,
            expected_round_number=metadata.round_number,
        )
        if compat.passed:
            return record, compat

    if provisioner is not None and (
        record is None or record.get("mode") == DEPLOYMENT_MODE_MODAL
    ):
        precheck = _fresh_modal_compatibility(config, manifest, metadata)
        if precheck is not None and precheck.passed:
            new_record = provisioner(manifest)
            return new_record, check_compatibility(
                manifest,
                new_record,
                sidecar_network=config.network,
                expected_round_number=metadata.round_number,
            )
        if record is None and precheck is not None:
            # The round itself is unscoreable (dry-run, wrong network, …);
            # record that verdict instead of demanding a deployment record.
            return {}, precheck

    if record is None or compat is None:
        raise DeploymentError(
            f"no deployment record found at {deployment_record_path(config)}; "
            "run `deploy-modal` or `start-sglang` first"
        )
    return record, compat


def _fresh_modal_compatibility(
    config: SidecarConfig,
    manifest: dict[str, Any],
    metadata: RoundMetadata,
):
    """Compatibility a fresh Modal deployment from this manifest would get.

    Builds the record such a deployment would write and runs the real checker
    against it, so only record-independent checks can fail. Returns ``None``
    when the manifest does not describe a deployable runtime at all.
    """

    try:
        spec = extract_runtime_spec(manifest)
    except ManifestRuntimeError:
        return None
    hypothetical = build_deployment_record(
        spec,
        mode=DEPLOYMENT_MODE_MODAL,
        endpoint_url=_PRECHECK_ENDPOINT_URL,
        deployed_at=_PRECHECK_DEPLOYED_AT,
    )
    return check_compatibility(
        manifest,
        hypothetical.as_dict(),
        sidecar_network=config.network,
        expected_round_number=metadata.round_number,
    )


def _load_deployment_record(config: SidecarConfig) -> dict[str, Any]:
    target = deployment_record_path(config)
    try:
        content = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeploymentError(
            f"no deployment record found at {target}; run `deploy-modal` or "
            "`start-sglang` first"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DeploymentError(
            f"deployment record is not valid JSON: {target}"
        ) from exc
    if not isinstance(content, dict):
        raise DeploymentError(
            f"deployment record must be a JSON object: {target}"
        )
    return content


def _outcome_from_compat_failure(failure: Failure | None) -> ScoreOutcome:
    category = failure.category if failure is not None else FailureCategory.MANIFEST_INCOMPATIBLE
    state = STATE_SKIPPED if category in _SKIP_CATEGORIES else STATE_SCORING_FAILED
    details: dict[str, Any] = {}
    if failure is not None:
        if failure.field is not None:
            details["field"] = failure.field
        if failure.message is not None:
            details["message"] = failure.message
        details.update(failure.details)
    return ScoreOutcome(
        sidecar_state=state,
        error_category=category.value,
        error_details=details or None,
    )


def _scored_outcome(
    backend_mode: str | None,
    hashes: dict[str, str],
    verification,
    *,
    sidecar_state: str = STATE_SCORED,
) -> ScoreOutcome:
    return ScoreOutcome(
        sidecar_state=sidecar_state,
        backend_mode=backend_mode,
        model_response_hash=hashes.get(HASH_MODEL_RESPONSE),
        validator_scores_hash=hashes.get(HASH_VALIDATOR_SCORES),
        selected_unl_hash=hashes.get(HASH_SELECTED_UNL),
        comparison_levels_matched=(
            verification.matched_levels if verification.compared else None
        ),
        error_category=(
            verification.failure.category.value
            if verification.failure is not None
            else None
        ),
        error_details=(
            verification.failure.details if verification.failure is not None else None
        ),
    )


def _scored_result(
    network: str,
    metadata: RoundMetadata,
    outcome: ScoreOutcome,
    compared: bool,
) -> ScoreResult:
    if not compared:
        status = SCORE_STATUS_COMPARISON_PENDING
    elif outcome.error_category == FailureCategory.OUTPUT_DIVERGENCE.value:
        status = SCORE_STATUS_DIVERGENT
    else:
        status = SCORE_STATUS_SCORED
    return ScoreResult(
        status=status,
        network=network,
        round_id=metadata.round_id,
        round_number=metadata.round_number,
        sidecar_state=outcome.sidecar_state,
        backend_mode=outcome.backend_mode,
        compared=compared,
        matched_levels=list(outcome.comparison_levels_matched or []),
        error_category=outcome.error_category,
        error_details=outcome.error_details,
    )


def _outcome_result(
    network: str,
    metadata: RoundMetadata,
    outcome: ScoreOutcome,
) -> ScoreResult:
    status = (
        SCORE_STATUS_SKIPPED
        if outcome.sidecar_state == STATE_SKIPPED
        else SCORE_STATUS_SCORING_FAILED
    )
    return ScoreResult(
        status=status,
        network=network,
        round_id=metadata.round_id,
        round_number=metadata.round_number,
        sidecar_state=outcome.sidecar_state,
        backend_mode=outcome.backend_mode,
        compared=False,
        matched_levels=[],
        error_category=outcome.error_category,
        error_details=outcome.error_details,
    )


def _pending_result(
    network: str,
    metadata: RoundMetadata,
    backend_mode: str | None,
    sidecar_state: str = STATE_SCORED,
) -> ScoreResult:
    return ScoreResult(
        status=SCORE_STATUS_COMPARISON_PENDING,
        network=network,
        round_id=metadata.round_id,
        round_number=metadata.round_number,
        sidecar_state=sidecar_state,
        backend_mode=backend_mode,
        compared=False,
        matched_levels=[],
        error_category=None,
    )


def _split_levels(value: str | None) -> list[str]:
    if not value:
        return []
    return value.split(",")


def _parse_persisted_details(value: str | None) -> dict[str, Any] | None:
    """Decode error details persisted as JSON in the round state, or ``None``."""

    if not value:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
