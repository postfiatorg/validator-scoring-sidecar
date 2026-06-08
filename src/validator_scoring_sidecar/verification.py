"""Output normalization, verification hashing, and foundation comparison.

After a backend returns the raw model response for a round, this module turns
that response into the canonical fingerprints the foundation publishes and
compares them, so an operator learns where their independent run agrees with
the foundation and where it first diverges.

It covers the two levels that are fully reproducible from the frozen input
package:

- ``raw_model_response`` — the raw response wrapped in the foundation's
  ``model_response`` document and hashed.
- ``validator_scores`` — the vendored parser's output rendered into the
  foundation's ``validator_scores`` document and hashed.

- ``selected_unl`` — the vendored selector run on the parsed scores with the
  previous UNL (now frozen into the input package as ``inputs/previous_unl.json``)
  and the manifest's selector parameters, rendered into the foundation's
  ``selected_unl`` document and hashed. It is computed only when the caller
  supplies the previous UNL and selector parameters.

``signed_validator_list`` is foundation-only; the sidecar never signs.

The document builders here mirror the foundation's ``_build_raw_response``,
``_build_scores``, and ``_build_unl`` in
``scoring_service/services/ipfs_publisher.py``. They must stay byte-for-byte in
sync with those, since the hashes are taken over their output.

This module is pure: it does no network I/O. Fetching the foundation's
``outputs/verification_hashes.json`` and recording state belong to the
end-to-end scoring command.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.failure import Failure, FailureCategory
from validator_scoring_sidecar.input_package import canonical_json_hash
from validator_scoring_sidecar.scoring import ScoringResult, parse_response, select_unl

VALIDATOR_MAP_RELATIVE_PATH = "inputs/validator_map.json"
PREVIOUS_UNL_RELATIVE_PATH = "inputs/previous_unl.json"
SCORED_DIR_NAME = "scored"
VERIFICATION_HASHES_FILE_NAME = "verification_hashes.json"

LEVEL_RAW = "RAW_MATCH"
LEVEL_PARSED = "PARSED_MATCH"
LEVEL_SELECTED_UNL = "SELECTED_UNL_MATCH"
HASH_MODEL_RESPONSE = "model_response_hash"
HASH_VALIDATOR_SCORES = "validator_scores_hash"
HASH_SELECTED_UNL = "selected_unl_hash"

# Sidecar-comparable levels, in priority order, mapped to the corresponding key
# in the foundation's outputs/verification_hashes.json. ``selected_unl`` is
# reproducible only once the previous UNL is frozen into the input package, so it
# is computed when the caller supplies the previous UNL and selector parameters.
COMPARABLE_LEVELS: tuple[tuple[str, str], ...] = (
    (LEVEL_RAW, HASH_MODEL_RESPONSE),
    (LEVEL_PARSED, HASH_VALIDATOR_SCORES),
    (LEVEL_SELECTED_UNL, HASH_SELECTED_UNL),
)


class VerificationError(RuntimeError):
    """Raised when verification inputs cannot be loaded or results persisted."""


@dataclass(frozen=True)
class VerificationResult:
    """Sidecar verification hashes and the comparison against the foundation."""

    input_package_hash: str
    hashes: dict[str, str]
    compared: bool
    matched_levels: list[str]
    diverged_levels: list[str]
    first_divergence: str | None
    failure: Failure | None = field(default=None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_package_hash": self.input_package_hash,
            "hashes": dict(self.hashes),
            "compared": self.compared,
            "matched_levels": list(self.matched_levels),
            "diverged_levels": list(self.diverged_levels),
            "first_divergence": self.first_divergence,
            "failure_category": (
                self.failure.category.value if self.failure is not None else None
            ),
            "failure_message": (
                self.failure.message if self.failure is not None else None
            ),
        }


def load_validator_map(package_path: Path) -> dict[str, Any]:
    """Load the frozen ``inputs/validator_map.json`` from a verified package."""

    target = Path(package_path).joinpath(*VALIDATOR_MAP_RELATIVE_PATH.split("/"))
    try:
        content = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerificationError(f"frozen validator map not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(
            f"frozen validator map is not valid JSON: {target}"
        ) from exc
    if not isinstance(content, dict):
        raise VerificationError(
            f"frozen validator map must be a JSON object: {target}"
        )
    return content


def load_previous_unl(package_path: Path) -> list[str]:
    """Load the frozen ``inputs/previous_unl.json`` from a verified package.

    Returns the previous round's UNL (validator master keys; empty for the first
    round). The foundation freezes this at INPUT_FROZEN so UNL selection is
    reproducible from the package alone.
    """

    target = Path(package_path).joinpath(*PREVIOUS_UNL_RELATIVE_PATH.split("/"))
    try:
        content = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerificationError(f"frozen previous UNL not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(
            f"frozen previous UNL is not valid JSON: {target}"
        ) from exc
    if not isinstance(content, dict):
        raise VerificationError(f"frozen previous UNL must be a JSON object: {target}")
    previous_unl = content.get("previous_unl")
    if not isinstance(previous_unl, list) or any(
        not isinstance(key, str) for key in previous_unl
    ):
        raise VerificationError(
            "frozen previous UNL must contain a 'previous_unl' list of strings: "
            f"{target}"
        )
    return previous_unl


def build_model_response_document(raw_text: str) -> dict[str, Any]:
    """Mirror the foundation's ``_build_raw_response``."""

    return {"raw_response": raw_text}


