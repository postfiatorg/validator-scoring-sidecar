import json

import pytest

from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.input_package import canonical_json_hash
from validator_scoring_sidecar.scoring import parse_response
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_SELECTED_UNL,
    HASH_VALIDATOR_SCORES,
    LEVEL_PARSED,
    LEVEL_RAW,
    LEVEL_SELECTED_UNL,
    VerificationError,
    build_model_response_document,
    build_selected_unl_document,
    build_validator_scores_document,
    compare_hashes,
    compute_verification_hashes,
    load_previous_unl,
    load_validator_map,
    persist_verification_hashes,
    read_verification_hashes,
    verification_hashes_path,
    verify_round,
)

SELECTOR_PARAMETERS = {"score_cutoff": 40, "max_size": 35, "min_score_gap": 5}

MASTER_KEY = "nHBoneMasterKey"
VALIDATOR_MAP = {"v1": {"master_key": MASTER_KEY}}
INPUT_PACKAGE_HASH = "a" * 64


def _entry(**overrides):
    entry = {
        "score": 80,
        "consensus": 81,
        "reliability": 82,
        "software": 83,
        "diversity": 84,
        "identity": 85,
        "reasoning": "solid",
    }
    entry.update(overrides)
    return entry


def _raw_response():
    return json.dumps({"v1": _entry(), "network_summary": "healthy"})


def _raw_response_with_report():
    report = {
        "headline": "H",
        "summary": "S",
        "categories": {
            "consensus": {"tone": "positive", "body": "b1"},
            "reliability": {"tone": "neutral", "body": "b2"},
            "software": {"tone": "warning", "body": "b3"},
            "diversity": {"tone": "mixed", "body": "b4"},
            "identity": {"tone": "negative", "body": "b5"},
        },
    }
    return json.dumps({"v1": _entry(), "network_report": report})


def _config(tmp_path):
    return load_config(
        base_url="https://scoring.example.org",
        data_dir=tmp_path,
        network="testnet",
        environ={},
    )


# ---------------------------------------------------------------------------
# Document shapes and hashing
# ---------------------------------------------------------------------------


def test_build_model_response_document_wraps_raw_text():
    assert build_model_response_document("raw text") == {"raw_response": "raw text"}


def test_compute_hashes_match_foundation_equivalent_documents():
    raw = _raw_response()

    hashes = compute_verification_hashes(raw, VALIDATOR_MAP)

    expected_model_response = {"raw_response": raw}
    expected_scores = {
        "validator_scores": [
            {
                "master_key": MASTER_KEY,
                "score": 80,
                "consensus": 81,
                "reliability": 82,
                "software": 83,
                "diversity": 84,
                "identity": 85,
                "reasoning": "solid",
            }
        ],
        "network_summary": "healthy",
    }
    assert hashes[HASH_MODEL_RESPONSE] == canonical_json_hash(expected_model_response)
    assert hashes[HASH_VALIDATOR_SCORES] == canonical_json_hash(expected_scores)


