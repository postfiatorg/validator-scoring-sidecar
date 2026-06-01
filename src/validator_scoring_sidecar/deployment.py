"""Deployment helpers for the sidecar inference runtime (Modal and local).

Reads a round's ``runtime/execution_manifest.json`` (from the verified input
package cache or an explicit path) and stands up a matching SGLang inference
endpoint on infrastructure the validator controls — either a Modal app under
the operator's own account, or a local SGLang container on the operator's own
H100. Either path writes the local ``runtime/deployment_record.json`` that the
manifest-compatibility checker in ``manifest.py`` consumes before scoring.

Only the runtime the foundation pinned for the round is reproduced: the
digest-pinned container image, GPU class, tensor-parallelism degree,
deterministic launch arguments, and SGLang workspace environment are read
straight from the manifest rather than from local defaults, so the resulting
endpoint is an independent reproduction.

The runtime-specific machinery lives behind the ``ModalDeployer`` and
``LocalRuntimeStarter`` protocols (implemented by ``modal_deployer`` and
``local_runtime``) so this module carries no dependency on the optional
``modal`` / ``huggingface_hub`` packages or on Docker, and stays unit-testable
against a synthesized manifest. The deployment record fields written here are
exactly the ones the compatibility checker reads back, so a record produced by
a successful deploy is the artifact the sidecar later uses to decide, per round,
whether the deployed runtime still matches the foundation's pinned manifest or
has drifted and must be redeployed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeGuard

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.manifest import (
    DETERMINISTIC_INFERENCE_FLAG,
    EXPECTED_MODEL_PROVIDER,
    HEX_DIGITS,
    HF_COMMIT_HASH_LENGTH,
)
from validator_scoring_sidecar.round_metadata import (
    MissingFrozenInputMetadata,
    RoundMetadata,
)

MANIFEST_RELATIVE_PATH = "runtime/execution_manifest.json"
DEPLOYMENT_RECORD_RELATIVE_PATH = "runtime/deployment_record.json"
DEPLOYMENT_MODE_MODAL = "modal"
DEPLOYMENT_MODE_LOCAL = "local"
# The foundation always stamps this kind in the manifest; both the Modal and
# local paths read the same value and reproduce its SGLang runtime.
MANIFEST_RUNTIME_KIND = "modal_sglang"
DEFAULT_LAUNCH_COMMAND = ("python", "-m", "sglang.launch_server")
DEFAULT_LOCAL_PORT = 8000
APP_NAME_PREFIX = "validator-scoring-sidecar"
MODEL_PATH_FLAG = "--model-path"


class DeploymentError(RuntimeError):
    """Base error for deployment operations."""


class ManifestRuntimeError(DeploymentError):
    """Raised when the manifest does not describe a deployable SGLang runtime."""


class ModalNotAvailableError(DeploymentError):
    """Raised when the Modal SDK or an operator Modal login is unavailable."""


class ModalDeploymentError(DeploymentError):
    """Raised when the Modal deployment itself fails."""


class LocalRuntimeError(DeploymentError):
    """Raised when the local SGLang runtime cannot be started."""


class GpuMismatchError(LocalRuntimeError):
    """Raised when the host GPU is missing or not the manifest's pinned class."""


class NoEligibleRoundError(DeploymentError):
    """Raised when no recent round exposes a frozen input package to deploy."""


@dataclass(frozen=True)
class RuntimeSpec:
    """The inference runtime to reproduce, extracted from a round's manifest.

    Shared by the Modal and local paths. Hosting-specific details (the Modal
    app name, the local port) are supplied separately, not derived from the
    manifest.
    """

    image: str
    gpu: str
    tensor_parallelism: int
    launch_command: list[str]
    launch_args: list[str]
    environment: dict[str, str]
    served_model_name: str
    model_repo_id: str
    model_revision: str


@dataclass(frozen=True)
class ModalDeploymentResult:
    """What the Modal deployer observed after standing up the endpoint."""

    endpoint_url: str


