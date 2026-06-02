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

The ``selected_unl`` level is intentionally not handled here: UNL selection
needs the previous round's UNL, which the foundation reads from its database at
scoring time and does not freeze into the input package, so it is not yet
reproducible (see ``docs/phase2/SidecarScoringSpec.md`` in the foundation
repo). ``signed_validator_list`` is foundation-only; the sidecar never signs.

The document builders here mirror the foundation's ``_build_raw_response`` and
``_build_scores`` in ``scoring_service/services/ipfs_publisher.py``. They must
stay byte-for-byte in sync with those, since the hashes are taken over their
output; unlike the parser and selector they are not yet pinned by a manifest
content hash.

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
from validator_scoring_sidecar.scoring import ScoringResult, parse_response

VALIDATOR_MAP_RELATIVE_PATH = "inputs/validator_map.json"
SCORED_DIR_NAME = "scored"
VERIFICATION_HASHES_FILE_NAME = "verification_hashes.json"

LEVEL_RAW = "RAW_MATCH"
LEVEL_PARSED = "PARSED_MATCH"
HASH_MODEL_RESPONSE = "model_response_hash"
HASH_VALIDATOR_SCORES = "validator_scores_hash"

# Sidecar-comparable levels, in priority order, mapped to the corresponding key
# in the foundation's outputs/verification_hashes.json.
COMPARABLE_LEVELS: tuple[tuple[str, str], ...] = (
    (LEVEL_RAW, HASH_MODEL_RESPONSE),
    (LEVEL_PARSED, HASH_VALIDATOR_SCORES),
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


def compute_verification_hashes(
    raw_text: str,
    validator_id_map: dict[str, Any],
) -> dict[str, str]:
    """Compute the sidecar's reproducible verification hashes from a response."""

    scoring_result = parse_response(raw_text, validator_id_map)
    return {
        HASH_MODEL_RESPONSE: canonical_json_hash(
            build_model_response_document(raw_text)
        ),
        HASH_VALIDATOR_SCORES: canonical_json_hash(
            build_validator_scores_document(scoring_result)
        ),
    }


def verify_round(
    raw_text: str,
    validator_id_map: dict[str, Any],
    *,
    input_package_hash: str,
    foundation_hashes: dict[str, Any] | None = None,
) -> VerificationResult:
    """Compute the sidecar hashes and compare them to the foundation's, if given.

    When ``foundation_hashes`` is ``None`` (the foundation final bundle is not
    available yet), the sidecar hashes are computed but no comparison is made;
    the caller persists them and retries the comparison on a later pass.
    """

    hashes = compute_verification_hashes(raw_text, validator_id_map)
    if foundation_hashes is None:
        return VerificationResult(
            input_package_hash=input_package_hash,
            hashes=hashes,
            compared=False,
            matched_levels=[],
            diverged_levels=[],
            first_divergence=None,
        )

    matched: list[str] = []
    diverged: list[str] = []
    for level, hash_name in COMPARABLE_LEVELS:
        foundation_hash = foundation_hashes.get(hash_name)
        if not isinstance(foundation_hash, str):
            continue
        if hashes[hash_name] == foundation_hash:
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
        hashes=hashes,
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
