"""Manifest compatibility checker — the gate before sidecar scoring.

Compares the round's ``runtime/execution_manifest.json`` (from the verified
input package cache, produced by the foundation scoring service) against the
sidecar's local ``deployment_record.json`` (produced by the sidecar's deploy
helpers when an operator provisions their inference backend). Decides whether
the sidecar's locally deployed inference backend is close enough to what the
foundation ran that the resulting scoring outputs can be honestly compared.

The contract for which fields must match, which are tolerant, and which are
ignored or conditionally ignored lives in
``docs/phase2/SidecarScoringSpec.md`` in the foundation repository. This
module is the implementation of that contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from validator_scoring_sidecar.failure import Failure, FailureCategory
from validator_scoring_sidecar.scoring import (
    SUPPORTED_PARSER_CONTENT_HASHES,
    SUPPORTED_SCORE_FORMULA_CONTENT_HASHES,
    SUPPORTED_SELECTOR_CONTENT_HASHES,
)

SUPPORTED_MANIFEST_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
DETERMINISTIC_INFERENCE_FLAG = "--enable-deterministic-inference"
HF_COMMIT_HASH_LENGTH = 40
HEX_DIGITS = frozenset("0123456789abcdef")

EXPECTED_MODEL_PROVIDER = "huggingface"
EXPECTED_REQUEST_TYPE = "openai_chat_completions"
EXPECTED_REQUEST_METHOD = "chat.completions.create"
EXPECTED_REQUEST_TEMPERATURE = 0
EXPECTED_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}
EXPECTED_CANONICAL_HASH_ALGORITHM = "sha256"
EXPECTED_CANONICAL_TEXT_ENCODING = "utf-8"
EXPECTED_CANONICAL_SEPARATORS = [",", ":"]

BackendMode = Literal["modal", "local"]
EffectiveMode = Literal["modal", "local", "local_unverified"]

# The sidecar's own runtime kind per mode. The manifest always carries the
# foundation's kind (modal_sglang); the sidecar reproduces the same engine on
# Modal (modal_sglang) or locally (local_sglang). Compatibility is matched on
# the engine, so the host placement (modal vs local) is allowed to differ.
_MODE_RUNTIME_KIND: dict[str, str] = {
    "modal": "modal_sglang",
    "local": "local_sglang",
}
SUPPORTED_RUNTIME_ENGINE = "sglang"


@dataclass(frozen=True)
class CompatibilityResult:
    """Outcome of a manifest compatibility check.

    When ``passed`` is true, ``effective_mode`` reports the backend mode the
    sidecar should stamp on the run (which may differ from the operator's
    selected mode if a downgrade applied). When ``passed`` is false,
    ``failure`` carries the structured reason.
    """

    passed: bool
    failure: Failure | None = None
    effective_mode: EffectiveMode | None = None


def check_compatibility(
    manifest: dict[str, Any],
    deployment_record: dict[str, Any],
    *,
    sidecar_network: str,
    expected_round_number: int,
) -> CompatibilityResult:
    """Compare the round's execution manifest against the local deployment record.

    Args:
        manifest: Parsed ``runtime/execution_manifest.json`` from the verified
            input package cache.
        deployment_record: Parsed ``deployment_record.json`` from the
            sidecar's local runtime state.
        sidecar_network: The network this sidecar instance is configured for
            (``testnet`` or ``devnet``). Must match the round's
            ``round.network``.
        expected_round_number: The round the sidecar intends to score, taken
            from the scoring-service round metadata the caller already has.
            Must match ``round.round_number`` in the manifest.

    Returns:
        A ``CompatibilityResult`` describing pass or failure. Pass results
        carry the ``effective_mode`` to stamp on the run.
    """
    schema_failure = _check_schema_version(manifest)
    if schema_failure is not None:
        return CompatibilityResult(passed=False, failure=schema_failure)

    round_section = manifest.get("round", {})
    round_kind = round_section.get("kind")

    if round_kind == "override":
        return CompatibilityResult(
            passed=False,
            failure=Failure(category=FailureCategory.SKIPPED_OVERRIDE),
        )
    if round_kind == "dry_run":
        return CompatibilityResult(
            passed=False,
            failure=Failure(
                category=FailureCategory.SKIPPED_OPERATOR_OPT_OUT,
                field="round.kind",
                message="dry_run rounds are not scored by the sidecar",
                details={"reason": "dry_run"},
            ),
        )
    if round_kind != "normal":
        return CompatibilityResult(
            passed=False,
            failure=Failure(
                category=FailureCategory.MANIFEST_INCOMPATIBLE,
                field="round.kind",
                message=f"unrecognized round.kind {round_kind!r}",
            ),
        )

    deployment_mode = deployment_record.get("mode")
    if deployment_mode not in _MODE_RUNTIME_KIND:
        return CompatibilityResult(
            passed=False,
            failure=Failure(
                category=FailureCategory.MANIFEST_INCOMPATIBLE,
                field="deployment_record.mode",
                message=(
                    f"deployment record mode must be 'modal' or 'local', "
                    f"got {deployment_mode!r}"
                ),
            ),
        )

    round_failure = _check_round(
        manifest,
        sidecar_network=sidecar_network,
        expected_round_number=expected_round_number,
    )
    if round_failure is not None:
        return CompatibilityResult(passed=False, failure=round_failure)

    for check in (
        _check_model,
        _check_runtime,
        _check_request,
        _check_code,
        _check_canonicalization,
    ):
        failure = check(manifest, deployment_record)
        if failure is not None:
            return CompatibilityResult(passed=False, failure=failure)

    effective_mode = _resolve_effective_mode(deployment_mode, deployment_record)
    return CompatibilityResult(passed=True, effective_mode=effective_mode)


def _check_schema_version(manifest: dict[str, Any]) -> Failure | None:
    schema = manifest.get("schema_version")
    if schema not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        return Failure(
            category=FailureCategory.MANIFEST_UNSUPPORTED,
            field="schema_version",
            message=(
                f"manifest schema_version {schema!r} is not in supported set "
                f"{sorted(SUPPORTED_MANIFEST_SCHEMA_VERSIONS)}"
            ),
        )
    return None


def _check_round(
    manifest: dict[str, Any],
    *,
    sidecar_network: str,
    expected_round_number: int,
) -> Failure | None:
    round_section = manifest.get("round", {})

    if round_section.get("network") != sidecar_network:
        return _incompatible(
            "round.network",
            (
                f"manifest round.network {round_section.get('network')!r} does "
                f"not match sidecar's configured network {sidecar_network!r}"
            ),
        )
    if round_section.get("inference_performed") is not True:
        return _incompatible(
            "round.inference_performed",
            "normal rounds must declare inference_performed=true",
        )

    round_number = round_section.get("round_number")
    if not isinstance(round_number, int):
        return _incompatible(
            "round.round_number",
            "round.round_number must be present as an integer",
        )
    if round_number != expected_round_number:
        return _incompatible(
            "round.round_number",
            (
                f"manifest round.round_number {round_number} does not match "
                f"expected round {expected_round_number}"
            ),
        )
    return None


def _check_model(
    manifest: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    model_section = manifest.get("model", {})
    deployment_revision = deployment_record.get("model_revision")
    deployment_served = deployment_record.get("served_model_name")

    if model_section.get("provider") != EXPECTED_MODEL_PROVIDER:
        return _incompatible(
            "model.provider",
            (
                f"only {EXPECTED_MODEL_PROVIDER!r} models are supported, got "
                f"{model_section.get('provider')!r}"
            ),
        )
    if model_section.get("repo_id") != deployment_served:
        return _incompatible(
            "model.repo_id",
            (
                f"manifest model.repo_id {model_section.get('repo_id')!r} does "
                f"not match deployed served model {deployment_served!r}"
            ),
        )
    if model_section.get("served_name") != deployment_served:
        return _incompatible(
            "model.served_name",
            (
                f"manifest model.served_name {model_section.get('served_name')!r} "
                f"does not match deployed served model {deployment_served!r}"
            ),
        )

    revision = model_section.get("revision")
    if not _is_full_commit_hash(revision):
        return _incompatible(
            "model.revision",
            (
                f"manifest model.revision must be a full 40-character commit "
                f"hash, got {revision!r}"
            ),
        )
    if revision != deployment_revision:
        return _incompatible(
            "model.revision",
            (
                f"manifest model.revision {revision!r} does not match deployed "
                f"model revision {deployment_revision!r}"
            ),
        )
    return None


def _check_runtime(
    manifest: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    runtime_section = manifest.get("runtime", {})
    deployment_mode = deployment_record["mode"]
    sidecar_kind = _MODE_RUNTIME_KIND[deployment_mode]

    manifest_kind = runtime_section.get("kind")
    if _runtime_engine(manifest_kind) != SUPPORTED_RUNTIME_ENGINE:
        return _incompatible(
            "runtime.kind",
            (
                f"manifest runtime.kind {manifest_kind!r} is not compatible with "
                f"the sidecar's {sidecar_kind!r} backend; only the "
                f"{SUPPORTED_RUNTIME_ENGINE!r} engine is supported, with Modal or "
                f"local hosting allowed to differ"
            ),
        )

    manifest_image = runtime_section.get("image")
    deployment_image = deployment_record.get("image")
    if not _has_image_digest(manifest_image):
        return _incompatible(
            "runtime.image",
            "manifest runtime.image must include an immutable @sha256: digest",
        )
    if manifest_image != deployment_image:
        return _incompatible(
            "runtime.image",
            (
                f"manifest runtime.image {manifest_image!r} does not match "
                f"deployed image {deployment_image!r}"
            ),
        )

    if runtime_section.get("tensor_parallelism") != deployment_record.get(
        "tensor_parallelism"
    ):
        return _incompatible(
            "runtime.tensor_parallelism",
            (
                f"manifest runtime.tensor_parallelism "
                f"{runtime_section.get('tensor_parallelism')!r} does not match "
                f"deployed value {deployment_record.get('tensor_parallelism')!r}"
            ),
        )

    gpu_failure = _check_gpu(runtime_section, deployment_record)
    if gpu_failure is not None:
        return gpu_failure

    launch_failure = _check_launch_args(runtime_section, deployment_record)
    if launch_failure is not None:
        return launch_failure

    return _check_runtime_environment(runtime_section, deployment_record)


def _check_gpu(
    runtime_section: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    manifest_gpu = runtime_section.get("gpu")
    deployment_gpu = deployment_record.get("gpu_class")
    if manifest_gpu == deployment_gpu:
        return None
    if (
        deployment_record["mode"] == "local"
        and deployment_record.get("gpu_mismatch_acknowledged") is True
    ):
        return None
    return _incompatible(
        "runtime.gpu",
        (
            f"manifest runtime.gpu {manifest_gpu!r} does not match deployed "
            f"gpu_class {deployment_gpu!r} (override available only in local "
            f"mode via --allow-gpu-mismatch)"
        ),
    )


def _check_launch_args(
    runtime_section: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    manifest_args = runtime_section.get("launch_args")
    deployment_args = deployment_record.get("launch_args")
    if not isinstance(manifest_args, list) or not isinstance(deployment_args, list):
        return _incompatible(
            "runtime.launch_args",
            "launch_args must be present as a list of strings on both sides",
        )

    manifest_pairs, manifest_flags = _parse_launch_args(manifest_args)
    deployment_pairs, deployment_flags = _parse_launch_args(deployment_args)

    if DETERMINISTIC_INFERENCE_FLAG not in manifest_flags:
        return _incompatible(
            "runtime.launch_args",
            (
                f"manifest runtime.launch_args must include "
                f"{DETERMINISTIC_INFERENCE_FLAG}"
            ),
        )
    if DETERMINISTIC_INFERENCE_FLAG not in deployment_flags:
        return _incompatible(
            "runtime.launch_args",
            (
                f"deployed launch_args must include "
                f"{DETERMINISTIC_INFERENCE_FLAG}"
            ),
        )
    if manifest_pairs != deployment_pairs or manifest_flags != deployment_flags:
        return _incompatible(
            "runtime.launch_args",
            (
                "launch_args differ between manifest and deployment record "
                "(order-independent comparison)"
            ),
        )
    return None


def _check_runtime_environment(
    runtime_section: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    manifest_env = runtime_section.get("environment", {})
    deployment_env = deployment_record.get("environment", {})
    key = "SGLANG_FLASHINFER_WORKSPACE_SIZE"
    if manifest_env.get(key) != deployment_env.get(key):
        return _incompatible(
            f"runtime.environment.{key}",
            (
                f"manifest {key}={manifest_env.get(key)!r} does not match "
                f"deployed value {deployment_env.get(key)!r}"
            ),
        )
    return None


def _check_request(
    manifest: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    request_section = manifest.get("request", {})
    deployment_served = deployment_record.get("served_model_name")

    if request_section.get("type") != EXPECTED_REQUEST_TYPE:
        return _incompatible(
            "request.type",
            (
                f"manifest request.type {request_section.get('type')!r} must "
                f"equal {EXPECTED_REQUEST_TYPE!r}"
            ),
        )
    if request_section.get("method") != EXPECTED_REQUEST_METHOD:
        return _incompatible(
            "request.method",
            (
                f"manifest request.method {request_section.get('method')!r} "
                f"must equal {EXPECTED_REQUEST_METHOD!r}"
            ),
        )
    if request_section.get("model") != deployment_served:
        return _incompatible(
            "request.model",
            (
                f"manifest request.model {request_section.get('model')!r} does "
                f"not match deployed served_model_name {deployment_served!r}"
            ),
        )
    if request_section.get("temperature") != EXPECTED_REQUEST_TEMPERATURE:
        return _incompatible(
            "request.temperature",
            (
                f"deterministic scoring requires "
                f"request.temperature={EXPECTED_REQUEST_TEMPERATURE}"
            ),
        )
    if request_section.get("response_format") != EXPECTED_RESPONSE_FORMAT:
        return _incompatible(
            "request.response_format",
            (
                f"manifest request.response_format must equal "
                f"{EXPECTED_RESPONSE_FORMAT!r}; the vendored parser depends on it"
            ),
        )

    max_tokens = request_section.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        return _incompatible(
            "request.max_tokens",
            "manifest request.max_tokens must be a positive integer",
        )

    extra_body = request_section.get("extra_body")
    if not isinstance(extra_body, dict):
        return _incompatible(
            "request.extra_body",
            "manifest request.extra_body must be present as a JSON object",
        )
    return None


def _check_code(
    manifest: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    del deployment_record
    code_section = manifest.get("code", {})

    if not isinstance(code_section.get("repository"), str):
        return _incompatible(
            "code.repository",
            "manifest code.repository must be present as a string",
        )
    if not isinstance(code_section.get("commit"), str):
        return _incompatible(
            "code.commit", "manifest code.commit must be present as a string"
        )

    parser_hash = code_section.get("parser", {}).get("content_sha256")
    if parser_hash not in SUPPORTED_PARSER_CONTENT_HASHES:
        return _incompatible(
            "code.parser.content_sha256",
            (
                f"manifest parser content hash {parser_hash!r} is not in the "
                f"sidecar's supported set; vendor refresh required"
            ),
        )

    selector_hash = code_section.get("selector", {}).get("content_sha256")
    if selector_hash not in SUPPORTED_SELECTOR_CONTENT_HASHES:
        return _incompatible(
            "code.selector.content_sha256",
            (
                f"manifest selector content hash {selector_hash!r} is not in "
                f"the sidecar's supported set; vendor refresh required"
            ),
        )

    # An absent key means a pre-formula round: legacy selection, nothing to
    # gate. Any present value — including null — must carry a supported hash,
    # so malformed sections fail closed.
    if "score_formula" in code_section:
        formula_section = code_section.get("score_formula")
        formula_hash = (
            formula_section.get("content_sha256")
            if isinstance(formula_section, dict)
            else None
        )
        if formula_hash not in SUPPORTED_SCORE_FORMULA_CONTENT_HASHES:
            return _incompatible(
                "code.score_formula.content_sha256",
                (
                    f"manifest score formula content hash {formula_hash!r} is "
                    f"not in the sidecar's supported set; vendor refresh required"
                ),
            )
    return None


def _check_canonicalization(
    manifest: dict[str, Any], deployment_record: dict[str, Any]
) -> Failure | None:
    del deployment_record
    canon = manifest.get("canonicalization", {})
    if canon.get("hash_algorithm") != EXPECTED_CANONICAL_HASH_ALGORITHM:
        return _incompatible(
            "canonicalization.hash_algorithm",
            (
                f"manifest canonicalization.hash_algorithm "
                f"{canon.get('hash_algorithm')!r} must equal "
                f"{EXPECTED_CANONICAL_HASH_ALGORITHM!r}"
            ),
        )
    if canon.get("text_encoding") != EXPECTED_CANONICAL_TEXT_ENCODING:
        return _incompatible(
            "canonicalization.text_encoding",
            (
                f"manifest canonicalization.text_encoding "
                f"{canon.get('text_encoding')!r} must equal "
                f"{EXPECTED_CANONICAL_TEXT_ENCODING!r}"
            ),
        )

    json_encoding = canon.get("json_encoding", {})
    if json_encoding.get("sort_keys") is not True:
        return _incompatible(
            "canonicalization.json_encoding.sort_keys",
            "sidecar canonical JSON requires sort_keys=true",
        )
    if json_encoding.get("separators") != EXPECTED_CANONICAL_SEPARATORS:
        return _incompatible(
            "canonicalization.json_encoding.separators",
            (
                f"sidecar canonical JSON requires separators="
                f"{EXPECTED_CANONICAL_SEPARATORS!r}"
            ),
        )
    return None


def _resolve_effective_mode(
    deployment_mode: str, deployment_record: dict[str, Any]
) -> EffectiveMode:
    if (
        deployment_mode == "local"
        and deployment_record.get("gpu_mismatch_acknowledged") is True
    ):
        return "local_unverified"
    return deployment_mode  # type: ignore[return-value]


def _parse_launch_args(
    args: list[str],
) -> tuple[set[tuple[str, str]], set[str]]:
    pairs: set[tuple[str, str]] = set()
    flags: set[str] = set()
    i = 0
    while i < len(args):
        token = args[i]
        if not isinstance(token, str) or not token.startswith("--"):
            i += 1
            continue
        if "=" in token:
            flag, _, value = token.partition("=")
            pairs.add((flag, value))
            i += 1
            continue
        next_token = args[i + 1] if i + 1 < len(args) else None
        if isinstance(next_token, str) and not next_token.startswith("--"):
            pairs.add((token, next_token))
            i += 2
        else:
            flags.add(token)
            i += 1
    return pairs, flags


def _is_full_commit_hash(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != HF_COMMIT_HASH_LENGTH:
        return False
    lowered = value.lower()
    return all(char in HEX_DIGITS for char in lowered)


def _has_image_digest(value: Any) -> bool:
    return isinstance(value, str) and "@sha256:" in value


def _runtime_engine(kind: Any) -> str | None:
    """Return the engine suffix of a runtime kind (e.g. ``modal_sglang`` →
    ``sglang``), so Modal and local hosting of the same engine compare equal."""

    if not isinstance(kind, str) or not kind:
        return None
    return kind.rpartition("_")[2] or kind


def _incompatible(field_name: str, message: str) -> Failure:
    return Failure(
        category=FailureCategory.MANIFEST_INCOMPATIBLE,
        field=field_name,
        message=message,
    )


class ManifestError(ValueError):
    """Raised when a required manifest value is missing or malformed."""


def score_formula_present(manifest: dict[str, Any]) -> bool:
    """Return whether the round's manifest declares the deterministic formula.

    Rounds carrying ``code.score_formula`` select over formula-derived final
    scores; rounds without it predate the deterministic final-score stage and
    reproduce selection directly from the model scores. A null section counts
    as absent here — ``check_compatibility`` fails such rounds closed before
    scoring, so the two predicates cannot disagree on a scored round.
    """
    code = manifest.get("code", {}) if isinstance(manifest, dict) else {}
    return isinstance(code, dict) and code.get("score_formula") is not None


def selector_parameters(manifest: dict[str, Any]) -> dict[str, int]:
    """Return the UNL selector parameters frozen in the execution manifest.

    Reads ``code.selector.parameters`` and returns ``score_cutoff``,
    ``max_size``, and ``min_score_gap`` as integers. Raises ``ManifestError`` if
    any is absent or not an integer.
    """
    code = manifest.get("code", {}) if isinstance(manifest, dict) else {}
    selector = code.get("selector", {}) if isinstance(code, dict) else {}
    params = selector.get("parameters", {}) if isinstance(selector, dict) else {}
    if not isinstance(params, dict):
        raise ManifestError("manifest code.selector.parameters must be an object")
    result: dict[str, int] = {}
    for name in ("score_cutoff", "max_size", "min_score_gap"):
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManifestError(
                f"manifest code.selector.parameters.{name} must be an integer"
            )
        result[name] = value
    return result
