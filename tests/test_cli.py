import json

import pytest

from validator_scoring_sidecar import __version__, cli
from validator_scoring_sidecar.deployment import (
    DeploymentRecord,
    ModalNotAvailableError,
)
from validator_scoring_sidecar.input_package import (
    FetchedInputPackage,
    InputPackageVerificationError,
)
from validator_scoring_sidecar.sync import SyncLockError, SyncResult


class FakeClient:
    payload = None
    rounds = None
    error = None
    last_config = None

    def __init__(self, config):
        self.config = config
        FakeClient.last_config = config

    def fetch_round(self, round_id):
        if self.error is not None:
            raise self.error
        return dict(self.payload)

    def fetch_rounds(self, *, limit, offset=0):
        if self.error is not None:
            raise self.error
        return [dict(payload) for payload in (self.rounds or [])]

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
    FakeClient.rounds = None
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


def test_version_flag_prints_and_exits(capsys):
    # argparse's version action raises SystemExit(0) after printing.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert captured.out.strip() == f"validator-scoring-sidecar {__version__}"


def test_version_resolves_from_installed_metadata():
    # Proves the importlib.metadata path is taken (an installed package), not the
    # source-tree "0.0.0+unknown" fallback — the version tracks pyproject.toml.
    assert __version__ != "0.0.0+unknown"
    assert __version__[0].isdigit()


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


def _deployment_record():
    return DeploymentRecord(
        mode="modal",
        image="lmsysorg/sglang:nightly@sha256:" + "d" * 64,
        gpu_class="H100",
        tensor_parallelism=1,
        launch_args=["--enable-deterministic-inference"],
        environment={"SGLANG_FLASHINFER_WORKSPACE_SIZE": "2147483648"},
        served_model_name="Qwen/Qwen3.6-27B-FP8",
        model_revision="a" * 40,
        endpoint_url="https://operator--app.modal.run",
        deployed_at="2026-06-01T00:00:00+00:00",
    )


def _write_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"runtime": {}, "model": {}}), encoding="utf-8")
    return manifest_path


