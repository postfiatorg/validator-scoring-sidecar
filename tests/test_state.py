import json
import sqlite3

import pytest

from validator_scoring_sidecar.input_package import FetchedInputPackage
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.state import (
    SCHEMA_VERSION,
    STATE_DB_FILENAME,
    STATE_DISCOVERED,
    STATE_INPUT_PACKAGE_VERIFIED,
    STATE_SCORED,
    STATE_SCORING_FAILED,
    ScoreOutcome,
    SidecarState,
    SidecarStateError,
)

_V1_SCHEMA = """
CREATE TABLE sidecar_rounds (
    network TEXT NOT NULL,
    round_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    scoring_status TEXT NOT NULL,
    sidecar_state TEXT NOT NULL,
    input_package_cid TEXT NOT NULL,
    input_package_hash TEXT NOT NULL,
    input_frozen_at TEXT NOT NULL,
    local_package_path TEXT,
    fetch_source TEXT,
    verified_file_count INTEGER,
    discovered_at TEXT NOT NULL,
    input_verified_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (network, round_id)
)
"""


def _metadata(package_hash="a" * 64):
    return RoundMetadata(
        round_id=123,
        round_number=456,
        status="INPUT_FROZEN",
        input_package_cid="QmInput",
        input_package_hash=package_hash,
        input_frozen_at="2026-05-25T00:00:00+00:00",
        final_bundle_cid=None,
    )


def _fetched_package(tmp_path, metadata):
    return FetchedInputPackage(
        round_id=metadata.round_id,
        round_number=metadata.round_number,
        network="testnet",
        input_package_cid=metadata.input_package_cid,
        input_package_hash=metadata.input_package_hash,
        input_frozen_at=metadata.input_frozen_at,
        source="https",
        cached=False,
        local_path=tmp_path / "packages" / metadata.input_package_hash,
        verified_file_count=3,
    )


def test_state_records_discovered_and_input_verified_round(tmp_path):
    metadata = _metadata()
    cache_path = tmp_path / "packages" / metadata.input_package_hash

    with SidecarState(tmp_path) as state:
        state.record_discovered("testnet", metadata)
        discovered = state.get_round("testnet", metadata.round_id)

        assert (tmp_path / STATE_DB_FILENAME).is_file()
        assert discovered is not None
        assert discovered.sidecar_state == STATE_DISCOVERED
        assert not state.is_input_package_verified(
            "testnet",
            metadata,
            cache_path=cache_path,
        )

        cache_path.mkdir(parents=True)
        state.record_input_verified("testnet", metadata, _fetched_package(tmp_path, metadata))
        verified = state.get_round("testnet", metadata.round_id)

        assert verified is not None
        assert verified.sidecar_state == STATE_INPUT_PACKAGE_VERIFIED
        assert verified.local_package_path == str(cache_path)
        assert verified.fetch_source == "https"
        assert verified.verified_file_count == 3
        assert state.is_input_package_verified(
            "testnet",
            metadata,
            cache_path=cache_path,
        )


def test_state_does_not_treat_changed_frozen_metadata_as_verified(tmp_path):
    metadata = _metadata()
    cache_path = tmp_path / "packages" / metadata.input_package_hash
    cache_path.mkdir(parents=True)

    with SidecarState(tmp_path) as state:
        state.record_input_verified("testnet", metadata, _fetched_package(tmp_path, metadata))

        changed_metadata = _metadata(package_hash="b" * 64)
        assert not state.is_input_package_verified(
            "testnet",
            changed_metadata,
            cache_path=tmp_path / "packages" / changed_metadata.input_package_hash,
        )


def test_record_score_persists_outcome_and_preserves_input_columns(tmp_path):
    metadata = _metadata()
    with SidecarState(tmp_path) as state:
        state.record_input_verified(
            "testnet", metadata, _fetched_package(tmp_path, metadata)
        )
        state.record_score(
            "testnet",
            metadata,
            ScoreOutcome(
                sidecar_state=STATE_SCORED,
                backend_mode="local",
                model_response_hash="m" * 64,
                validator_scores_hash="v" * 64,
                comparison_levels_matched=["RAW_MATCH", "PARSED_MATCH"],
            ),
        )

        record = state.get_round("testnet", metadata.round_id)

    assert record.sidecar_state == STATE_SCORED
    assert record.backend_mode == "local"
    assert record.model_response_hash == "m" * 64
    assert record.validator_scores_hash == "v" * 64
    assert record.comparison_levels_matched == "RAW_MATCH,PARSED_MATCH"
    # input-fetch columns are preserved by the score upsert.
    assert record.local_package_path == str(
        tmp_path / "packages" / metadata.input_package_hash
    )
    assert record.fetch_source == "https"


