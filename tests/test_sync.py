import pytest

from validator_scoring_sidecar import sync as sync_module
from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.input_package import (
    FetchedInputPackage,
    InputPackageVerificationError,
)
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.state import (
    STATE_DISCOVERED,
    STATE_INPUT_PACKAGE_VERIFIED,
    SidecarState,
)
from validator_scoring_sidecar.sync import (
    SYNC_STATUS_INPUT_PACKAGE_READY,
    SYNC_STATUS_NO_ELIGIBLE_ROUND,
    SidecarLock,
    SyncLockError,
    sync_input_package,
)


class FakeClient:
    def __init__(self, rounds):
        self.rounds = rounds
        self.last_limit = None

    def fetch_rounds(self, *, limit, offset=0):
        self.last_limit = limit
        assert offset == 0
        return list(self.rounds)


class FakeFetcher:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def __call__(self, metadata, config, client, *, source, force):
        self.calls.append((metadata, source, force))
        if self.error is not None:
            raise self.error
        local_path = config.data_dir / "packages" / metadata.input_package_hash
        local_path.mkdir(parents=True, exist_ok=True)
        return FetchedInputPackage(
            round_id=metadata.round_id,
            round_number=metadata.round_number,
            network=config.network,
            input_package_cid=metadata.input_package_cid,
            input_package_hash=metadata.input_package_hash,
            input_frozen_at=metadata.input_frozen_at,
            source="https",
            cached=False,
            local_path=local_path,
            verified_file_count=3,
        )


def _config(tmp_path):
    return load_config(
        base_url="https://scoring.example.org",
        data_dir=tmp_path,
        network="testnet",
        environ={},
    )


def _payload(**overrides):
    payload = {
        "id": 123,
        "round_number": 456,
        "status": "INPUT_FROZEN",
        "input_package_cid": "QmInput",
        "input_package_hash": "a" * 64,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": None,
    }
    payload.update(overrides)
    return payload


def test_sync_fetches_newest_unhandled_round_with_frozen_metadata(tmp_path):
    config = _config(tmp_path)
    fetcher = FakeFetcher()
    rounds = [
        _payload(id=125, round_number=458, input_package_hash=None),
        _payload(id=124, round_number=457, input_package_hash="b" * 64),
        _payload(id=123, round_number=456),
    ]

    result = sync_input_package(
        config,
        FakeClient(rounds),
        source="ipfs",
        round_limit=10,
        package_fetcher=fetcher,
    )

    assert result.status == SYNC_STATUS_INPUT_PACKAGE_READY
    assert result.package is not None
    assert result.package.round_id == 124
    assert result.action == "fetched"
    assert fetcher.calls[0][0].round_id == 124
    assert fetcher.calls[0][1] == "ipfs"
    assert fetcher.calls[0][2] is False

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 124)

    assert record is not None
    assert record.sidecar_state == STATE_INPUT_PACKAGE_VERIFIED


def test_sync_is_idempotent_after_round_is_verified(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fetcher = FakeFetcher()
    client = FakeClient([_payload()])
    monkeypatch.setattr(
        sync_module,
        "verify_cached_input_package",
        lambda cache_path, metadata, config: None,
    )

    first = sync_input_package(config, client, package_fetcher=fetcher)
    second = sync_input_package(config, client, package_fetcher=fetcher)

    assert first.status == SYNC_STATUS_INPUT_PACKAGE_READY
    assert second.status == SYNC_STATUS_NO_ELIGIBLE_ROUND
    assert second.package is None
    assert len(fetcher.calls) == 1


def test_sync_records_discovered_state_before_fetch_failure(tmp_path):
    config = _config(tmp_path)
    fetcher = FakeFetcher(error=InputPackageVerificationError("hash mismatch"))

    with pytest.raises(InputPackageVerificationError):
        sync_input_package(config, FakeClient([_payload()]), package_fetcher=fetcher)

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)

    assert record is not None
    assert record.sidecar_state == STATE_DISCOVERED


def test_sync_revalidates_previously_verified_cache_before_skip(tmp_path):
    config = _config(tmp_path)
    payload = _payload()
    metadata = RoundMetadata.from_api_payload(payload, requested_round_id=123)
    cache_path = config.data_dir / "packages" / metadata.input_package_hash
    cache_path.mkdir(parents=True)
    fetcher = FakeFetcher()

    with SidecarState(tmp_path) as state:
        state.record_input_verified(
            "testnet",
            metadata,
            FetchedInputPackage(
                round_id=metadata.round_id,
                round_number=metadata.round_number,
                network=config.network,
                input_package_cid=metadata.input_package_cid,
                input_package_hash=metadata.input_package_hash,
                input_frozen_at=metadata.input_frozen_at,
                source="https",
                cached=False,
                local_path=cache_path,
                verified_file_count=3,
            ),
        )

    with pytest.raises(InputPackageVerificationError, match="Previously verified"):
        sync_input_package(config, FakeClient([payload]), package_fetcher=fetcher)

    assert fetcher.calls == []


def test_sync_lock_prevents_overlapping_runs(tmp_path):
    config = _config(tmp_path)

    with SidecarLock(tmp_path):
        with pytest.raises(SyncLockError):
            sync_input_package(
                config,
                FakeClient([_payload()]),
                package_fetcher=FakeFetcher(),
            )
