import importlib
import json

DEPLOY_ENV = {
    "SIDECAR_MODAL_APP_NAME": "validator-scoring-sidecar-testnet",
    "SIDECAR_MODAL_IMAGE": "lmsysorg/sglang:tag@sha256:" + "d" * 64,
    "SIDECAR_MODAL_GPU": "H100",
    "SIDECAR_MODAL_LAUNCH_COMMAND": json.dumps(
        ["python", "-m", "sglang.launch_server"]
    ),
    "SIDECAR_MODAL_LAUNCH_ARGS": json.dumps(["--enable-deterministic-inference"]),
    "SIDECAR_MODAL_ENVIRONMENT": json.dumps({"SGLANG_KEY": "value"}),
    "SIDECAR_MODAL_MODEL_REPO_ID": "Qwen/Qwen3.6-27B-FP8",
    "SIDECAR_MODAL_MODEL_REVISION": "a" * 40,
    "SIDECAR_MODAL_SCALEDOWN_MINUTES": "5",
}


def test_deploy_config_is_baked_for_container_reimport(monkeypatch):
    for key, value in DEPLOY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SIDECAR_MODAL_MODEL_VOLUME", raising=False)

    import validator_scoring_sidecar._modal_app as modal_app

    modal_app = importlib.reload(modal_app)

    # Modal re-imports this module inside the served GPU container, where the
    # deployer's environment does not exist. Every deploy parameter must
    # therefore round-trip through the baked image environment — a missing key
    # crash-loops the container with KeyError at import.
    for key, value in DEPLOY_ENV.items():
        assert modal_app._DEPLOY_CONFIG[key] == value
    assert (
        modal_app._DEPLOY_CONFIG["SIDECAR_MODAL_MODEL_VOLUME"]
        == modal_app.MODEL_VOLUME_NAME
    )
    assert modal_app.MODEL_REVISION == "a" * 40


def test_hf_token_becomes_runtime_secret_and_stays_out_of_baked_env(monkeypatch):
    for key, value in DEPLOY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    import validator_scoring_sidecar._modal_app as modal_app

    modal_app = importlib.reload(modal_app)
    assert modal_app.CONTAINER_SECRETS == []

    monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    modal_app = importlib.reload(modal_app)
    assert len(modal_app.CONTAINER_SECRETS) == 1
    # The credential must never round-trip through the baked image environment.
    assert "HF_TOKEN" not in modal_app._DEPLOY_CONFIG
    assert "HF_TOKEN" not in modal_app.RUNTIME_ENV


def test_scaledown_window_reads_deployed_value(monkeypatch):
    for key, value in DEPLOY_ENV.items():
        monkeypatch.setenv(key, value)

    import validator_scoring_sidecar._modal_app as modal_app

    modal_app = importlib.reload(modal_app)
    assert modal_app.SCALEDOWN_MINUTES == 5

    monkeypatch.setenv("SIDECAR_MODAL_SCALEDOWN_MINUTES", "12")
    modal_app = importlib.reload(modal_app)
    assert modal_app.SCALEDOWN_MINUTES == 12
    assert modal_app._DEPLOY_CONFIG["SIDECAR_MODAL_SCALEDOWN_MINUTES"] == "12"


def test_job_interface_is_defined(monkeypatch):
    for key, value in DEPLOY_ENV.items():
        monkeypatch.setenv(key, value)

    import validator_scoring_sidecar._modal_app as modal_app

    modal_app = importlib.reload(modal_app)
    # The submit/poll control endpoints must exist alongside the web server,
    # and their result statuses are the contract the sidecar backend parses.
    assert modal_app.submit is not None
    assert modal_app.result is not None
    assert modal_app.RESULT_STATUS_PENDING == "pending"
    assert modal_app.RESULT_STATUS_DONE == "done"
    assert modal_app.RESULT_STATUS_FAILED == "failed"