def test_deploy_modal_human_output(capsys, monkeypatch, tmp_path):
    manifest_path = _write_manifest(tmp_path)
    record = _deployment_record()

    def fake_deploy(manifest, config, *, deployer, app_name):
        return record

    monkeypatch.setattr(cli, "deploy_modal_endpoint", fake_deploy)

    exit_code = cli.main(
        [
            "deploy-modal",
            "--manifest",
            str(manifest_path),
            "--data-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Mode: modal" in captured.out
    assert "Endpoint URL: https://operator--app.modal.run" in captured.out
    assert "GPU class: H100" in captured.out
    assert str(tmp_path / "runtime" / "deployment_record.json") in captured.out
    assert captured.err == ""


def test_deploy_modal_json_output(capsys, monkeypatch, tmp_path):
    manifest_path = _write_manifest(tmp_path)
    record = _deployment_record()

    monkeypatch.setattr(
        cli,
        "deploy_modal_endpoint",
        lambda manifest, config, *, deployer, app_name: record,
    )

    exit_code = cli.main(
        [
            "deploy-modal",
            "--manifest",
            str(manifest_path),
            "--data-dir",
            str(tmp_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == record.as_dict()
    assert captured.err == ""


def test_deploy_modal_rejects_round_id_with_manifest(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "deploy-modal",
                "--round-id",
                "123",
                "--manifest",
                str(tmp_path / "manifest.json"),
            ]
        )

    assert exc_info.value.code == 2


def test_deploy_modal_error_exits_operator_error(capsys, monkeypatch, tmp_path):
    manifest_path = _write_manifest(tmp_path)

    def fake_deploy(manifest, config, *, deployer, app_name):
        raise ModalNotAvailableError("no Modal login found; run `modal setup`")

    monkeypatch.setattr(cli, "deploy_modal_endpoint", fake_deploy)

    exit_code = cli.main(
        [
            "deploy-modal",
            "--manifest",
            str(manifest_path),
            "--data-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no Modal login" in captured.err
    assert captured.out == ""


def test_deploy_modal_round_id_loads_package_manifest(capsys, monkeypatch, tmp_path):
    runtime_dir = tmp_path / "packages" / ("a" * 64) / "runtime"
    runtime_dir.mkdir(parents=True)
    manifest = {"runtime": {"kind": "modal_sglang"}, "model": {"provider": "huggingface"}}
    (runtime_dir / "execution_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    seen = {}

    def fake_deploy(loaded_manifest, config, *, deployer, app_name):
        seen["manifest"] = loaded_manifest
        return _deployment_record()

    monkeypatch.setattr(cli, "deploy_modal_endpoint", fake_deploy)

    exit_code = cli.main(
        [
            "deploy-modal",
            "--round-id",
            "123",
            "--data-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert seen["manifest"] == manifest
    assert captured.err == ""


def test_deploy_modal_defaults_to_latest_round(capsys, monkeypatch, tmp_path):
    runtime_dir = tmp_path / "packages" / ("a" * 64) / "runtime"
    runtime_dir.mkdir(parents=True)
    manifest = {"runtime": {"kind": "modal_sglang"}, "model": {"provider": "huggingface"}}
    (runtime_dir / "execution_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    FakeClient.rounds = [
        _payload(id=200, round_number=500, input_package_hash=None),
        _payload(id=199, round_number=499),
    ]
    seen = {}

    def fake_deploy(loaded_manifest, config, *, deployer, app_name):
        seen["manifest"] = loaded_manifest
        return _deployment_record()

    monkeypatch.setattr(cli, "deploy_modal_endpoint", fake_deploy)

    exit_code = cli.main(["deploy-modal", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert seen["manifest"] == manifest
    assert captured.err == ""


def test_deploy_modal_no_eligible_round_exits_operator_error(capsys, tmp_path):
    FakeClient.rounds = [_payload(id=200, input_package_cid=None)]

    exit_code = cli.main(["deploy-modal", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no recent round exposes a frozen input package" in captured.err
    assert captured.out == ""


def _local_deployment_record():
    return DeploymentRecord(
        mode="local",
        image="lmsysorg/sglang:nightly@sha256:" + "d" * 64,
        gpu_class="H100",
        tensor_parallelism=1,
        launch_args=["--enable-deterministic-inference"],
        environment={"SGLANG_FLASHINFER_WORKSPACE_SIZE": "2147483648"},
        served_model_name="Qwen/Qwen3.6-27B-FP8",
        model_revision="a" * 40,
        endpoint_url="http://localhost:8000/v1",
        deployed_at="2026-06-01T00:00:00+00:00",
    )


def test_start_sglang_human_output(capsys, monkeypatch, tmp_path):
    manifest_path = _write_manifest(tmp_path)
    record = _local_deployment_record()

    def fake_start(manifest, config, *, starter, gpu_detector, port):
        return record

    monkeypatch.setattr(cli, "start_local_sglang_endpoint", fake_start)

    exit_code = cli.main(
        [
            "start-sglang",
            "--manifest",
            str(manifest_path),
            "--data-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Mode: local" in captured.out
    assert "Endpoint URL: http://localhost:8000/v1" in captured.out
    assert str(tmp_path / "runtime" / "deployment_record.json") in captured.out
    assert captured.err == ""


def test_start_sglang_gpu_mismatch_exits_operator_error(capsys, monkeypatch, tmp_path):
    from validator_scoring_sidecar.deployment import GpuMismatchError

    manifest_path = _write_manifest(tmp_path)

    def fake_start(manifest, config, *, starter, gpu_detector, port):
        raise GpuMismatchError(
            "host GPU 'NVIDIA A100 80GB' does not match the manifest's pinned "
            "GPU class 'H100'"
        )

    monkeypatch.setattr(cli, "start_local_sglang_endpoint", fake_start)

    exit_code = cli.main(
        [
            "start-sglang",
            "--manifest",
            str(manifest_path),
            "--data-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "does not match" in captured.err
    assert captured.out == ""


def _score_result(**overrides):
    from validator_scoring_sidecar.score import ScoreResult

    fields = {
        "status": "scored",
        "network": "testnet",
        "round_id": 123,
        "round_number": 456,
        "sidecar_state": "SCORED",
        "backend_mode": "modal",
        "compared": True,
        "matched_levels": ["RAW_MATCH", "PARSED_MATCH"],
        "error_category": None,
    }
    fields.update(overrides)
    return ScoreResult(**fields)


def test_score_command_human_output(capsys, monkeypatch, tmp_path):
    result = _score_result()
    monkeypatch.setattr(
        cli,
        "score_round",
        lambda config, client, *, round_id, source, round_limit: result,
    )

    exit_code = cli.main(["score", "--round-id", "123", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Score status: scored" in captured.out
    assert "Sidecar state: SCORED" in captured.out
    assert "Matched levels: RAW_MATCH, PARSED_MATCH" in captured.out
    assert captured.err == ""


def test_score_command_json_output(capsys, monkeypatch, tmp_path):
    result = _score_result(status="comparison_pending", compared=False, matched_levels=[])
    monkeypatch.setattr(
        cli,
        "score_round",
        lambda config, client, *, round_id, source, round_limit: result,
    )

    exit_code = cli.main(["score", "--data-dir", str(tmp_path), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == result.as_dict()
    assert captured.err == ""


def test_score_command_deployment_error_exits_operator_error(capsys, monkeypatch, tmp_path):
    from validator_scoring_sidecar.deployment import DeploymentError

    def fake_score(config, client, *, round_id, source, round_limit):
        raise DeploymentError(
            "no deployment record found; run `deploy-modal` or `start-sglang` first"
        )

    monkeypatch.setattr(cli, "score_round", fake_score)

    exit_code = cli.main(["score", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no deployment record" in captured.err
    assert captured.out == ""


def test_warm_runtime_human_output(capsys, monkeypatch, tmp_path):
    from validator_scoring_sidecar.participate import WARM_STATUS_READY, WarmRuntimeResult

    result = WarmRuntimeResult(
        status=WARM_STATUS_READY, endpoint_url="https://operator--app.modal.run"
    )
    monkeypatch.setattr(
        cli,
        "warm_modal_runtime",
        lambda config, client, *, source, round_limit: result,
    )

    exit_code = cli.main(["warm-runtime", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Runtime warm-up: {WARM_STATUS_READY}" in captured.out
    assert "Endpoint URL: https://operator--app.modal.run" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "status, expected_exit",
    [
        ("endpoint_still_starting", 3),
        ("endpoint_unverified", 1),
    ],
)
def test_warm_runtime_unconfirmed_endpoint_exits_nonzero(
    capsys, monkeypatch, tmp_path, status, expected_exit
):
    from validator_scoring_sidecar.participate import WarmRuntimeResult

    result = WarmRuntimeResult(
        status=status, endpoint_url="https://operator--app.modal.run"
    )
    monkeypatch.setattr(
        cli,
        "warm_modal_runtime",
        lambda config, client, *, source, round_limit: result,
    )

    exit_code = cli.main(["warm-runtime", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == expected_exit
    assert f"Runtime warm-up: {status}" in captured.out


def test_warm_runtime_json_output(capsys, monkeypatch, tmp_path):
    from validator_scoring_sidecar.participate import (
        WARM_STATUS_SKIPPED_NO_CREDENTIALS,
        WarmRuntimeResult,
    )

    result = WarmRuntimeResult(status=WARM_STATUS_SKIPPED_NO_CREDENTIALS)
    monkeypatch.setattr(
        cli,
        "warm_modal_runtime",
        lambda config, client, *, source, round_limit: result,
    )

    exit_code = cli.main(["warm-runtime", "--data-dir", str(tmp_path), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == result.as_dict()
    assert captured.err == ""


def test_warm_runtime_deploy_error_exits_operator_error(capsys, monkeypatch, tmp_path):
    from validator_scoring_sidecar.deployment import ModalDeploymentError

    def fake_warm(config, client, *, source, round_limit):
        raise ModalDeploymentError("modal deploy failed")

    monkeypatch.setattr(cli, "warm_modal_runtime", fake_warm)

    exit_code = cli.main(["warm-runtime", "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "modal deploy failed" in captured.err
    assert captured.out == ""
