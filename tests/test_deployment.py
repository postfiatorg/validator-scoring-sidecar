import json

import pytest

from validator_scoring_sidecar.config import (
    DEFAULT_MODAL_SCALEDOWN_MINUTES,
    load_config,
)
from validator_scoring_sidecar.deployment import (
    GpuMismatchError,
    LocalRuntimeError,
    LocalStartResult,
    ManifestRuntimeError,
    ModalDeploymentError,
    ModalDeploymentResult,
    NoEligibleRoundError,
    default_app_name,
    deploy_modal_endpoint,
    deployment_record_path,
    extract_runtime_spec,
    load_round_manifest,
    match_gpu_class,
    read_manifest_file,
    resolve_model_path_args,
    select_latest_deployable_round,
    start_local_sglang_endpoint,
)
from validator_scoring_sidecar.manifest import check_compatibility
from validator_scoring_sidecar.modal_deployer import (
    ENV_APP_NAME,
    ENV_SCALEDOWN_MINUTES,
    RealModalDeployer,
)
from validator_scoring_sidecar.scoring import (
    SUPPORTED_PARSER_CONTENT_HASHES,
    SUPPORTED_SELECTOR_CONTENT_HASHES,
)

IMAGE_REF = "lmsysorg/sglang:nightly-dev@sha256:" + "d" * 64
MODEL_ID = "Qwen/Qwen3.6-27B-FP8"
MODEL_REVISION = "a" * 40
H100_DEVICE_NAME = "NVIDIA H100 80GB HBM3"


class FakeDeployer:
    def __init__(self, endpoint_url="https://operator--app.modal.run"):
        self.endpoint_url = endpoint_url
        self.spec = None
        self.app_name = None
        self.scaledown_minutes = None

    def deploy(self, spec, *, app_name, scaledown_minutes):
        self.spec = spec
        self.app_name = app_name
        self.scaledown_minutes = scaledown_minutes
        return ModalDeploymentResult(endpoint_url=self.endpoint_url)


class FakeStarter:
    def __init__(self, endpoint_url="http://localhost:8000/v1"):
        self.endpoint_url = endpoint_url
        self.spec = None
        self.port = None
        self.started = False

    def start(self, spec, *, port):
        self.started = True
        self.spec = spec
        self.port = port
        return LocalStartResult(endpoint_url=self.endpoint_url)


def _config(tmp_path):
    return load_config(
        base_url="https://scoring.example.org",
        data_dir=tmp_path,
        network="testnet",
        environ={},
    )