def test_validator_scores_document_includes_network_report():
    result = parse_response(_raw_response_with_report(), VALIDATOR_MAP)

    doc = build_validator_scores_document(result)

    assert "network_summary" not in doc
    assert doc["network_report"]["headline"] == "H"
    assert set(doc["network_report"]["categories"]) == {
        "consensus",
        "reliability",
        "software",
        "diversity",
        "identity",
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_verify_round_without_foundation_hashes_defers_comparison():
    result = verify_round(
        _raw_response(),
        VALIDATOR_MAP,
        input_package_hash=INPUT_PACKAGE_HASH,
    )

    assert result.compared is False
    assert result.matched_levels == []
    assert result.diverged_levels == []
    assert result.first_divergence is None
    assert result.failure is None
    assert set(result.hashes) == {HASH_MODEL_RESPONSE, HASH_VALIDATOR_SCORES}


def test_verify_round_all_levels_match():
    raw = _raw_response()
    foundation = compute_verification_hashes(raw, VALIDATOR_MAP)

    result = verify_round(
        raw,
        VALIDATOR_MAP,
        input_package_hash=INPUT_PACKAGE_HASH,
        foundation_hashes=foundation,
    )

    assert result.compared is True
    assert result.matched_levels == [LEVEL_RAW, LEVEL_PARSED]
    assert result.diverged_levels == []
    assert result.first_divergence is None
    assert result.failure is None


def test_verify_round_raw_divergence_reports_first_divergence():
    raw = _raw_response()
    foundation = dict(compute_verification_hashes(raw, VALIDATOR_MAP))
    foundation[HASH_MODEL_RESPONSE] = "0" * 64  # raw differs, parsed still matches

    result = verify_round(
        raw,
        VALIDATOR_MAP,
        input_package_hash=INPUT_PACKAGE_HASH,
        foundation_hashes=foundation,
    )

    assert result.matched_levels == [LEVEL_PARSED]
    assert result.diverged_levels == [LEVEL_RAW]
    assert result.first_divergence == LEVEL_RAW
    assert result.failure.category == FailureCategory.OUTPUT_DIVERGENCE


def test_verify_round_parsed_divergence():
    raw = _raw_response()
    foundation = dict(compute_verification_hashes(raw, VALIDATOR_MAP))
    foundation[HASH_VALIDATOR_SCORES] = "0" * 64  # raw matches, parsed differs

    result = verify_round(
        raw,
        VALIDATOR_MAP,
        input_package_hash=INPUT_PACKAGE_HASH,
        foundation_hashes=foundation,
    )

    assert result.matched_levels == [LEVEL_RAW]
    assert result.diverged_levels == [LEVEL_PARSED]
    assert result.first_divergence == LEVEL_PARSED
    assert result.failure.category == FailureCategory.OUTPUT_DIVERGENCE


def test_verify_round_with_empty_foundation_hashes_compares_nothing():
    result = verify_round(
        _raw_response(),
        VALIDATOR_MAP,
        input_package_hash=INPUT_PACKAGE_HASH,
        foundation_hashes={},
    )

    assert result.compared is False
    assert result.matched_levels == []
    assert result.diverged_levels == []
    assert result.failure is None
    assert set(result.hashes) == {HASH_MODEL_RESPONSE, HASH_VALIDATOR_SCORES}


def test_verify_round_skips_levels_the_foundation_did_not_publish():
    raw = _raw_response()
    foundation = {HASH_MODEL_RESPONSE: compute_verification_hashes(raw, VALIDATOR_MAP)[
        HASH_MODEL_RESPONSE
    ]}

    result = verify_round(
        raw,
        VALIDATOR_MAP,
        input_package_hash=INPUT_PACKAGE_HASH,
        foundation_hashes=foundation,
    )

    assert result.matched_levels == [LEVEL_RAW]
    assert result.diverged_levels == []
    assert result.failure is None


# ---------------------------------------------------------------------------
# Persistence and input loading
# ---------------------------------------------------------------------------


def test_persist_verification_hashes_writes_sibling_file(tmp_path):
    config = _config(tmp_path)
    hashes = {HASH_MODEL_RESPONSE: "a" * 64, HASH_VALIDATOR_SCORES: "b" * 64}

    path = persist_verification_hashes(config, "c" * 64, hashes)

    assert path == verification_hashes_path(config, "c" * 64)
    assert path == tmp_path / "scored" / ("c" * 64) / "verification_hashes.json"
    assert json.loads(path.read_text(encoding="utf-8")) == hashes


def test_load_validator_map_reads_inputs_file(tmp_path):
    package_path = tmp_path / "packages" / ("a" * 64)
    inputs_dir = package_path / "inputs"
    inputs_dir.mkdir(parents=True)
    (inputs_dir / "validator_map.json").write_text(
        json.dumps(VALIDATOR_MAP), encoding="utf-8"
    )

    assert load_validator_map(package_path) == VALIDATOR_MAP


def test_read_verification_hashes_missing_returns_none(tmp_path):
    assert read_verification_hashes(_config(tmp_path), "a" * 64) is None


def test_read_verification_hashes_invalid_json_raises(tmp_path):
    config = _config(tmp_path)
    path = verification_hashes_path(config, "a" * 64)
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")

    with pytest.raises(VerificationError, match="not valid JSON"):
        read_verification_hashes(config, "a" * 64)


def test_load_validator_map_missing_raises(tmp_path):
    with pytest.raises(VerificationError, match="not found"):
        load_validator_map(tmp_path)


def test_load_validator_map_invalid_json_raises(tmp_path):
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "validator_map.json").write_text("{bad", encoding="utf-8")

    with pytest.raises(VerificationError, match="not valid JSON"):
        load_validator_map(tmp_path)


