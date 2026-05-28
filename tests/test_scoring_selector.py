from validator_scoring_sidecar.scoring import (
    ScoringResult,
    UNLSelectionResult,
    ValidatorScore,
    select_unl,
)


def _validator(master_key: str, score: int) -> ValidatorScore:
    return ValidatorScore(
        master_key=master_key,
        score=score,
        consensus=score,
        reliability=score,
        software=score,
        diversity=score,
        identity=score,
        reasoning="test reasoning",
    )


def _result(*validators: ValidatorScore) -> ScoringResult:
    return ScoringResult(
        validator_scores=list(validators),
        network_summary="summary",
        network_report=None,
        raw_response="{}",
        complete=True,
        errors=[],
    )


def test_select_unl_returns_empty_for_empty_input():
    result = select_unl(_result(), cutoff=40, max_size=5, min_gap=5)

    assert result == UNLSelectionResult(unl=[], alternates=[])


def test_select_unl_returns_empty_when_all_below_cutoff():
    result = select_unl(
        _result(
            _validator("A", 30),
            _validator("B", 39),
            _validator("C", 10),
        ),
        cutoff=40,
        max_size=5,
        min_gap=5,
    )

    assert result == UNLSelectionResult(unl=[], alternates=[])


def test_select_unl_first_round_selects_all_when_below_max_size():
    result = select_unl(
        _result(
            _validator("A", 80),
            _validator("B", 70),
        ),
        cutoff=40,
        max_size=5,
        min_gap=5,
    )

    assert result.unl == ["A", "B"]
    assert result.alternates == []


def test_select_unl_first_round_caps_at_max_size_and_overflow_goes_to_alternates():
    result = select_unl(
        _result(
            _validator("A", 95),
            _validator("B", 90),
            _validator("C", 85),
            _validator("D", 80),
            _validator("E", 75),
        ),
        cutoff=40,
        max_size=3,
        min_gap=5,
    )

    assert result.unl == ["A", "B", "C"]
    assert result.alternates == ["D", "E"]


def test_select_unl_first_round_orders_by_score_desc_then_master_key_asc():
    result = select_unl(
        _result(
            _validator("ZED", 80),
            _validator("ABC", 80),
            _validator("MID", 80),
        ),
        cutoff=40,
        max_size=5,
        min_gap=5,
    )

    assert result.unl == ["ABC", "MID", "ZED"]


def test_select_unl_continuation_keeps_incumbent_when_challenger_within_min_gap():
    previous = ["INCUMBENT"]
    result = select_unl(
        _result(
            _validator("INCUMBENT", 60),
            _validator("CHALLENGER", 63),
        ),
        cutoff=40,
        max_size=1,
        min_gap=5,
        previous_unl=previous,
    )

    assert result.unl == ["INCUMBENT"]
    assert result.alternates == ["CHALLENGER"]


def test_select_unl_continuation_swaps_incumbent_when_challenger_exceeds_min_gap():
    previous = ["INCUMBENT"]
    result = select_unl(
        _result(
            _validator("INCUMBENT", 60),
            _validator("CHALLENGER", 70),
        ),
        cutoff=40,
        max_size=1,
        min_gap=5,
        previous_unl=previous,
    )

    assert result.unl == ["CHALLENGER"]
    assert result.alternates == ["INCUMBENT"]


def test_select_unl_continuation_fills_open_seats_without_min_gap_requirement():
    # max_size 3, only one incumbent qualifies, two challengers should fill
    # the open seats without min_gap gating.
    previous = ["INC_A", "INC_B"]
    result = select_unl(
        _result(
            _validator("INC_A", 80),
            _validator("CHA_X", 55),
            _validator("CHA_Y", 50),
        ),
        cutoff=40,
        max_size=3,
        min_gap=10,
        previous_unl=previous,
    )

    assert result.unl == ["INC_A", "CHA_X", "CHA_Y"]
    assert result.alternates == []


