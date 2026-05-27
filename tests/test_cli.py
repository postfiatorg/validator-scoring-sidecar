import json

import pytest

from validator_scoring_sidecar import cli
from validator_scoring_sidecar.input_package import (
    FetchedInputPackage,
    InputPackageVerificationError,
)
from validator_scoring_sidecar.sync import SyncLockError, SyncResult


class FakeClient:
    payload = None
    error = None
    last_config = None

    def __init__(self, config):
        self.config = config
        FakeClient.last_config = config

    def fetch_round(self, round_id):
        if self.error is not None:
            raise self.error
        return dict(self.payload)

    def close(self):
        pass


class FakePackageFetcher:
    error = None
    last_force = None
    last_metadata = None
    last_source = None

    def __call__(self, metadata, config, client, *, source, force):
        FakePackageFetcher.last_force = force
        FakePackageFetcher.last_metadata = metadata
        FakePackageFetcher.last_source = source
        if FakePackageFetcher.error is not None:
            raise FakePackageFetcher.error
        return FetchedInputPackage(
            round_id=metadata.round_id,
            round_number=metadata.round_number,
            network=config.network,
            input_package_cid=metadata.input_package_cid,
            input_package_hash=metadata.input_package_hash,
            input_frozen_at=metadata.input_frozen_at,
            source="https",
            cached=False,
            local_path=config.data_dir / "packages" / metadata.input_package_hash,
            verified_file_count=3,
        )


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.payload = _payload()
    FakeClient.error = None
    FakeClient.last_config = None
    FakePackageFetcher.error = None
    FakePackageFetcher.last_force = None
    FakePackageFetcher.last_metadata = None
    FakePackageFetcher.last_source = None
    monkeypatch.setattr(cli, "ScoringClient", FakeClient)
    monkeypatch.setattr(cli, "fetch_verified_input_package", FakePackageFetcher())


def _payload(**overrides):
    payload = {
        "id": 123,
        "round_number": 456,
        "status": "COMPLETE",
        "input_package_cid": "QmInput",
        "input_package_hash": "a" * 64,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": "QmFinal",
    }
    payload.update(overrides)
    return payload


