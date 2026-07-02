"""Inference backends that re-run a frozen scoring round on validator hardware.

The sidecar reproduces a scoring round by sending the round's frozen
``inputs/model_request.json`` to an inference endpoint the operator stood up
(see ``deployment.py`` and ``docs/Deployment.md``) and capturing the raw model
response. The request is submitted exactly as the foundation froze it — the
same fields, no normalization or default-filling — so the response is a
faithful independent reproduction that a later stage can compare against the
foundation's output.

Both backends share one OpenAI-compatible ``POST /v1/chat/completions`` path
(``_ChatCompletionsBackend``), so they issue a byte-identical request; this
matters because a divergence in how the frozen request reaches the model would
surface as a false disagreement. ``ModalBackend`` adds Modal ``Modal-Key`` /
``Modal-Secret`` proxy-auth headers and targets the operator's deployed Modal
app; ``LocalSglangBackend`` targets a local SGLang server (localhost by default)
and sends no auth headers. Both stop at the raw response: parsing, selection,
hashing, comparison, and persistence belong to later stages, and both classify
failures with the shared ``FailureCategory`` vocabulary so downstream
convergence reporting reads one taxonomy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from validator_scoring_sidecar.failure import Failure, FailureCategory

ENV_MODAL_KEY = "POSTFIAT_SIDECAR_MODAL_KEY"
ENV_MODAL_SECRET = "POSTFIAT_SIDECAR_MODAL_SECRET"
ENV_LOCAL_ENDPOINT_URL = "POSTFIAT_SIDECAR_LOCAL_ENDPOINT_URL"
MODEL_REQUEST_RELATIVE_PATH = "inputs/model_request.json"
BACKEND_MODE_MODAL = "modal"
BACKEND_MODE_LOCAL = "local"
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 180.0
DEFAULT_INFERENCE_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_INFERENCE_WRITE_TIMEOUT_SECONDS = 10.0
DEFAULT_INFERENCE_POOL_TIMEOUT_SECONDS = 5.0
DEFAULT_LOCAL_ENDPOINT_URL = "http://localhost:8000/v1"
PROXY_AUTH_KEY_HEADER = "Modal-Key"
PROXY_AUTH_SECRET_HEADER = "Modal-Secret"
CHAT_COMPLETIONS_SUFFIX = "chat/completions"

# Fields forwarded verbatim from the frozen request, matching the foundation's
# ModalClient.score_request selection. extra_body is merged at the top level,
# which is how the OpenAI client serializes it on the wire.
REQUEST_BODY_KEYS = (
    "model",
    "messages",
    "temperature",
    "max_tokens",
    "response_format",
)


class InferenceConfigError(RuntimeError):
    """Raised when the backend lacks required setup (endpoint or credentials)."""


class ModelRequestError(RuntimeError):
    """Raised when the frozen model request cannot be loaded."""


class InferenceError(RuntimeError):
    """Raised when an inference attempt fails, carrying a shared failure category."""

    def __init__(
        self,
        category: FailureCategory,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        self.failure = Failure(category=category, message=message, details=details or {})
        super().__init__(message)

    @property
    def category(self) -> FailureCategory:
        return self.failure.category


@dataclass(frozen=True)
class InferenceResult:
    """Raw model response from one inference attempt.

    ``content`` is the model output text the parser consumes; ``response_payload``
    is the full OpenAI-compatible response body, preserved for the later
    output-hashing and comparison stage.
    """

    content: str
    response_payload: dict[str, Any]


class InferenceBackend(Protocol):
    """Submits a frozen model request and returns the raw model response."""

    backend_mode: str

    def run(self, model_request: dict[str, Any]) -> InferenceResult: ...

    def close(self) -> None: ...


def inference_timeout(read_timeout_seconds: float | None = None) -> httpx.Timeout:
    """Build the explicit timeout used for one inference request."""

    read_timeout = (
        DEFAULT_INFERENCE_TIMEOUT_SECONDS
        if read_timeout_seconds is None
        else max(float(read_timeout_seconds), 0.001)
    )
    return httpx.Timeout(
        connect=DEFAULT_INFERENCE_CONNECT_TIMEOUT_SECONDS,
        read=read_timeout,
        write=DEFAULT_INFERENCE_WRITE_TIMEOUT_SECONDS,
        pool=DEFAULT_INFERENCE_POOL_TIMEOUT_SECONDS,
    )


def load_model_request(package_path: Path) -> dict[str, Any]:
    """Load the frozen ``inputs/model_request.json`` from a verified package."""

    target = Path(package_path).joinpath(*MODEL_REQUEST_RELATIVE_PATH.split("/"))
    try:
        content = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModelRequestError(f"frozen model request not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise ModelRequestError(
            f"frozen model request is not valid JSON: {target}"
        ) from exc
    if not isinstance(content, dict):
        raise ModelRequestError(
            f"frozen model request must be a JSON object: {target}"
        )
    return content


class _ChatCompletionsBackend:
    """Shared OpenAI-compatible chat-completions path for inference backends.

    Subclasses set ``backend_mode`` and supply the endpoint and any request
    headers; the frozen-request body construction, the HTTP call, failure
    classification, and response extraction are identical so the two backends
    issue a byte-identical request.
    """

    backend_mode: str = ""

    def __init__(
        self,
        endpoint_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ):
        self._url = _chat_completions_url(endpoint_url)
        self._headers = dict(headers or {})
        # Modal answers requests that run past ~150s (cold starts, long
        # inference) with a 303 redirect chain to a result-polling URL; the
        # foundation's OpenAI-SDK client follows it by default and this client
        # must match, or every cold-start round fails with INFERENCE_ERROR 303.
        self._http = http_client or httpx.Client(
            timeout=inference_timeout(timeout_seconds), follow_redirects=True
        )
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def run(self, model_request: dict[str, Any]) -> InferenceResult:
        body = _build_request_body(model_request)
        try:
            response = self._http.post(self._url, json=body, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise InferenceError(
                FailureCategory.INFERENCE_TIMEOUT,
                f"inference request to {self._url} timed out: {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise InferenceError(
                FailureCategory.RUNTIME_UNAVAILABLE,
                f"could not reach inference endpoint at {self._url}: {exc}",
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise InferenceError(
                FailureCategory.INFERENCE_ERROR,
                f"inference endpoint returned HTTP {response.status_code} "
                f"for {self._url}",
                details={"status_code": response.status_code},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise InferenceError(
                FailureCategory.INFERENCE_ERROR,
                f"inference endpoint returned invalid JSON for {self._url}",
            ) from exc

        return InferenceResult(
            content=_extract_content(payload),
            response_payload=payload,
        )


class ModalBackend(_ChatCompletionsBackend):
    """Calls an operator-deployed Modal SGLang endpoint over its OpenAI API."""

    backend_mode = BACKEND_MODE_MODAL

    def __init__(
        self,
        endpoint_url: str,
        *,
        proxy_auth_key: str | None,
        proxy_auth_secret: str | None,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ):
        key = (proxy_auth_key or "").strip()
        secret = (proxy_auth_secret or "").strip()
        if not key or not secret:
            raise InferenceConfigError(
                f"{ENV_MODAL_KEY} and {ENV_MODAL_SECRET} are required to call the "
                "deployed Modal endpoint"
            )
        super().__init__(
            endpoint_url,
            headers={
                PROXY_AUTH_KEY_HEADER: key,
                PROXY_AUTH_SECRET_HEADER: secret,
            },
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )

    @classmethod
    def from_environment(
        cls,
        endpoint_url: str,
        *,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ModalBackend":
        """Build a backend, reading proxy-auth credentials only from the env."""

        env = os.environ if environ is None else environ
        return cls(
            endpoint_url,
            proxy_auth_key=env.get(ENV_MODAL_KEY),
            proxy_auth_secret=env.get(ENV_MODAL_SECRET),
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )


class LocalSglangBackend(_ChatCompletionsBackend):
    """Calls an operator's local SGLang server over its OpenAI API.

    The local server is not proxy-auth protected, so no credentials are sent;
    the endpoint defaults to ``http://localhost:8000/v1``.
    """

    backend_mode = BACKEND_MODE_LOCAL

    def __init__(
        self,
        endpoint_url: str = DEFAULT_LOCAL_ENDPOINT_URL,
        *,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ):
        super().__init__(
            endpoint_url,
            headers=None,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )

    @classmethod
    def from_environment(
        cls,
        endpoint_url: str,
        *,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "LocalSglangBackend":
        """Build a backend, letting the environment override the endpoint.

        The deployment record points at the endpoint as seen from the host that
        started it (typically ``localhost``), which is not reachable from inside
        the sidecar container. ``POSTFIAT_SIDECAR_LOCAL_ENDPOINT_URL`` lets the
        containerized loop reach the same server (e.g. via
        ``http://host.docker.internal:8000/v1``) without touching the record.
        """

        env = os.environ if environ is None else environ
        override = (env.get(ENV_LOCAL_ENDPOINT_URL) or "").strip()
        return cls(
            override or endpoint_url,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )


def _build_request_body(model_request: dict[str, Any]) -> dict[str, Any]:
    body = {key: model_request[key] for key in REQUEST_BODY_KEYS if key in model_request}
    extra_body = model_request.get("extra_body")
    if isinstance(extra_body, dict):
        body.update(extra_body)
    return body


def _extract_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise InferenceError(
            FailureCategory.INFERENCE_ERROR,
            "inference response was not a JSON object",
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InferenceError(
            FailureCategory.INFERENCE_ERROR,
            "inference response contained no choices",
        )
    first_choice = choices[0]
    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise InferenceError(
            FailureCategory.INFERENCE_ERROR,
            "inference response had no message content",
        )
    return content


def _chat_completions_url(endpoint_url: str) -> str:
    base = (endpoint_url or "").strip().rstrip("/")
    if not base:
        raise InferenceConfigError("an inference endpoint URL is required")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/{CHAT_COMPLETIONS_SUFFIX}"