def test_select_unl_continuation_caps_surviving_incumbents_at_max_size():
    # Four incumbents all clear the cutoff but max_size is only 3; the
    # lowest-ranked incumbent drops to alternates.
    previous = ["INC_A", "INC_B", "INC_C", "INC_D"]
    result = select_unl(
        _result(
            _validator("INC_A", 90),
            _validator("INC_B", 85),
            _validator("INC_C", 80),
            _validator("INC_D", 75),
        ),
        cutoff=40,
        max_size=3,
        min_gap=5,
        previous_unl=previous,
    )

    assert result.unl == ["INC_A", "INC_B", "INC_C"]
    assert result.alternates == ["INC_D"]


def test_select_unl_continuation_mixes_cap_displacement_and_failed_challengers():
    # Two incumbents fit, third drops to alternates by cap; one challenger
    # tries to swap the weakest seated incumbent but fails the min_gap test.
    previous = ["INC_A", "INC_B", "INC_C"]
    result = select_unl(
        _result(
            _validator("INC_A", 90),
            _validator("INC_B", 85),
            _validator("INC_C", 70),
            _validator("CHA_X", 72),
        ),
        cutoff=40,
        max_size=2,
        min_gap=5,
        previous_unl=previous,
    )

    assert result.unl == ["INC_A", "INC_B"]
    # INC_C displaced by cap, CHA_X failed the gap test (72 vs 85 + 5 = 90).
    assert result.alternates == ["CHA_X", "INC_C"]


def test_select_unl_continuation_successful_swap_returns_displaced_incumbent_to_alternates():
    # max_size 1: a strong challenger replaces the only incumbent.
    previous = ["INC_A"]
    result = select_unl(
        _result(
            _validator("INC_A", 60),
            _validator("CHA_X", 80),
            _validator("CHA_Y", 55),
        ),
        cutoff=40,
        max_size=1,
        min_gap=5,
        previous_unl=previous,
    )

    assert result.unl == ["CHA_X"]
    # INC_A and CHA_Y both alternates, sorted by score desc.
    assert result.alternates == ["INC_A", "CHA_Y"]


def test_select_unl_ignores_previous_unl_entries_not_in_scoring_result():
    # An incumbent that vanished from this round's scoring input (dropped
    # from the candidate pool) is silently ignored. The remaining incumbent
    # holds its seat and the open seats are filled by challengers.
    previous = ["GONE", "INC_A"]
    result = select_unl(
        _result(
            _validator("INC_A", 80),
            _validator("CHA_X", 70),
        ),
        cutoff=40,
        max_size=5,
        min_gap=5,
        previous_unl=previous,
    )

    assert result.unl == ["INC_A", "CHA_X"]
    assert result.alternates == []


def test_select_unl_min_gap_zero_allows_equal_score_swap():
    # With min_gap=0, the displacement condition becomes >= weakest.score,
    # which means an equal-scoring challenger displaces the incumbent. This
    # is vendor-faithful behavior and is pinned by this test so a future
    # refresh does not silently change it.
    previous = ["INCUMBENT"]
    result = select_unl(
        _result(
            _validator("INCUMBENT", 60),
            _validator("CHALLENGER", 60),
        ),
        cutoff=40,
        max_size=1,
        min_gap=0,
        previous_unl=previous,
    )

    assert result.unl == ["CHALLENGER"]
    assert result.alternates == ["INCUMBENT"]


def test_select_unl_empty_previous_unl_treated_as_first_round():
    # An empty list (not None) should also trip the first-round path so
    # operators can pass either signal without changing behavior.
    result = select_unl(
        _result(
            _validator("A", 80),
            _validator("B", 70),
        ),
        cutoff=40,
        max_size=5,
        min_gap=5,
        previous_unl=[],
    )

    assert result.unl == ["A", "B"]
    assert result.alternates == []
