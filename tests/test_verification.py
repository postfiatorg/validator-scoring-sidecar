import json

import pytest

from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.input_package import canonical_json_hash
from validator_scoring_sidecar.scoring import parse_response
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_VALIDATOR_SCORES,
    LEVEL_PARSED,
    LEVEL_RAW,
    VerificationError,
    build_model_response_document,
    build_validator_scores_document,
    compute_verification_hashes,
    load_validator_map,
    persist_verification_hashes,
    verification_hashes_path,
    verify_round,
)

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


def test_load_validator_map_missing_raises(tmp_path):
    with pytest.raises(VerificationError, match="not found"):
        load_validator_map(tmp_path)


def test_load_validator_map_invalid_json_raises(tmp_path):
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "validator_map.json").write_text("{bad", encoding="utf-8")

    with pytest.raises(VerificationError, match="not valid JSON"):
        load_validator_map(tmp_path)