@dataclass(frozen=True)
class LocalStartResult:
    """What the local starter observed after standing up the endpoint."""

    endpoint_url: str


class ModalDeployer(Protocol):
    """Stands up a Modal endpoint for a spec and reports where it lives."""

    def deploy(self, spec: RuntimeSpec, *, app_name: str) -> ModalDeploymentResult: ...


class LocalRuntimeStarter(Protocol):
    """Starts a local SGLang container for a spec and reports where it lives."""

    def start(self, spec: RuntimeSpec, *, port: int) -> LocalStartResult: ...


class GpuDetector(Protocol):
    """Returns the host GPU device name, or an empty string if none is found."""

    def __call__(self) -> str: ...


@dataclass(frozen=True)
class DeploymentRecord:
    """Local record of the deployed runtime, consumed by the compat checker.

    The field set mirrors exactly what ``manifest.check_compatibility`` reads
    from the deployment record. ``mode`` is ``modal`` or ``local``. There is no
    ``gpu_mismatch_acknowledged`` field: the local path is strict and only
    records a GPU class it has confirmed matches the manifest.
    """

    mode: str
    image: str
    gpu_class: str
    tensor_parallelism: int
    launch_args: list[str]
    environment: dict[str, str]
    served_model_name: str
    model_revision: str
    endpoint_url: str
    deployed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "image": self.image,
            "gpu_class": self.gpu_class,
            "tensor_parallelism": self.tensor_parallelism,
            "launch_args": list(self.launch_args),
            "environment": dict(self.environment),
            "served_model_name": self.served_model_name,
            "model_revision": self.model_revision,
            "endpoint_url": self.endpoint_url,
            "deployed_at": self.deployed_at,
        }


def default_app_name(network: str) -> str:
    """Stable per-network Modal app name so redeploys replace in place."""

    return f"{APP_NAME_PREFIX}-{network}"


def deployment_record_path(config: SidecarConfig) -> Path:
    """Return the local path the deployment record is written to."""

    return config.data_dir.joinpath(*DEPLOYMENT_RECORD_RELATIVE_PATH.split("/"))


