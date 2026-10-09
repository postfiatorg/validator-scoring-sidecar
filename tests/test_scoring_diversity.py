"""Tests for the vendored deterministic diversity sub-score (diversity formula v1)."""

import pytest

from validator_scoring_sidecar.scoring import (
    ScoringResult,
    ValidatorScore,
    apply_diversity_formula,
    compute_diversity,
)
from validator_scoring_sidecar.scoring.diversity import (
    AXIS_PENALTY,
    AXIS_POINTS,
    DIVERSITY_FORMULA_VERSION,
    UNKNOWN_AXIS_POINTS,
)

# Testnet round 26: 44 resolved endpoints; the values below are the
# foundation design doc's worked examples.
ROUND_26_RESOLVED = 44


def _validator(master_key, diversity):
    return ValidatorScore(
        master_key=master_key,
        score=80,
        consensus=90,
        reliability=90,
        software=100,
        diversity=diversity,
        identity=80,
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


DIVERSITY_INPUTS = {
    "resolved_endpoints": ROUND_26_RESOLVED,
    "validators": [
        {"master_key": "nA", "country_validators": 1, "provider_validators": 1},
        {"master_key": "nB", "country_validators": 9, "provider_validators": 14},
    ],
}


def test_parameters_match_foundation_spec():
    assert DIVERSITY_FORMULA_VERSION == 1
    assert AXIS_POINTS == 50
    assert AXIS_PENALTY == 119
    assert UNKNOWN_AXIS_POINTS == 10


@pytest.mark.parametrize(
    ("country", "provider", "expected"),
    [
        (1, 1, 100),  # unique on both axes
        (1, 10, 75),  # Vultr (10) in a unique country
        (2, 1, 97),  # Akamai (1) in Japan (2)
        (9, 14, 42),  # Hetzner (14) in Germany (9)
        (15, 14, 25),  # Hetzner (14) in the United States (15)
        (3, None, 54),  # Canada (3), provider unknown
        (None, None, 20),  # location unknown
    ],
)
def test_worked_examples_from_foundation_design_doc(country, provider, expected):
    assert compute_diversity(country, provider, ROUND_26_RESOLVED) == expected


def test_a_lone_resolved_validator_scores_full_points():
    assert compute_diversity(1, 1, 1) == 100
    assert compute_diversity(None, 1, 1) == UNKNOWN_AXIS_POINTS + AXIS_POINTS
    assert compute_diversity(None, None, 0) == 2 * UNKNOWN_AXIS_POINTS


def test_an_axis_bottoms_out_at_zero_before_full_crowding():
    assert compute_diversity(1, 18, 44) > compute_diversity(1, 19, 44) == AXIS_POINTS
    assert compute_diversity(1, 19, 44) == compute_diversity(1, 44, 44)
    assert compute_diversity(44, 44, 44) == 0


def test_rounds_half_up_without_floating_point():
    # others = 2: shared axis 50*2 - 119 < 0 -> 0, unique axis 100: 100 / 2 = 50.
    assert compute_diversity(2, 1, 3) == 50
    # others = 3: shared axis 150 - 119 = 31, unique axis 150: 181 / 3 = 60.33 -> 60.
    assert compute_diversity(2, 1, 4) == 60


def test_apply_diversity_formula_replaces_only_diversity_and_preserves_input():
    original = _result([_validator("nA", 90), _validator("nB", 40)])

    computed = apply_diversity_formula(original, DIVERSITY_INPUTS)

    assert [v.diversity for v in computed.validator_scores] == [100, 42]
    assert [v.diversity for v in original.validator_scores] == [90, 40]
    for before, after in zip(original.validator_scores, computed.validator_scores):
        assert after.model_dump(exclude={"diversity"}) == before.model_dump(
            exclude={"diversity"}
        )
    assert computed.raw_response == original.raw_response


def test_apply_diversity_formula_fails_closed_on_a_validator_without_inputs():
    with pytest.raises(ValueError, match="nC"):
        apply_diversity_formula(
            _result([_validator("nA", 90), _validator("nC", 50)]), DIVERSITY_INPUTS
        )