def _manifest(**overrides):
    manifest = {
        "schema_version": 1,
        "round": {
            "kind": "normal",
            "network": "testnet",
            "round_number": 456,
            "inference_performed": True,
        },
        "model": {
            "provider": "huggingface",
            "repo_id": MODEL_ID,
            "served_name": MODEL_ID,
            "revision": MODEL_REVISION,
        },
        "runtime": {
            "kind": "modal_sglang",
            "image": IMAGE_REF,
            "gpu": "H100",
            "tensor_parallelism": 1,
            "launch_command": ["python", "-m", "sglang.launch_server"],
            "launch_args": [
                "--model-path",
                MODEL_ID,
                "--served-model-name",
                MODEL_ID,
                "--tp",
                "1",
                "--enable-deterministic-inference",
            ],
            "environment": {"SGLANG_FLASHINFER_WORKSPACE_SIZE": "2147483648"},
        },
        "request": {
            "type": "openai_chat_completions",
            "method": "chat.completions.create",
            "model": MODEL_ID,
            "temperature": 0,
            "max_tokens": 16384,
            "response_format": {"type": "json_object"},
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        "code": {
            "repository": "postfiatorg/dynamic-unl-scoring",
            "commit": "c" * 40,
            "parser": {"content_sha256": next(iter(SUPPORTED_PARSER_CONTENT_HASHES))},
            "selector": {
                "content_sha256": next(iter(SUPPORTED_SELECTOR_CONTENT_HASHES))
            },
        },
        "canonicalization": {
            "hash_algorithm": "sha256",
            "text_encoding": "utf-8",
            "json_encoding": {"sort_keys": True, "separators": [",", ":"]},
        },
    }
    manifest.update(overrides)
    return manifest


def _round_payload(**overrides):
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


# ---------------------------------------------------------------------------
# Runtime spec extraction (shared by Modal and local)
# ---------------------------------------------------------------------------


def test_extract_runtime_spec_reads_runtime_and_model():
    spec = extract_runtime_spec(_manifest())

    assert spec.image == IMAGE_REF
    assert spec.gpu == "H100"
    assert spec.tensor_parallelism == 1
    assert spec.launch_command == ["python", "-m", "sglang.launch_server"]
    assert "--enable-deterministic-inference" in spec.launch_args
    assert spec.environment == {"SGLANG_FLASHINFER_WORKSPACE_SIZE": "2147483648"}
    assert spec.served_model_name == MODEL_ID
    assert spec.model_repo_id == MODEL_ID
    assert spec.model_revision == MODEL_REVISION


@pytest.mark.parametrize(
    "mutation",
    [
        {"runtime": {"kind": "modal_vllm"}},
        {"runtime": "not-an-object"},
        {"model": "not-an-object"},
    ],
)
def test_extract_runtime_spec_rejects_structural_problems(mutation):
    manifest = _manifest()
    manifest.update(mutation)
    with pytest.raises(ManifestRuntimeError):
        extract_runtime_spec(manifest)


def test_extract_runtime_spec_requires_digest_pinned_image():
    manifest = _manifest()
    manifest["runtime"]["image"] = "lmsysorg/sglang:nightly-dev"
    with pytest.raises(ManifestRuntimeError, match="@sha256:"):
        extract_runtime_spec(manifest)


def test_extract_runtime_spec_requires_full_commit_revision():
    manifest = _manifest()
    manifest["model"]["revision"] = "main"
    with pytest.raises(ManifestRuntimeError, match="40-character"):
        extract_runtime_spec(manifest)


def test_extract_runtime_spec_requires_deterministic_flag():
    manifest = _manifest()
    manifest["runtime"]["launch_args"] = ["--model-path", MODEL_ID]
    with pytest.raises(ManifestRuntimeError, match="enable-deterministic-inference"):
        extract_runtime_spec(manifest)


# ---------------------------------------------------------------------------
# Modal deployment
# ---------------------------------------------------------------------------


def test_deploy_modal_endpoint_writes_record(tmp_path):
    config = _config(tmp_path)
    deployer = FakeDeployer()

    record = deploy_modal_endpoint(
        _manifest(),
        config,
        deployer=deployer,
        app_name="my-app",
        now="2026-06-01T00:00:00+00:00",
    )

    assert deployer.app_name == "my-app"
    assert deployer.scaledown_minutes == DEFAULT_MODAL_SCALEDOWN_MINUTES
    assert record.mode == "modal"
    assert record.endpoint_url == "https://operator--app.modal.run"
    assert record.deployed_at == "2026-06-01T00:00:00+00:00"

    path = deployment_record_path(config)
    assert path == tmp_path / "runtime" / "deployment_record.json"
    assert json.loads(path.read_text(encoding="utf-8")) == record.as_dict()


def test_deploy_modal_endpoint_passes_configured_scaledown(tmp_path):
    config = load_config(
        base_url="https://scoring.example.org",
        data_dir=tmp_path,
        network="testnet",
        environ={"POSTFIAT_SIDECAR_MODAL_SCALEDOWN_MINUTES": "12"},
    )
    deployer = FakeDeployer()

    deploy_modal_endpoint(_manifest(), config, deployer=deployer)

    assert deployer.scaledown_minutes == 12


def test_real_deployer_stamps_scaledown_into_subprocess_env():
    spec = extract_runtime_spec(_manifest())
    environment = RealModalDeployer()._deploy_environment(spec, "my-app", 7)

    assert environment[ENV_SCALEDOWN_MINUTES] == "7"
    assert environment[ENV_APP_NAME] == "my-app"


def test_deploy_modal_endpoint_defaults_app_name_to_network(tmp_path):
    deployer = FakeDeployer()

    deploy_modal_endpoint(_manifest(), _config(tmp_path), deployer=deployer)

    assert deployer.app_name == default_app_name("testnet")
    assert deployer.app_name == "validator-scoring-sidecar-testnet"


def test_deploy_record_passes_compatibility_checker(tmp_path):
    manifest = _manifest()

    record = deploy_modal_endpoint(manifest, _config(tmp_path), deployer=FakeDeployer())

    result = check_compatibility(
        manifest,
        record.as_dict(),
        sidecar_network="testnet",
        expected_round_number=456,
    )
    assert result.passed
    assert result.effective_mode == "modal"


def test_deploy_modal_endpoint_rejects_empty_endpoint_url(tmp_path):
    with pytest.raises(ModalDeploymentError):
        deploy_modal_endpoint(
            _manifest(),
            _config(tmp_path),
            deployer=FakeDeployer(endpoint_url="   "),
        )


# ---------------------------------------------------------------------------
# GPU matching and model-path resolution (local helpers)
# ---------------------------------------------------------------------------


def test_match_gpu_class_accepts_verbose_device_name():
    assert match_gpu_class("H100", H100_DEVICE_NAME) == "H100"


def test_match_gpu_class_rejects_different_gpu():
    with pytest.raises(GpuMismatchError):
        match_gpu_class("H100", "NVIDIA A100 80GB")


@pytest.mark.parametrize("detected", ["", "   ", None])
def test_match_gpu_class_rejects_missing_gpu(detected):
    with pytest.raises(GpuMismatchError):
        match_gpu_class("H100", detected)


def test_resolve_model_path_args_space_form():
    args = ["--model-path", MODEL_ID, "--tp", "1"]
    assert resolve_model_path_args(args, "/snap") == ["--model-path", "/snap", "--tp", "1"]


def test_resolve_model_path_args_equals_form():
    args = [f"--model-path={MODEL_ID}", "--tp", "1"]
    assert resolve_model_path_args(args, "/snap") == ["--model-path=/snap", "--tp", "1"]


def test_resolve_model_path_args_appends_when_absent():
    args = ["--tp", "1"]
    assert resolve_model_path_args(args, "/snap") == ["--tp", "1", "--model-path", "/snap"]


# ---------------------------------------------------------------------------
# Local SGLang startup
# ---------------------------------------------------------------------------


def test_start_local_sglang_endpoint_writes_local_record(tmp_path):
    config = _config(tmp_path)
    starter = FakeStarter()

    record = start_local_sglang_endpoint(
        _manifest(),
        config,
        starter=starter,
        gpu_detector=lambda: H100_DEVICE_NAME,
        port=8000,
        now="2026-06-01T00:00:00+00:00",
    )

    assert starter.port == 8000
    assert record.mode == "local"
    assert record.gpu_class == "H100"
    assert record.endpoint_url == "http://localhost:8000/v1"
    assert record.deployed_at == "2026-06-01T00:00:00+00:00"

    path = deployment_record_path(config)
    assert json.loads(path.read_text(encoding="utf-8")) == record.as_dict()


def test_start_local_record_passes_compatibility_checker(tmp_path):
    manifest = _manifest()

    record = start_local_sglang_endpoint(
        manifest,
        _config(tmp_path),
        starter=FakeStarter(),
        gpu_detector=lambda: H100_DEVICE_NAME,
    )

    result = check_compatibility(
        manifest,
        record.as_dict(),
        sidecar_network="testnet",
        expected_round_number=456,
    )
    assert result.passed
    assert result.effective_mode == "local"


def test_start_local_refuses_and_skips_start_on_gpu_mismatch(tmp_path):
    config = _config(tmp_path)
    starter = FakeStarter()

    with pytest.raises(GpuMismatchError):
        start_local_sglang_endpoint(
            _manifest(),
            config,
            starter=starter,
            gpu_detector=lambda: "NVIDIA A100 80GB",
        )

    assert starter.started is False
    assert not deployment_record_path(config).exists()


def test_start_local_rejects_empty_endpoint_url(tmp_path):
    with pytest.raises(LocalRuntimeError):
        start_local_sglang_endpoint(
            _manifest(),
            _config(tmp_path),
            starter=FakeStarter(endpoint_url="   "),
            gpu_detector=lambda: H100_DEVICE_NAME,
        )


# ---------------------------------------------------------------------------
# Manifest loading and round selection
# ---------------------------------------------------------------------------


def test_load_round_manifest_reads_from_verified_package(tmp_path):
    package_path = tmp_path / "packages" / ("a" * 64)
    runtime_dir = package_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "execution_manifest.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )

    manifest = load_round_manifest(package_path)

    assert manifest["round"]["round_number"] == 456


def test_read_manifest_file_missing_raises(tmp_path):
    with pytest.raises(ManifestRuntimeError, match="not found"):
        read_manifest_file(tmp_path / "missing.json")


def test_read_manifest_file_invalid_json_raises(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestRuntimeError, match="not valid JSON"):
        read_manifest_file(path)


def test_select_latest_deployable_round_picks_newest_eligible():
    payloads = [
        _round_payload(id=130, input_package_hash=None),
        _round_payload(id=129, input_package_hash="b" * 64),
        _round_payload(id=128),
    ]

    metadata = select_latest_deployable_round(payloads)

    assert metadata.round_id == 129
    assert metadata.input_package_hash == "b" * 64


def test_select_latest_deployable_round_raises_when_none_eligible():
    payloads = [_round_payload(id=130, input_package_cid=None)]

    with pytest.raises(NoEligibleRoundError):
        select_latest_deployable_round(payloads)


def test_select_latest_deployable_round_raises_on_empty_list():
    with pytest.raises(NoEligibleRoundError):
        select_latest_deployable_round([])