def read_manifest_file(path: Path) -> dict[str, Any]:
    """Load and shallow-validate an execution manifest JSON file."""

    try:
        content = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestRuntimeError(f"manifest file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestRuntimeError(f"manifest file is not valid JSON: {path}") from exc
    if not isinstance(content, dict):
        raise ManifestRuntimeError(f"manifest file must contain a JSON object: {path}")
    return content


def load_round_manifest(package_path: Path) -> dict[str, Any]:
    """Load the execution manifest from a verified input package directory."""

    return read_manifest_file(
        Path(package_path).joinpath(*MANIFEST_RELATIVE_PATH.split("/"))
    )


def select_latest_deployable_round(
    round_payloads: list[dict[str, Any]],
) -> RoundMetadata:
    """Return the newest round payload that exposes frozen input metadata.

    Payloads are expected newest-first, matching the scoring service's
    ``/api/scoring/rounds`` ordering. Rounds without frozen input metadata
    (override, dry-run, or legacy) are skipped, mirroring ``sync`` discovery,
    because deployment needs the runtime pinned in a frozen round's manifest.
    """

    for payload in round_payloads:
        identifier = payload.get("id")
        requested_round_id = (
            identifier if isinstance(identifier, int) and identifier > 0 else 0
        )
        try:
            return RoundMetadata.from_api_payload(
                payload,
                requested_round_id=requested_round_id,
            )
        except MissingFrozenInputMetadata:
            continue
    raise NoEligibleRoundError(
        "no recent round exposes a frozen input package to deploy from; "
        "pass --round-id explicitly or retry after the next scoring round"
    )


def extract_runtime_spec(manifest: dict[str, Any]) -> RuntimeSpec:
    """Validate and extract the deployable SGLang runtime from a manifest.

    Only the ``runtime`` and ``model`` sections are inspected; full
    manifest-compatibility (request, code, canonicalization) is the scoring
    step's concern, enforced later through the deployment record this helper
    produces.
    """

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise ManifestRuntimeError("manifest runtime section must be a JSON object")
    model = manifest.get("model")
    if not isinstance(model, dict):
        raise ManifestRuntimeError("manifest model section must be a JSON object")

    kind = runtime.get("kind")
    if kind != MANIFEST_RUNTIME_KIND:
        raise ManifestRuntimeError(
            f"manifest runtime.kind {kind!r} is not a deployable SGLang runtime "
            f"(expected {MANIFEST_RUNTIME_KIND!r})"
        )

    image = runtime.get("image")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise ManifestRuntimeError(
            "manifest runtime.image must be a digest-pinned reference containing "
            "'@sha256:'"
        )

    gpu = runtime.get("gpu")
    if not isinstance(gpu, str) or not gpu.strip():
        raise ManifestRuntimeError("manifest runtime.gpu must be a non-empty string")

    tensor_parallelism = runtime.get("tensor_parallelism")
    if isinstance(tensor_parallelism, bool) or not isinstance(tensor_parallelism, int):
        raise ManifestRuntimeError(
            "manifest runtime.tensor_parallelism must be an integer"
        )
    if tensor_parallelism <= 0:
        raise ManifestRuntimeError(
            "manifest runtime.tensor_parallelism must be greater than zero"
        )

    launch_args = runtime.get("launch_args")
    if not isinstance(launch_args, list) or not all(
        isinstance(arg, str) for arg in launch_args
    ):
        raise ManifestRuntimeError(
            "manifest runtime.launch_args must be a list of strings"
        )
    if DETERMINISTIC_INFERENCE_FLAG not in launch_args:
        raise ManifestRuntimeError(
            f"manifest runtime.launch_args must include {DETERMINISTIC_INFERENCE_FLAG}"
        )

    launch_command = runtime.get("launch_command", list(DEFAULT_LAUNCH_COMMAND))
    if (
        not isinstance(launch_command, list)
        or not launch_command
        or not all(isinstance(token, str) for token in launch_command)
    ):
        raise ManifestRuntimeError(
            "manifest runtime.launch_command must be a non-empty list of strings"
        )

    environment = runtime.get("environment", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ManifestRuntimeError(
            "manifest runtime.environment must be a JSON object of string values"
        )

    provider = model.get("provider")
    if provider != EXPECTED_MODEL_PROVIDER:
        raise ManifestRuntimeError(
            f"manifest model.provider {provider!r} is not supported "
            f"(expected {EXPECTED_MODEL_PROVIDER!r})"
        )

    repo_id = model.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id.strip():
        raise ManifestRuntimeError("manifest model.repo_id must be a non-empty string")

    served_name = model.get("served_name")
    if not isinstance(served_name, str) or not served_name.strip():
        raise ManifestRuntimeError(
            "manifest model.served_name must be a non-empty string"
        )

    revision = model.get("revision")
    if not _is_full_commit_hash(revision):
        raise ManifestRuntimeError(
            "manifest model.revision must be a full 40-character commit hash"
        )

    return RuntimeSpec(
        image=image,
        gpu=gpu,
        tensor_parallelism=tensor_parallelism,
        launch_command=list(launch_command),
        launch_args=list(launch_args),
        environment=dict(environment),
        served_model_name=served_name,
        model_repo_id=repo_id,
        model_revision=revision,
    )


def match_gpu_class(manifest_gpu: str, detected_name: str | None) -> str:
    """Confirm the host GPU is the manifest's pinned class and return that class.

    The host reports a verbose device name (e.g. ``NVIDIA H100 80GB HBM3``); the
    manifest pins a compact class (``H100``). The match is the compact class
    appearing in the detected name. On success the compact class is returned so
    the deployment record's ``gpu_class`` satisfies the gate's exact-match check.
    The local path is strict: a missing or non-matching GPU is refused.
    """

    if not isinstance(detected_name, str) or not detected_name.strip():
        raise GpuMismatchError(
            f"no GPU detected on this host; the manifest pins {manifest_gpu!r}. "
            "Run the local backend on a matching GPU host."
        )
    if manifest_gpu.strip().lower() in detected_name.strip().lower():
        return manifest_gpu
    raise GpuMismatchError(
        f"host GPU {detected_name.strip()!r} does not match the manifest's pinned "
        f"GPU class {manifest_gpu!r}; the local backend requires matching hardware"
    )


def resolve_model_path_args(launch_args: list[str], model_path: str) -> list[str]:
    """Return launch args with ``--model-path`` pointed at the local snapshot."""

    args = list(launch_args)
    for index, token in enumerate(args):
        if token == MODEL_PATH_FLAG and index + 1 < len(args):
            args[index + 1] = model_path
            return args
        if token.startswith(f"{MODEL_PATH_FLAG}="):
            args[index] = f"{MODEL_PATH_FLAG}={model_path}"
            return args
    return args + [MODEL_PATH_FLAG, model_path]


def build_deployment_record(
    spec: RuntimeSpec,
    *,
    mode: str,
    endpoint_url: str,
    deployed_at: str,
) -> DeploymentRecord:
    """Assemble the deployment record from the spec and resolved endpoint."""

    return DeploymentRecord(
        mode=mode,
        image=spec.image,
        gpu_class=spec.gpu,
        tensor_parallelism=spec.tensor_parallelism,
        launch_args=list(spec.launch_args),
        environment=dict(spec.environment),
        served_model_name=spec.served_model_name,
        model_revision=spec.model_revision,
        endpoint_url=endpoint_url,
        deployed_at=deployed_at,
    )


def deploy_modal_endpoint(
    manifest: dict[str, Any],
    config: SidecarConfig,
    *,
    deployer: ModalDeployer,
    app_name: str | None = None,
    now: str | None = None,
) -> DeploymentRecord:
    """Deploy a manifest-pinned Modal endpoint and persist its record."""

    spec = extract_runtime_spec(manifest)
    resolved_app_name = app_name or default_app_name(config.network)
    result = deployer.deploy(spec, app_name=resolved_app_name)
    if not isinstance(result, ModalDeploymentResult) or not result.endpoint_url.strip():
        raise ModalDeploymentError("Modal deployment did not return an endpoint URL")
    record = build_deployment_record(
        spec,
        mode=DEPLOYMENT_MODE_MODAL,
        endpoint_url=result.endpoint_url,
        deployed_at=now if now is not None else _utc_now(),
    )
    _write_deployment_record(record, config)
    return record


def start_local_sglang_endpoint(
    manifest: dict[str, Any],
    config: SidecarConfig,
    *,
    starter: LocalRuntimeStarter,
    gpu_detector: GpuDetector,
    port: int = DEFAULT_LOCAL_PORT,
    now: str | None = None,
) -> DeploymentRecord:
    """Start a manifest-pinned local SGLang endpoint and persist its record."""

    spec = extract_runtime_spec(manifest)
    match_gpu_class(spec.gpu, gpu_detector())
    result = starter.start(spec, port=port)
    if not isinstance(result, LocalStartResult) or not result.endpoint_url.strip():
        raise LocalRuntimeError("local SGLang startup did not return an endpoint URL")
    record = build_deployment_record(
        spec,
        mode=DEPLOYMENT_MODE_LOCAL,
        endpoint_url=result.endpoint_url,
        deployed_at=now if now is not None else _utc_now(),
    )
    _write_deployment_record(record, config)
    return record


def _write_deployment_record(
    record: DeploymentRecord,
    config: SidecarConfig,
) -> None:
    target = deployment_record_path(config)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        temp_path.write_text(
            json.dumps(record.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(target)
    except OSError as exc:
        raise DeploymentError(
            f"Failed to write deployment record at {target}: {exc}"
        ) from exc


def _is_full_commit_hash(value: Any) -> TypeGuard[str]:
    if not isinstance(value, str) or len(value) != HF_COMMIT_HASH_LENGTH:
        return False
    return all(char in HEX_DIGITS for char in value.lower())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
