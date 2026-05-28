import json

import pytest

from validator_scoring_sidecar.scoring import (
    DIMENSIONAL_FIELDS,
    NetworkReport,
    ScoringResult,
    parse_response,
)


def _validator_map(*ids: str) -> dict[str, dict[str, str]]:
    return {
        anon_id: {"master_key": f"MASTER_{anon_id.upper()}"}
        for anon_id in ids
    }


def _validator_entry(
    score: int = 80,
    consensus: int = 80,
    reliability: int = 80,
    software: int = 80,
    diversity: int = 80,
    identity: int = 80,
    reasoning: str = "reasoning text",
) -> dict:
    return {
        "score": score,
        "consensus": consensus,
        "reliability": reliability,
        "software": software,
        "diversity": diversity,
        "identity": identity,
        "reasoning": reasoning,
    }


def _network_report_payload() -> dict:
    return {
        "headline": "Network healthy",
        "summary": "All systems nominal.",
        "categories": {
            field: {"tone": "positive", "body": f"{field} looks good"}
            for field in DIMENSIONAL_FIELDS
        },
    }


def _build_response(
    validators: dict[str, dict] | None = None,
    network_summary: str | None = "Network is stable.",
    network_report: dict | None = None,
) -> str:
    payload: dict = {}
    if network_summary is not None:
        payload["network_summary"] = network_summary
    if network_report is not None:
        payload["network_report"] = network_report
    if validators:
        payload.update(validators)
    return json.dumps(payload)


def test_parse_response_complete_with_network_summary():
    validator_id_map = _validator_map("v001", "v002")
    raw = _build_response(
        validators={"v001": _validator_entry(), "v002": _validator_entry(score=70)},
    )

    result = parse_response(raw, validator_id_map)

    assert isinstance(result, ScoringResult)
    assert result.complete is True
    assert result.errors == []
    assert len(result.validator_scores) == 2
    assert {s.master_key for s in result.validator_scores} == {
        "MASTER_V001",
        "MASTER_V002",
    }
    assert result.network_summary == "Network is stable."
    assert result.network_report is None
    assert result.raw_response == raw


