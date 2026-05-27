from validator_scoring_sidecar.input_package import FetchedInputPackage
from validator_scoring_sidecar.round_metadata import RoundMetadata
import sqlite3

import pytest

from validator_scoring_sidecar.state import (
    STATE_DISCOVERED,
    STATE_INPUT_PACKAGE_VERIFIED,
    STATE_DB_FILENAME,
    SCHEMA_VERSION,
    SidecarStateError,
    SidecarState,
)


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
