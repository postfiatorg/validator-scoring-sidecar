"""Concrete local SGLang starter and GPU detector — the live seam.

This is the only sidecar module that drives Docker, ``nvidia-smi``, and the
optional ``huggingface_hub`` dependency. It runs only on an operator's GPU host.
The pure record-building and GPU-matching logic lives in ``deployment.py`` and
is unit-tested without any of this; importing this module is cheap and does not
require the ``[local]`` extra (``huggingface_hub`` is imported lazily inside the
download step).

Startup mirrors the foundation's runtime: the model weights are pinned to the
manifest's Hugging Face commit by downloading that exact snapshot, the snapshot
is mounted into the manifest's digest-pinned SGLang container, and the container
serves the OpenAI-compatible API locally.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from validator_scoring_sidecar.deployment import (
    LocalRuntimeError,
    LocalStartResult,
    RuntimeSpec,
    resolve_model_path_args,
)

HEALTH_TIMEOUT_SECONDS = 30 * 60
HEALTH_POLL_SECONDS = 5
NVIDIA_SMI_QUERY = ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]
DOCKER_INSTALL_HINT = (
    "Docker is required for the local backend but was not found on PATH"
)
HF_INSTALL_HINT = (
    "huggingface_hub is required for the local backend; install it with "
    "`pip install validator-scoring-sidecar[local]`"
)


def detect_gpu() -> str:
    """Return the host's first GPU device name, or '' if none is detectable."""

    if shutil.which("nvidia-smi") is None:
        return ""
    try:
        completed = subprocess.run(
            NVIDIA_SMI_QUERY,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    lines = completed.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


class RealLocalSglangStarter:
    """Starts the manifest-pinned SGLang container on the local host."""

    def start(self, spec: RuntimeSpec, *, port: int) -> LocalStartResult:
        self._require_docker()
        model_path = self._download_snapshot(spec)
        self._run_container(spec, model_path, port)
        self._wait_for_health(port)
        return LocalStartResult(endpoint_url=f"http://localhost:{port}/v1")

    def _require_docker(self) -> None:
        if shutil.which("docker") is None:
            raise LocalRuntimeError(DOCKER_INSTALL_HINT)

    def _download_snapshot(self, spec: RuntimeSpec) -> str:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise LocalRuntimeError(HF_INSTALL_HINT) from exc
        try:
            return snapshot_download(
                repo_id=spec.model_repo_id,
                revision=spec.model_revision,
            )
        except Exception as exc:  # noqa: BLE001 - surface any download failure
            raise LocalRuntimeError(
                f"failed to download model snapshot "
                f"{spec.model_repo_id}@{spec.model_revision}: {exc}"
            ) from exc

    def _run_container(self, spec: RuntimeSpec, model_path: str, port: int) -> None:
        # snapshot_download returns a snapshots/<commit>/ directory whose files are
        # symlinks into the sibling blobs/ directory of the model cache. Mount the
        # model cache root (which holds both snapshots/ and blobs/) so the symlinks
        # resolve inside the container; --model-path still points at the snapshot
        # subpath. The foundation manifest never pins --host/--port, so the local
        # server values are appended here.
        model_cache_root = Path(model_path).parents[1]
        command = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--gpus",
            "all",
            "--publish",
            f"{port}:{port}",
            "--volume",
            f"{model_cache_root}:{model_cache_root}:ro",
        ]
        for key, value in spec.environment.items():
            command += ["--env", f"{key}={value}"]
        command += [
            spec.image,
            *spec.launch_command,
            *resolve_model_path_args(spec.launch_args, model_path),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True)
        except OSError as exc:
            raise LocalRuntimeError(
                f"failed to launch the SGLang container: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise LocalRuntimeError(f"`docker run` failed: {detail}")

    def _wait_for_health(self, port: int) -> None:
        health_url = f"http://localhost:{port}/health"
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(HEALTH_POLL_SECONDS)
        raise LocalRuntimeError(
            f"local SGLang server did not become healthy within "
            f"{HEALTH_TIMEOUT_SECONDS}s"
        )
