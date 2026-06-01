"""Modal deployment helper for the sidecar inference runtime.

Reads a round's ``runtime/execution_manifest.json`` (from the verified input
package cache or an explicit path), deploys a matching SGLang inference
endpoint under the operator's own Modal account, and writes the local
``runtime/deployment_record.json`` that the manifest-compatibility checker in
``manifest.py`` consumes before scoring.

Only the runtime the foundation pinned for the round is reproduced: the
digest-pinned container image, GPU class, tensor-parallelism degree,
deterministic launch arguments, and SGLang workspace environment are read
straight from the manifest rather than from local defaults, so the resulting
endpoint is an independent reproduction on validator-owned infrastructure.

The Modal SDK interaction lives behind the ``ModalDeployer`` protocol
(implemented by ``modal_deployer.RealModalDeployer``) so this module carries no
dependency on the optional ``modal`` package and stays unit-testable against a
synthesized manifest. The deployment record fields written here are exactly the
ones the compatibility checker reads back, so a record produced by a successful
deploy is the artifact the sidecar later uses to decide, per round, whether the
deployed runtime still matches the foundation's pinned manifest or has drifted
and must be redeployed.
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
MODAL_RUNTIME_KIND = "modal_sglang"
DEFAULT_LAUNCH_COMMAND = ("python", "-m", "sglang.launch_server")
APP_NAME_PREFIX = "validator-scoring-sidecar"


class DeploymentError(RuntimeError):
    """Base error for Modal deployment operations."""


class ManifestRuntimeError(DeploymentError):
    """Raised when the manifest does not describe a deployable Modal runtime."""


class ModalNotAvailableError(DeploymentError):
    """Raised when the Modal SDK or an operator Modal login is unavailable."""


class ModalDeploymentError(DeploymentError):
    """Raised when the Modal deployment itself fails."""


class NoEligibleRoundError(DeploymentError):
    """Raised when no recent round exposes a frozen input package to deploy."""


@dataclass(frozen=True)
class ModalDeploymentSpec:
    """Runtime to reproduce on Modal, extracted from a round's manifest."""

    app_name: str
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
    """What the deployer observed after standing up the endpoint."""

    endpoint_url: str


class ModalDeployer(Protocol):
    """Stands up a Modal endpoint for a spec and reports where it lives."""

    def deploy(self, spec: ModalDeploymentSpec) -> ModalDeploymentResult: ...


@dataclass(frozen=True)
class DeploymentRecord:
    """Local record of the deployed runtime, consumed by the compat checker.

    The field set mirrors exactly what ``manifest.check_compatibility`` reads
    from the deployment record. ``gpu_mismatch_acknowledged`` is intentionally
    absent: it is the local-mode ``--allow-gpu-mismatch`` escape hatch and has
    no meaning for a Modal deployment, where the GPU class is whatever the
    manifest pinned.
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


def extract_modal_spec(
    manifest: dict[str, Any],
    *,
    app_name: str,
) -> ModalDeploymentSpec:
    """Validate and extract the deployable Modal runtime from a manifest.

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
    if kind != MODAL_RUNTIME_KIND:
        raise ManifestRuntimeError(
            f"manifest runtime.kind {kind!r} is not deployable as a Modal SGLang "
            f"endpoint (expected {MODAL_RUNTIME_KIND!r})"
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

    return ModalDeploymentSpec(
        app_name=app_name,
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


def build_deployment_record(
    spec: ModalDeploymentSpec,
    result: ModalDeploymentResult,
    *,
    deployed_at: str,
) -> DeploymentRecord:
    """Assemble the deployment record from the spec and deploy result."""

    return DeploymentRecord(
        mode=DEPLOYMENT_MODE_MODAL,
        image=spec.image,
        gpu_class=spec.gpu,
        tensor_parallelism=spec.tensor_parallelism,
        launch_args=list(spec.launch_args),
        environment=dict(spec.environment),
        served_model_name=spec.served_model_name,
        model_revision=spec.model_revision,
        endpoint_url=result.endpoint_url,
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

    resolved_app_name = app_name or default_app_name(config.network)
    spec = extract_modal_spec(manifest, app_name=resolved_app_name)
    result = deployer.deploy(spec)
    if not isinstance(result, ModalDeploymentResult) or not result.endpoint_url.strip():
        raise ModalDeploymentError(
            "Modal deployment did not return an endpoint URL"
        )
    record = build_deployment_record(
        spec,
        result,
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
