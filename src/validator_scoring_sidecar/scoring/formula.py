"""Deterministic final-score computation (score formula v1).

Computes the authoritative per-validator final score from the five
dimensional sub-scores, replacing the model's holistic overall score as
the quantity UNL selection consumes. The model's judgment stays in the
sub-scores; this module owns their composition. Integer arithmetic only,
so any independent reimplementation is bit-identical. Specification,
rationale, and empirical validation live in the foundation repository's
``docs/DeterministicFinalScore.md``.

Vendored from foundation ``scoring_service/services/score_formula.py``.
Local adaptations: ``ScoringResult`` and ``ValidatorScore`` are imported
from the vendored parser module instead of the foundation package, and the
foundation-only ``build_final_scores_artifact`` helper is omitted (the
sidecar reproduces selection, not the final-scores artifact). See the
package docstring in ``__init__.py`` for the refresh procedure.
"""

from validator_scoring_sidecar.scoring.parser import ScoringResult, ValidatorScore

FORMULA_VERSION = 1

WEIGHTS = {
    "consensus": 50,
    "reliability": 20,
    "software": 10,
    "diversity": 10,
    "identity": 10,
}

# Caps the final score at consensus + margin so secondary virtues cannot
# substitute for consensus participation.
CONSENSUS_GATE_MARGIN = 25


def compute_final_score(
    consensus: int,
    reliability: int,
    software: int,
    diversity: int,
    identity: int,
) -> int:
    """Compute one validator's final score from its five sub-scores."""
    weighted_sum = (
        WEIGHTS["consensus"] * consensus
        + WEIGHTS["reliability"] * reliability
        + WEIGHTS["software"] * software
        + WEIGHTS["diversity"] * diversity
        + WEIGHTS["identity"] * identity
    ) // 100
    return min(weighted_sum, consensus + CONSENSUS_GATE_MARGIN)


def _final_score_for(validator: ValidatorScore) -> int:
    return compute_final_score(
        validator.consensus,
        validator.reliability,
        validator.software,
        validator.diversity,
        validator.identity,
    )


def apply_formula(scoring_result: ScoringResult) -> ScoringResult:
    """Return a copy of the result whose overall scores are the final scores.

    The input result keeps the model's advisory scores untouched. The
    returned copy is what UNL selection consumes on formula rounds.
    """
    return scoring_result.model_copy(
        update={
            "validator_scores": [
                v.model_copy(update={"score": _final_score_for(v)})
                for v in scoring_result.validator_scores
            ]
        }
    )