def build_validator_scores_document(scoring_result: ScoringResult) -> dict[str, Any]:
    """Mirror the foundation's ``_build_scores``."""

    scores: dict[str, Any] = {
        "validator_scores": [
            {
                "master_key": v.master_key,
                "score": v.score,
                "consensus": v.consensus,
                "reliability": v.reliability,
                "software": v.software,
                "diversity": v.diversity,
                "identity": v.identity,
                "reasoning": v.reasoning,
            }
            for v in scoring_result.validator_scores
        ],
    }
    if scoring_result.network_summary:
        scores["network_summary"] = scoring_result.network_summary
    if scoring_result.network_report is not None:
        scores["network_report"] = scoring_result.network_report.model_dump(mode="json")
    return scores


def build_selected_unl_document(unl_result) -> dict[str, Any]:
    """Mirror the foundation's ``_build_unl``."""

    return {
        "unl": list(unl_result.unl),
        "alternates": list(unl_result.alternates),
    }


def compute_verification_hashes(
    raw_text: str,
    validator_id_map: dict[str, Any],
    *,
    previous_unl: list[str] | None = None,
    selector_parameters: dict[str, int] | None = None,
) -> dict[str, str]:
    """Compute the sidecar's reproducible verification hashes from a response.

    ``model_response`` and ``validator_scores`` are always computed.
    ``selected_unl`` is computed only when both ``previous_unl`` and
    ``selector_parameters`` are supplied — the previous UNL comes from the frozen
    ``inputs/previous_unl.json`` and the parameters from the execution manifest's
    ``code.selector.parameters``.
    """

    scoring_result = parse_response(raw_text, validator_id_map)
    hashes = {
        HASH_MODEL_RESPONSE: canonical_json_hash(
            build_model_response_document(raw_text)
        ),
        HASH_VALIDATOR_SCORES: canonical_json_hash(
            build_validator_scores_document(scoring_result)
        ),
    }
    if previous_unl is not None and selector_parameters is not None:
        unl_result = select_unl(
            scoring_result,
            cutoff=selector_parameters["score_cutoff"],
            max_size=selector_parameters["max_size"],
            min_gap=selector_parameters["min_score_gap"],
            previous_unl=previous_unl,
        )
        hashes[HASH_SELECTED_UNL] = canonical_json_hash(
            build_selected_unl_document(unl_result)
        )
    return hashes