def test_record_score_pending_leaves_comparison_null(tmp_path):
    metadata = _metadata()
    with SidecarState(tmp_path) as state:
        state.record_input_verified(
            "testnet", metadata, _fetched_package(tmp_path, metadata)
        )
        state.record_score(
            "testnet",
            metadata,
            ScoreOutcome(
                sidecar_state=STATE_SCORED,
                backend_mode="modal",
                model_response_hash="m" * 64,
                validator_scores_hash="v" * 64,
                comparison_levels_matched=None,
            ),
        )

        record = state.get_round("testnet", metadata.round_id)

    assert record.comparison_levels_matched is None


def test_record_score_failed_persists_error(tmp_path):
    metadata = _metadata()
    with SidecarState(tmp_path) as state:
        state.record_input_verified(
            "testnet", metadata, _fetched_package(tmp_path, metadata)
        )
        state.record_score(
            "testnet",
            metadata,
            ScoreOutcome(
                sidecar_state=STATE_SCORING_FAILED,
                backend_mode="modal",
                error_category="INFERENCE_TIMEOUT",
                error_details={"message": "slow"},
            ),
        )

        record = state.get_round("testnet", metadata.round_id)

    assert record.sidecar_state == STATE_SCORING_FAILED
    assert record.error_category == "INFERENCE_TIMEOUT"
    assert json.loads(record.error_details) == {"message": "slow"}


def test_scored_round_counts_as_input_ready(tmp_path):
    metadata = _metadata()
    cache_path = tmp_path / "packages" / metadata.input_package_hash
    cache_path.mkdir(parents=True)
    with SidecarState(tmp_path) as state:
        state.record_input_verified(
            "testnet", metadata, _fetched_package(tmp_path, metadata)
        )
        state.record_score(
            "testnet",
            metadata,
            ScoreOutcome(
                sidecar_state=STATE_SCORED,
                model_response_hash="m" * 64,
                validator_scores_hash="v" * 64,
                comparison_levels_matched=["RAW_MATCH"],
            ),
        )

        assert state.is_input_package_verified(
            "testnet", metadata, cache_path=cache_path
        )


def test_v1_database_migrates_to_v2_and_preserves_rows(tmp_path):
    db_path = tmp_path / STATE_DB_FILENAME
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_V1_SCHEMA)
        connection.execute(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                discovered_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "testnet",
                123,
                456,
                "INPUT_FROZEN",
                STATE_INPUT_PACKAGE_VERIFIED,
                "QmInput",
                "a" * 64,
                "2026-05-25T00:00:00+00:00",
                "t",
                "t",
            ),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)
        assert record is not None
        assert record.sidecar_state == STATE_INPUT_PACKAGE_VERIFIED
        assert record.model_response_hash is None  # new v2 column present, NULL
        # record_score works against the migrated schema.
        state.record_score(
            "testnet",
            _metadata(),
            ScoreOutcome(
                sidecar_state=STATE_SCORED,
                backend_mode="modal",
                model_response_hash="m" * 64,
                validator_scores_hash="v" * 64,
                comparison_levels_matched=["RAW_MATCH", "PARSED_MATCH"],
            ),
        )
        assert state.get_round("testnet", 123).sidecar_state == STATE_SCORED

    # Reopening is idempotent (already at the current version).
    with SidecarState(tmp_path) as state:
        assert state.get_round("testnet", 123).sidecar_state == STATE_SCORED


def test_chain_cursor_round_trip(tmp_path):
    with SidecarState(tmp_path) as state:
        assert state.get_chain_cursor("testnet", "rPub") is None

        state.set_chain_cursor("testnet", "rPub", 500, "a" * 64)
        cursor = state.get_chain_cursor("testnet", "rPub")
        assert cursor is not None
        assert cursor.network == "testnet"
        assert cursor.account == "rPub"
        assert cursor.last_processed_ledger_index == 500
        assert cursor.last_processed_tx_hash == "a" * 64

        state.set_chain_cursor("testnet", "rPub", 501, "b" * 64)
        updated = state.get_chain_cursor("testnet", "rPub")
        assert updated.last_processed_ledger_index == 501
        assert updated.last_processed_tx_hash == "b" * 64


def test_fresh_database_is_schema_v3(tmp_path):
    with SidecarState(tmp_path) as state:
        state.set_chain_cursor("testnet", "rPub", 1, "h")

    connection = sqlite3.connect(tmp_path / STATE_DB_FILENAME)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    assert version == SCHEMA_VERSION


def test_v1_database_migrates_to_v3_adds_chain_cursor(tmp_path):
    db_path = tmp_path / STATE_DB_FILENAME
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_V1_SCHEMA)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    with SidecarState(tmp_path) as state:
        state.set_chain_cursor("testnet", "rPub", 10, "h")
        assert state.get_chain_cursor("testnet", "rPub").last_processed_ledger_index == 10

    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    assert version == SCHEMA_VERSION


def test_state_rejects_newer_schema_version(tmp_path):
    db_path = tmp_path / STATE_DB_FILENAME
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SidecarStateError, match="newer than supported"):
        with SidecarState(tmp_path):
            pass