def test_invalid_config_exits_usage_error(capsys):
    exit_code = cli.main(
        [
            "fetch-input-package",
            "--round-id",
            "123",
            "--base-url",
            "not-a-url",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Configuration error" in captured.err


def test_cli_network_flag_selects_matching_default_url(capsys, monkeypatch):
    monkeypatch.setenv(
        "POSTFIAT_SCORING_BASE_URL",
        "https://scoring-testnet.postfiat.org",
    )

    exit_code = cli.main(
        ["fetch-input-package", "--round-id", "123", "--network", "devnet"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert FakeClient.last_config.scoring_base_url == "https://scoring-devnet.postfiat.org"
    assert str(FakeClient.last_config.data_dir).endswith(
        ".postfiat/validator-scoring-sidecar/devnet"
    )
    assert captured.err == ""


def test_fetch_input_package_human_output(capsys, tmp_path):
    exit_code = cli.main(
        [
            "fetch-input-package",
            "--round-id",
            "123",
            "--data-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Round ID: 123" in captured.out
    assert "Round number: 456" in captured.out
    assert "Network: testnet" in captured.out
    assert "Input package CID: QmInput" in captured.out
    assert "Input package hash: " + "a" * 64 in captured.out
    assert "Source: https" in captured.out
    assert "Cache status: fetched" in captured.out
    assert "Verified files: 3" in captured.out
    assert str(tmp_path / "packages" / ("a" * 64)) in captured.out
    assert FakePackageFetcher.last_source == "auto"
    assert FakePackageFetcher.last_force is False
    assert captured.err == ""


def test_fetch_input_package_json_output(capsys, tmp_path):
    exit_code = cli.main(
        [
            "fetch-input-package",
            "--round-id",
            "123",
            "--data-dir",
            str(tmp_path),
            "--source",
            "ipfs",
            "--force",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "round_id": 123,
        "round_number": 456,
        "network": "testnet",
        "input_package_cid": "QmInput",
        "input_package_hash": "a" * 64,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "source": "https",
        "cached": False,
        "local_path": str(tmp_path / "packages" / ("a" * 64)),
        "verified_file_count": 3,
    }
    assert FakePackageFetcher.last_source == "ipfs"
    assert FakePackageFetcher.last_force is True


def test_fetch_input_package_missing_frozen_metadata_fails(capsys):
    FakeClient.payload = _payload(input_package_cid=None)

    exit_code = cli.main(["fetch-input-package", "--round-id", "123"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "does not expose frozen input package metadata" in captured.err
    assert captured.out == ""


def test_fetch_input_package_verification_error_fails(capsys):
    FakePackageFetcher.error = InputPackageVerificationError("hash mismatch")

    exit_code = cli.main(["fetch-input-package", "--round-id", "123"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "hash mismatch" in captured.err
    assert captured.out == ""


def test_sync_json_output(capsys, monkeypatch, tmp_path):
    def fake_sync(config, client, *, source, round_limit):
        assert source == "auto"
        assert round_limit == 5
        return SyncResult(
            status="input_package_ready",
            network=config.network,
            scanned_rounds=2,
            package=FetchedInputPackage(
                round_id=123,
                round_number=456,
                network=config.network,
                input_package_cid="QmInput",
                input_package_hash="a" * 64,
                input_frozen_at="2026-05-25T00:00:00+00:00",
                source="https",
                cached=False,
                local_path=tmp_path / "packages" / ("a" * 64),
                verified_file_count=3,
            ),
        )

    monkeypatch.setattr(cli, "sync_input_package", fake_sync)

    exit_code = cli.main(
        [
            "sync",
            "--data-dir",
            str(tmp_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "status": "input_package_ready",
        "network": "testnet",
        "scanned_rounds": 2,
        "action": "fetched",
        "round_id": 123,
        "round_number": 456,
        "package": {
            "round_id": 123,
            "round_number": 456,
            "network": "testnet",
            "input_package_cid": "QmInput",
            "input_package_hash": "a" * 64,
            "input_frozen_at": "2026-05-25T00:00:00+00:00",
            "source": "https",
            "cached": False,
            "local_path": str(tmp_path / "packages" / ("a" * 64)),
            "verified_file_count": 3,
        },
    }
    assert captured.err == ""


def test_sync_lock_json_output(capsys, monkeypatch, tmp_path):
    def fake_sync(config, client, *, source, round_limit):
        raise SyncLockError(tmp_path / "sidecar.lock")

    monkeypatch.setattr(cli, "sync_input_package", fake_sync)

    exit_code = cli.main(
        [
            "sync",
            "--data-dir",
            str(tmp_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert json.loads(captured.out) == {
        "status": "locked",
        "network": "testnet",
        "lock_path": str(tmp_path / "sidecar.lock"),
    }
    assert captured.err == ""


def test_sync_no_eligible_round_json_output(capsys, monkeypatch, tmp_path):
    def fake_sync(config, client, *, source, round_limit):
        return SyncResult(
            status="no_eligible_round",
            network=config.network,
            scanned_rounds=2,
        )

    monkeypatch.setattr(cli, "sync_input_package", fake_sync)

    exit_code = cli.main(
        [
            "sync",
            "--data-dir",
            str(tmp_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "status": "no_eligible_round",
        "network": "testnet",
        "scanned_rounds": 2,
    }
    assert captured.err == ""


def test_sync_human_output(capsys, monkeypatch, tmp_path):
    def fake_sync(config, client, *, source, round_limit):
        return SyncResult(
            status="input_package_ready",
            network=config.network,
            scanned_rounds=1,
            package=FetchedInputPackage(
                round_id=123,
                round_number=456,
                network=config.network,
                input_package_cid="QmInput",
                input_package_hash="a" * 64,
                input_frozen_at="2026-05-25T00:00:00+00:00",
                source="https",
                cached=True,
                local_path=tmp_path / "packages" / ("a" * 64),
                verified_file_count=3,
            ),
        )

    monkeypatch.setattr(cli, "sync_input_package", fake_sync)

    exit_code = cli.main(["sync", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Sync status: input package ready" in captured.out
    assert "Action: cache_reused" in captured.out
    assert "Scanned rounds: 1" in captured.out
    assert "Cache status: reused" in captured.out
    assert captured.err == ""


def test_sync_rejects_round_limit_above_sidecar_cap(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["sync", "--round-limit", "21"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "less than or equal to 20" in captured.err


def test_sync_data_dir_file_fails_without_traceback(capsys, tmp_path):
    data_dir = tmp_path / "sidecar-data"
    data_dir.write_text("not a directory", encoding="utf-8")

    exit_code = cli.main(["sync", "--data-dir", str(data_dir)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Failed to prepare sidecar sync lock" in captured.err
    assert "Traceback" not in captured.err