def test_build_selected_unl_document_mirrors_foundation():
    class _Result:
        unl = ["nHa", "nHb"]
        alternates = ["nHc"]

    assert build_selected_unl_document(_Result()) == {
        "unl": ["nHa", "nHb"],
        "alternates": ["nHc"],
    }


def test_compute_verification_hashes_includes_selected_unl_when_enabled():
    hashes = compute_verification_hashes(
        _raw_response(),
        VALIDATOR_MAP,
        previous_unl=[],
        selector_parameters=SELECTOR_PARAMETERS,
    )
    # v1 scores 80 (>= cutoff 40), first round → it is the only UNL member.
    assert hashes[HASH_SELECTED_UNL] == canonical_json_hash(
        {"unl": [MASTER_KEY], "alternates": []}
    )


def test_compute_verification_hashes_omits_selected_unl_without_inputs():
    hashes = compute_verification_hashes(_raw_response(), VALIDATOR_MAP)
    assert HASH_SELECTED_UNL not in hashes
    assert HASH_MODEL_RESPONSE in hashes
    assert HASH_VALIDATOR_SCORES in hashes


def test_compare_hashes_skips_selected_unl_the_sidecar_did_not_compute():
    sidecar = compute_verification_hashes(_raw_response(), VALIDATOR_MAP)  # 2 hashes
    foundation = dict(sidecar)
    foundation[HASH_SELECTED_UNL] = "f" * 64  # foundation has it; sidecar does not
    result = compare_hashes(INPUT_PACKAGE_HASH, sidecar, foundation)
    assert LEVEL_SELECTED_UNL not in result.matched_levels
    assert LEVEL_SELECTED_UNL not in result.diverged_levels
    assert result.failure is None


def test_load_previous_unl_reads_frozen_file(tmp_path):
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "previous_unl.json").write_text(
        '{"previous_unl": ["nHx", "nHy"]}', encoding="utf-8"
    )
    assert load_previous_unl(tmp_path) == ["nHx", "nHy"]


def test_selection_is_bimodal_on_score_formula():
    # score 80 clears the cutoff, but consensus 0 caps the formula final at
    # 25, below it: a formula round must exclude what a legacy round selects.
    raw = json.dumps({
        "v1": _entry(consensus=0, reliability=85, software=100, diversity=40, identity=80),
        "network_summary": "healthy",
    })

    legacy = compute_verification_hashes(
        raw,
        VALIDATOR_MAP,
        previous_unl=[],
        selector_parameters=SELECTOR_PARAMETERS,
    )
    formula = compute_verification_hashes(
        raw,
        VALIDATOR_MAP,
        previous_unl=[],
        selector_parameters=SELECTOR_PARAMETERS,
        apply_score_formula=True,
    )

    assert legacy[HASH_SELECTED_UNL] == canonical_json_hash(
        {"unl": [MASTER_KEY], "alternates": []}
    )
    assert formula[HASH_SELECTED_UNL] == canonical_json_hash(
        {"unl": [], "alternates": []}
    )
    # The LLM-output levels are unaffected by the formula.
    assert legacy[HASH_MODEL_RESPONSE] == formula[HASH_MODEL_RESPONSE]
    assert legacy[HASH_VALIDATOR_SCORES] == formula[HASH_VALIDATOR_SCORES]
