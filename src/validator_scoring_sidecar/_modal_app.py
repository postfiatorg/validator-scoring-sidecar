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
# In-container read timeout for one spawned generation; there is no middlebox
# inside the container, so this only guards against a hung server.
GENERATE_TIMEOUT = 30 * MINUTES

# These SIDECAR_MODAL_* variables are set by RealModalDeployer (modal_deployer.py)
# on the `modal deploy` subprocess that runs this file. Operators never set them.
# Modal re-imports this module inside the served container, where the deployer's
# environment does not exist — so the same values are baked into the image env
# below, and the in-container import reads back exactly what was deployed.
_DEPLOY_CONFIG = {
    name: os.environ[name]
    for name in (
        "SIDECAR_MODAL_APP_NAME",
        "SIDECAR_MODAL_IMAGE",
        "SIDECAR_MODAL_GPU",
        "SIDECAR_MODAL_LAUNCH_COMMAND",
        "SIDECAR_MODAL_LAUNCH_ARGS",
        "SIDECAR_MODAL_ENVIRONMENT",
        "SIDECAR_MODAL_MODEL_REPO_ID",
        "SIDECAR_MODAL_MODEL_REVISION",
        "SIDECAR_MODAL_SCALEDOWN_MINUTES",
    )
}
APP_NAME = _DEPLOY_CONFIG["SIDECAR_MODAL_APP_NAME"]
IMAGE_REF = _DEPLOY_CONFIG["SIDECAR_MODAL_IMAGE"]
GPU_TYPE = _DEPLOY_CONFIG["SIDECAR_MODAL_GPU"]
LAUNCH_COMMAND = json.loads(_DEPLOY_CONFIG["SIDECAR_MODAL_LAUNCH_COMMAND"])
LAUNCH_ARGS = json.loads(_DEPLOY_CONFIG["SIDECAR_MODAL_LAUNCH_ARGS"])
MANIFEST_ENVIRONMENT = json.loads(_DEPLOY_CONFIG["SIDECAR_MODAL_ENVIRONMENT"])
MODEL_REPO_ID = _DEPLOY_CONFIG["SIDECAR_MODAL_MODEL_REPO_ID"]
MODEL_REVISION = _DEPLOY_CONFIG["SIDECAR_MODAL_MODEL_REVISION"]
MODEL_VOLUME_NAME = os.environ.get(
    "SIDECAR_MODAL_MODEL_VOLUME",
    f"{APP_NAME}-model-weights",
)
_DEPLOY_CONFIG["SIDECAR_MODAL_MODEL_VOLUME"] = MODEL_VOLUME_NAME
# Idle GPU-billing minutes before scale-to-zero; operator-tuned via
# POSTFIAT_SIDECAR_MODAL_SCALEDOWN_MINUTES (validated in config.py).
SCALEDOWN_MINUTES = int(_DEPLOY_CONFIG["SIDECAR_MODAL_SCALEDOWN_MINUTES"])

# Optional Hugging Face token for authenticated (unthrottled) weight downloads;
# huggingface_hub reads HF_TOKEN from the environment on its own. Injected as a
# Modal Secret at container runtime and kept out of _DEPLOY_CONFIG: the baked
# image environment is recorded in the image definition, where a credential
# must never land.
HF_TOKEN = os.environ.get("HF_TOKEN", "")
CONTAINER_SECRETS = (
    [modal.Secret.from_dict({"HF_TOKEN": HF_TOKEN})] if HF_TOKEN else []
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
    .env({**RUNTIME_ENV, **_DEPLOY_CONFIG})
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
    secrets=CONTAINER_SECRETS,
    timeout=60 * MINUTES,
    scaledown_window=SCALEDOWN_MINUTES * MINUTES,
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

    @modal.method()
    def generate(self, body: dict) -> dict:
        """Run one chat completion against the in-container SGLang server.

        Spawned by the ``submit`` endpoint so the generation runs (and its
        result is retained by Modal) independent of any client connection —
        the job half of the sidecar's submit-and-poll transport. The request
        body arrives verbatim from the sidecar's frozen model request.
        """

        response = requests.post(
            f"http://127.0.0.1:{SGLANG_PORT}/v1/chat/completions",
            json=body,
            timeout=GENERATE_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    @modal.exit()
    def stop(self):
        self.process.terminate()


# Control-plane endpoints for the submit-and-poll transport. They run on a
# slim CPU image so a submit or poll never waits on (or bills) the GPU
# container; the GPU cold start begins when the spawned `generate` call is
# scheduled. Proxy auth uses the same Modal key/secret pair as `serve`. The
# deploy config is baked in because Modal re-imports this module inside the
# control containers too, where the deployer's environment does not exist.
control_image = (
    modal.Image.debian_slim().pip_install("fastapi[standard]").env(_DEPLOY_CONFIG)
)

RESULT_STATUS_PENDING = "pending"
RESULT_STATUS_DONE = "done"
RESULT_STATUS_FAILED = "failed"


@app.function(image=control_image, timeout=60)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def submit(body: dict) -> dict:
    call = SidecarScoringEndpoint().generate.spawn(body)
    return {"call_id": call.object_id}


@app.function(image=control_image, timeout=60)
@modal.fastapi_endpoint(method="GET", requires_proxy_auth=True)
def result(call_id: str) -> dict:
    call = modal.FunctionCall.from_id(call_id)
    try:
        payload = call.get(timeout=0)
    except TimeoutError:
        return {"status": RESULT_STATUS_PENDING}
    except Exception as exc:  # noqa: BLE001 - expired ids and remote failures alike must reach the client as data
        return {"status": RESULT_STATUS_FAILED, "error": str(exc)}
    return {"status": RESULT_STATUS_DONE, "response": payload}
