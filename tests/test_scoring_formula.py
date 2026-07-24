"""Tests for the vendored deterministic score formula (score formula v1)."""

from validator_scoring_sidecar.scoring import (
    ScoringResult,
    ValidatorScore,
    apply_formula,
    compute_final_score,
)
from validator_scoring_sidecar.scoring.formula import (
    CONSENSUS_GATE_MARGIN,
    FORMULA_VERSION,
    WEIGHTS,
)


def _validator(master_key, score, consensus, reliability, software, diversity, identity):
    return ValidatorScore(
        master_key=master_key,
        score=score,
        consensus=consensus,
        reliability=reliability,
        software=software,
        diversity=diversity,
        identity=identity,
        reasoning="test",
    )


def _result(validators):
    return ScoringResult(
        validator_scores=validators,
        network_summary="test",
        raw_response="{}",
        complete=True,
        errors=[],
    )


def test_parameters_match_foundation_spec():
    assert FORMULA_VERSION == 1
    assert WEIGHTS == {
        "consensus": 50,
        "reliability": 20,
        "software": 10,
        "diversity": 10,
        "identity": 10,
    }
    assert sum(WEIGHTS.values()) == 100
    assert CONSENSUS_GATE_MARGIN == 25


def test_worked_examples_from_foundation_design_doc():
    # The worked-examples table in the foundation's DeterministicFinalScore.md.
    assert compute_final_score(100, 90, 100, 40, 80) == 90
    assert compute_final_score(100, 85, 100, 50, 80) == 90
    assert compute_final_score(99, 91, 100, 55, 75) == 90
    assert compute_final_score(96, 70, 100, 62, 50) == 83
    assert compute_final_score(0, 85, 100, 40, 80) == 25


def test_consensus_gate_binds_only_when_consensus_lags():
    assert compute_final_score(100, 0, 0, 0, 0) == 50
    assert compute_final_score(0, 100, 100, 100, 100) == CONSENSUS_GATE_MARGIN


def test_apply_formula_replaces_scores_and_preserves_input():
    original = _result([
        _validator("nA", 92, 100, 90, 100, 40, 80),
        _validator("nB", 20, 0, 85, 100, 40, 80),
    ])
    final = apply_formula(original)

    assert [v.score for v in final.validator_scores] == [90, 25]
    assert [v.score for v in original.validator_scores] == [92, 20]
    assert final.validator_scores[0].master_key == "nA"
    assert final.raw_response == original.raw_response