def test_parse_response_complete_with_network_report_only():
    validator_id_map = _validator_map("v001")
    raw = _build_response(
        validators={"v001": _validator_entry()},
        network_summary=None,
        network_report=_network_report_payload(),
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is True
    assert result.errors == []
    assert result.network_summary == ""
    assert isinstance(result.network_report, NetworkReport)
    assert result.network_report.headline == "Network healthy"


def test_parse_response_complete_with_both_network_fields():
    validator_id_map = _validator_map("v001")
    raw = _build_response(
        validators={"v001": _validator_entry()},
        network_summary="Summary.",
        network_report=_network_report_payload(),
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is True
    assert result.network_summary == "Summary."
    assert result.network_report is not None


def test_parse_response_missing_both_network_fields_is_incomplete():
    validator_id_map = _validator_map("v001")
    raw = _build_response(
        validators={"v001": _validator_entry()},
        network_summary=None,
        network_report=None,
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("network_summary or network_report" in err for err in result.errors)


def test_parse_response_strips_code_fenced_json_with_language_tag():
    validator_id_map = _validator_map("v001")
    inner = _build_response(validators={"v001": _validator_entry()})
    raw = f"```json\n{inner}\n```"

    result = parse_response(raw, validator_id_map)

    assert result.complete is True
    assert len(result.validator_scores) == 1


def test_parse_response_strips_code_fenced_json_without_language_tag():
    validator_id_map = _validator_map("v001")
    inner = _build_response(validators={"v001": _validator_entry()})
    raw = f"```\n{inner}\n```"

    result = parse_response(raw, validator_id_map)

    assert result.complete is True
    assert len(result.validator_scores) == 1


def test_parse_response_recovers_json_object_surrounded_by_prose():
    validator_id_map = _validator_map("v001")
    inner = _build_response(validators={"v001": _validator_entry()})
    raw = f"Here is the result:\n{inner}\nEnd of output."

    result = parse_response(raw, validator_id_map)

    assert result.complete is True
    assert len(result.validator_scores) == 1


def test_parse_response_returns_incomplete_for_unparseable_text():
    validator_id_map = _validator_map("v001")
    raw = "this is not JSON at all"

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert result.validator_scores == []
    assert any("Failed to extract" in err for err in result.errors)


def test_parse_response_returns_incomplete_for_empty_input():
    validator_id_map = _validator_map("v001")

    result = parse_response("   \n  ", validator_id_map)

    assert result.complete is False
    assert any("Failed to extract" in err for err in result.errors)


def test_parse_response_reports_missing_validator():
    validator_id_map = _validator_map("v001", "v002")
    raw = _build_response(validators={"v001": _validator_entry()})

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("Missing validators: v002" in err for err in result.errors)
    assert len(result.validator_scores) == 1


def test_parse_response_reports_extra_validator():
    validator_id_map = _validator_map("v001")
    raw = _build_response(
        validators={
            "v001": _validator_entry(),
            "v999": _validator_entry(),
        },
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("Unexpected entries: v999" in err for err in result.errors)


@pytest.mark.parametrize(
    "bad_score",
    [101, -1, 100.5, "75", True, False, None],
)
def test_parse_response_rejects_invalid_overall_score(bad_score):
    validator_id_map = _validator_map("v001")
    raw = _build_response(
        validators={"v001": _validator_entry(score=bad_score)},
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("invalid or missing score" in err for err in result.errors)
    assert result.validator_scores == []


def test_parse_response_accepts_integer_valued_float_score():
    validator_id_map = _validator_map("v001")
    raw = _build_response(
        validators={"v001": _validator_entry(score=75.0)},
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is True
    assert result.validator_scores[0].score == 75


@pytest.mark.parametrize("missing_field", DIMENSIONAL_FIELDS)
def test_parse_response_rejects_validator_missing_dimensional_score(missing_field):
    validator_id_map = _validator_map("v001")
    entry = _validator_entry()
    entry.pop(missing_field)
    raw = _build_response(validators={"v001": entry})

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any(f"invalid or missing {missing_field}" in err for err in result.errors)
    assert result.validator_scores == []


def test_parse_response_rejects_validator_with_empty_reasoning():
    validator_id_map = _validator_map("v001")
    raw = _build_response(
        validators={"v001": _validator_entry(reasoning="   ")},
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("missing or empty reasoning" in err for err in result.errors)


def test_parse_response_rejects_validator_with_non_dict_entry():
    validator_id_map = _validator_map("v001")
    payload = {"network_summary": "summary", "v001": "not a dict"}
    raw = json.dumps(payload)

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("entry is not a dict" in err for err in result.errors)


def test_parse_response_rejects_network_report_with_missing_category():
    validator_id_map = _validator_map("v001")
    report = _network_report_payload()
    del report["categories"]["consensus"]
    raw = _build_response(
        validators={"v001": _validator_entry()},
        network_summary=None,
        network_report=report,
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("missing categories: consensus" in err for err in result.errors)


def test_parse_response_rejects_network_report_with_extra_category():
    validator_id_map = _validator_map("v001")
    report = _network_report_payload()
    report["categories"]["bogus"] = {"tone": "positive", "body": "extra"}
    raw = _build_response(
        validators={"v001": _validator_entry()},
        network_summary=None,
        network_report=report,
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("unexpected categories: bogus" in err for err in result.errors)


def test_parse_response_rejects_network_report_with_invalid_tone():
    validator_id_map = _validator_map("v001")
    report = _network_report_payload()
    report["categories"]["consensus"]["tone"] = "ecstatic"
    raw = _build_response(
        validators={"v001": _validator_entry()},
        network_summary=None,
        network_report=report,
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("network_report is invalid" in err for err in result.errors)


def test_parse_response_rejects_network_report_with_empty_body():
    validator_id_map = _validator_map("v001")
    report = _network_report_payload()
    report["categories"]["consensus"]["body"] = "   "
    raw = _build_response(
        validators={"v001": _validator_entry()},
        network_summary=None,
        network_report=report,
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is False
    assert any("network_report is invalid" in err for err in result.errors)


def test_parse_response_preserves_unicode_and_multiline_reasoning():
    validator_id_map = _validator_map("v001")
    reasoning = (
        "Validator looks healthy.\n"
        "  - Consensus rate excellent\n"
        "  - Identity verified via 한국어 domain (example.한국)\n"
        "  - Diversity score reflects EU presence"
    )
    raw = _build_response(
        validators={"v001": _validator_entry(reasoning=reasoning)},
    )

    result = parse_response(raw, validator_id_map)

    assert result.complete is True
    assert result.validator_scores[0].reasoning == reasoning


def test_parse_response_remaps_anonymous_ids_to_master_keys():
    validator_id_map = {
        "v001": {"master_key": "ED_FIRST_VALIDATOR"},
        "v002": {"master_key": "ED_SECOND_VALIDATOR"},
    }
    raw = _build_response(
        validators={
            "v001": _validator_entry(score=90),
            "v002": _validator_entry(score=80),
        },
    )

    result = parse_response(raw, validator_id_map)

    by_key = {s.master_key: s.score for s in result.validator_scores}
    assert by_key == {"ED_FIRST_VALIDATOR": 90, "ED_SECOND_VALIDATOR": 80}
