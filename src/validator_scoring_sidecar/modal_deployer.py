"""Concrete Modal deployer that stands up the bundled SGLang endpoint.

This is the live seam behind the ``ModalDeployer`` protocol in
``deployment.py``. It is the only sidecar module that drives the optional
``modal`` dependency, and it does so lazily so importing the package (and the
unattended sync container) never requires ``modal`` to be installed.

Deployment mirrors the foundation's own flow: configuration is passed by
environment to the bundled ``_modal_app.py`` and applied with ``modal deploy``
under the operator's existing Modal login. The endpoint class name must match
the one defined in ``_modal_app.py``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

from validator_scoring_sidecar.deployment import (
    ModalDeploymentError,
    ModalDeploymentResult,
    ModalNotAvailableError,
    RuntimeSpec,
)

APP_MODULE_PATH = Path(__file__).with_name("_modal_app.py")
ENDPOINT_CLASS_NAME = "SidecarScoringEndpoint"
ENDPOINT_WEB_METHOD = "serve"
# The Modal SDK's own credential variables; the deploy subprocess inherits them.
ENV_MODAL_TOKEN_ID = "MODAL_TOKEN_ID"
ENV_MODAL_TOKEN_SECRET = "MODAL_TOKEN_SECRET"
MODAL_LOGIN_HINT = (
    "no Modal login found; run `modal setup` (or set "
    f"{ENV_MODAL_TOKEN_ID} and {ENV_MODAL_TOKEN_SECRET}) before deploying"
)
MODAL_INSTALL_HINT = (
    "the Modal SDK is not installed; install it with "
    "`pip install validator-scoring-sidecar[modal]`"
)
_DEPLOY_TIMEOUT_SECONDS = 30 * 60
_WEB_URL_PATTERN = re.compile(r"https://[^\s'\"]+\.modal\.run[^\s'\"]*")

ENV_APP_NAME = "SIDECAR_MODAL_APP_NAME"
ENV_IMAGE = "SIDECAR_MODAL_IMAGE"
ENV_GPU = "SIDECAR_MODAL_GPU"
ENV_LAUNCH_COMMAND = "SIDECAR_MODAL_LAUNCH_COMMAND"
ENV_LAUNCH_ARGS = "SIDECAR_MODAL_LAUNCH_ARGS"
ENV_ENVIRONMENT = "SIDECAR_MODAL_ENVIRONMENT"
ENV_MODEL_REPO_ID = "SIDECAR_MODAL_MODEL_REPO_ID"
ENV_MODEL_REVISION = "SIDECAR_MODAL_MODEL_REVISION"


class RealModalDeployer:
    """Deploys the bundled Modal app under the operator's account."""

    def deploy(self, spec: RuntimeSpec, *, app_name: str) -> ModalDeploymentResult:
        self._require_modal_installed()
        self._require_modal_login()
        completed = self._run_modal_deploy(spec, app_name)
        endpoint_url = self._resolve_endpoint_url(app_name, completed.stdout)
        return ModalDeploymentResult(endpoint_url=endpoint_url)

    def _require_modal_installed(self) -> None:
        if importlib.util.find_spec("modal") is None:
            raise ModalNotAvailableError(MODAL_INSTALL_HINT)

    def _require_modal_login(self) -> None:
        if os.environ.get(ENV_MODAL_TOKEN_ID) and os.environ.get(
            ENV_MODAL_TOKEN_SECRET
        ):
            return
        if (Path.home() / ".modal.toml").is_file():
            return
        raise ModalNotAvailableError(MODAL_LOGIN_HINT)

    def _run_modal_deploy(
        self,
        spec: RuntimeSpec,
        app_name: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["modal", "deploy", str(APP_MODULE_PATH)],
                env=self._deploy_environment(spec, app_name),
                capture_output=True,
                text=True,
                timeout=_DEPLOY_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise ModalNotAvailableError(MODAL_INSTALL_HINT) from exc
        except subprocess.TimeoutExpired as exc:
            raise ModalDeploymentError(
                f"`modal deploy` timed out after {_DEPLOY_TIMEOUT_SECONDS}s"
            ) from exc

        if completed.returncode != 0:
            raise ModalDeploymentError(self._deploy_failure_message(completed))
        return completed

    def _deploy_environment(
        self,
        spec: RuntimeSpec,
        app_name: str,
    ) -> dict[str, str]:
        import json

        environment = dict(os.environ)
        environment.update(
            {
                ENV_APP_NAME: app_name,
                ENV_IMAGE: spec.image,
                ENV_GPU: spec.gpu,
                ENV_LAUNCH_COMMAND: json.dumps(spec.launch_command),
                ENV_LAUNCH_ARGS: json.dumps(spec.launch_args),
                ENV_ENVIRONMENT: json.dumps(spec.environment),
                ENV_MODEL_REPO_ID: spec.model_repo_id,
                ENV_MODEL_REVISION: spec.model_revision,
            }
        )
        return environment

    def _resolve_endpoint_url(
        self,
        app_name: str,
        deploy_stdout: str,
    ) -> str:
        url = self._lookup_endpoint_url(app_name)
        if url:
            return url
        match = _WEB_URL_PATTERN.search(deploy_stdout or "")
        if match:
            return match.group(0)
        raise ModalDeploymentError(
            "deployment succeeded but no endpoint URL could be resolved; "
            f"inspect the {app_name!r} app with `modal app list`"
        )

    def _lookup_endpoint_url(self, app_name: str) -> str | None:
        try:
            import modal

            endpoint = modal.Cls.from_name(app_name, ENDPOINT_CLASS_NAME)
            web_method = getattr(endpoint(), ENDPOINT_WEB_METHOD)
            return web_method.get_web_url()
        except Exception:
            return None

    def _deploy_failure_message(
        self,
        completed: subprocess.CompletedProcess[str],
    ) -> str:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = f"`modal deploy` failed with exit code {completed.returncode}"
        if not detail:
            return message
        if re.search(r"auth|token|unauthor|not logged", detail, re.IGNORECASE):
            return f"{message}: {detail}\n{MODAL_LOGIN_HINT}"
        return f"{message}: {detail}"
