import importlib
import json

import pytest

pytest.importorskip("modal", reason="the bundled Modal app requires the modal extra")

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
