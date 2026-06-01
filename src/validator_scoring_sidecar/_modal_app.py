"""Bundled Modal application for the sidecar's local SGLang inference endpoint.

This module is never imported by the sidecar package. It is the deployment
target the operator's ``modal deploy`` invocation runs (driven by
``modal_deployer.RealModalDeployer``), and it requires the optional ``modal``
dependency plus an operator Modal login.

Every runtime parameter is read from environment variables the deployer sets
from the round's execution manifest, so the served endpoint is a faithful
reproduction of the foundation's pinned runtime: the same digest-pinned image,
GPU class, deterministic launch arguments, and SGLang workspace environment.
The model weights are pinned to the manifest's Hugging Face commit by
downloading that exact snapshot and pointing ``--model-path`` at it.
"""

import json
import os
import subprocess
import time

import modal

SGLANG_PORT = 8000
MINUTES = 60
HF_CACHE_PATH = "/model-cache/huggingface"
STARTUP_TIMEOUT = 35 * MINUTES

# These SIDECAR_MODAL_* variables are set by RealModalDeployer (modal_deployer.py)
# on the `modal deploy` subprocess that runs this file. Operators never set them.
APP_NAME = os.environ["SIDECAR_MODAL_APP_NAME"]
IMAGE_REF = os.environ["SIDECAR_MODAL_IMAGE"]
GPU_TYPE = os.environ["SIDECAR_MODAL_GPU"]
LAUNCH_COMMAND = json.loads(os.environ["SIDECAR_MODAL_LAUNCH_COMMAND"])
LAUNCH_ARGS = json.loads(os.environ["SIDECAR_MODAL_LAUNCH_ARGS"])
MANIFEST_ENVIRONMENT = json.loads(os.environ["SIDECAR_MODAL_ENVIRONMENT"])
MODEL_REPO_ID = os.environ["SIDECAR_MODAL_MODEL_REPO_ID"]
MODEL_REVISION = os.environ["SIDECAR_MODAL_MODEL_REVISION"]
MODEL_VOLUME_NAME = os.environ.get(
    "SIDECAR_MODAL_MODEL_VOLUME",
    f"{APP_NAME}-model-weights",
)

# The manifest environment is reproduced verbatim; HF cache locations are added
# so weight downloads persist in the Modal volume across cold starts.
RUNTIME_ENV = {
    **MANIFEST_ENVIRONMENT,
    "HF_HOME": HF_CACHE_PATH,
    "HF_HUB_CACHE": HF_CACHE_PATH,
}

app = modal.App(name=APP_NAME)

sglang_image = (
    modal.Image.from_registry(IMAGE_REF)
    .entrypoint([])
    .pip_install("huggingface_hub", "hf_xet")
    .env(RUNTIME_ENV)
)

model_volume = modal.Volume.from_name(MODEL_VOLUME_NAME, create_if_missing=True)

with sglang_image.imports():
    import requests


def _download_pinned_snapshot() -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=MODEL_REPO_ID, revision=MODEL_REVISION)


def _resolve_launch_args(model_path: str) -> list[str]:
    """Return the manifest launch args with ``--model-path`` pinned to the
    downloaded snapshot so the served weights match the manifest revision."""

    args = list(LAUNCH_ARGS)
    for index, token in enumerate(args):
        if token == "--model-path" and index + 1 < len(args):
            args[index + 1] = model_path
            return args
        if token.startswith("--model-path="):
            args[index] = f"--model-path={model_path}"
            return args
    return args + ["--model-path", model_path]


def _wait_for_server(timeout: int = 30 * MINUTES) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = requests.get(
                f"http://127.0.0.1:{SGLANG_PORT}/health",
                timeout=5,
            )
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(5)
    raise TimeoutError(f"SGLang server not ready within {timeout}s")


@app.cls(
    image=sglang_image,
    gpu=GPU_TYPE,
    volumes={HF_CACHE_PATH: model_volume},
    timeout=60 * MINUTES,
    scaledown_window=20 * MINUTES,
    max_containers=1,
)
class SidecarScoringEndpoint:
    @modal.enter()
    def start_server(self):
        model_path = _download_pinned_snapshot()
        command = [
            *LAUNCH_COMMAND,
            *_resolve_launch_args(model_path),
            "--host",
            "0.0.0.0",
            "--port",
            str(SGLANG_PORT),
        ]
        print(f"Launching SGLang: {' '.join(command)}", flush=True)
        self.process = subprocess.Popen(command)
        _wait_for_server()

    @modal.web_server(
        port=SGLANG_PORT,
        startup_timeout=STARTUP_TIMEOUT,
        requires_proxy_auth=True,
    )
    def serve(self):
        pass

    @modal.exit()
    def stop(self):
        self.process.terminate()