def verify_round(
    raw_text: str,
    validator_id_map: dict[str, Any],
    *,
    input_package_hash: str,
    foundation_hashes: dict[str, Any] | None = None,
    previous_unl: list[str] | None = None,
    selector_parameters: dict[str, int] | None = None,
) -> VerificationResult:
    """Compute the sidecar hashes and compare them to the foundation's, if given.

    When ``foundation_hashes`` is ``None`` (the foundation final bundle is not
    available yet), the sidecar hashes are computed but no comparison is made;
    the caller persists them and retries the comparison on a later pass.

    ``previous_unl`` and ``selector_parameters`` enable the ``selected_unl``
    level; omit them to compute only the model-response and validator-scores
    levels.
    """

    hashes = compute_verification_hashes(
        raw_text,
        validator_id_map,
        previous_unl=previous_unl,
        selector_parameters=selector_parameters,
    )
    if foundation_hashes is None:
        return VerificationResult(
            input_package_hash=input_package_hash,
            hashes=hashes,
            compared=False,
            matched_levels=[],
            diverged_levels=[],
            first_divergence=None,
        )
    return compare_hashes(input_package_hash, hashes, foundation_hashes)


def compare_hashes(
    input_package_hash: str,
    sidecar_hashes: dict[str, str],
    foundation_hashes: dict[str, Any],
) -> VerificationResult:
    """Compare already-computed sidecar hashes against the foundation's.

    Used both fresh (right after scoring) and for the deferred path, where the
    sidecar hashes were persisted on an earlier pass and only the foundation's
    final bundle has since become available.
    """

    matched: list[str] = []
    diverged: list[str] = []
    for level, hash_name in COMPARABLE_LEVELS:
        foundation_hash = foundation_hashes.get(hash_name)
        sidecar_hash = sidecar_hashes.get(hash_name)
        # Skip a level unless both sides produced it: a level the sidecar did
        # not reproduce (e.g. selected_unl without a frozen previous UNL) is not
        # a divergence.
        if not isinstance(foundation_hash, str) or not isinstance(sidecar_hash, str):
            continue
        if sidecar_hash == foundation_hash:
            matched.append(level)
        else:
            diverged.append(level)

    first_divergence = next(
        (level for level, _ in COMPARABLE_LEVELS if level in diverged),
        None,
    )
    failure = (
        Failure(
            category=FailureCategory.OUTPUT_DIVERGENCE,
            message=f"sidecar output diverged from the foundation at {first_divergence}",
            details={"matched_levels": matched, "diverged_levels": diverged},
        )
        if diverged
        else None
    )
    return VerificationResult(
        input_package_hash=input_package_hash,
        hashes=dict(sidecar_hashes),
        compared=bool(matched or diverged),
        matched_levels=matched,
        diverged_levels=diverged,
        first_divergence=first_divergence,
        failure=failure,
    )


def verification_hashes_path(config: SidecarConfig, input_package_hash: str) -> Path:
    """Return the local path the sidecar's verification hashes are written to."""

    return config.data_dir.joinpath(
        SCORED_DIR_NAME, input_package_hash, VERIFICATION_HASHES_FILE_NAME
    )


def read_verification_hashes(
    config: SidecarConfig,
    input_package_hash: str,
) -> dict[str, str] | None:
    """Read previously persisted sidecar hashes, or None if not yet written."""

    target = verification_hashes_path(config, input_package_hash)
    try:
        content = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise VerificationError(
            f"persisted verification hashes are not valid JSON: {target}"
        ) from exc
    if not isinstance(content, dict):
        raise VerificationError(
            f"persisted verification hashes must be a JSON object: {target}"
        )
    return content


def persist_verification_hashes(
    config: SidecarConfig,
    input_package_hash: str,
    hashes: dict[str, str],
) -> Path:
    """Write the sidecar's verification hashes for operator inspection.

    This is a new sibling directory to the verified input package cache; the
    M2.1 cache contract is untouched.
    """

    target = verification_hashes_path(config, input_package_hash)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        temp_path.write_text(
            json.dumps(hashes, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(target)
    except OSError as exc:
        raise VerificationError(
            f"failed to persist verification hashes at {target}: {exc}"
        ) from exc
    return target
