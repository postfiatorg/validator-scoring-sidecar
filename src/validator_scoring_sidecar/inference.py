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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from validator_scoring_sidecar.config import DEFAULT_INFERENCE_TIMEOUT_SECONDS
from validator_scoring_sidecar.failure import Failure, FailureCategory

ENV_MODAL_KEY = "POSTFIAT_SIDECAR_MODAL_KEY"
ENV_MODAL_SECRET = "POSTFIAT_SIDECAR_MODAL_SECRET"
ENV_LOCAL_ENDPOINT_URL = "POSTFIAT_SIDECAR_LOCAL_ENDPOINT_URL"
MODEL_REQUEST_RELATIVE_PATH = "inputs/model_request.json"
BACKEND_MODE_MODAL = "modal"
BACKEND_MODE_LOCAL = "local"
DEFAULT_INFERENCE_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_INFERENCE_WRITE_TIMEOUT_SECONDS = 10.0
DEFAULT_INFERENCE_POOL_TIMEOUT_SECONDS = 5.0
DEFAULT_LOCAL_ENDPOINT_URL = "http://localhost:8000/v1"
DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS = 20.0
PROXY_AUTH_KEY_HEADER = "Modal-Key"
PROXY_AUTH_SECRET_HEADER = "Modal-Secret"
CHAT_COMPLETIONS_SUFFIX = "chat/completions"
HEALTH_SUFFIX = "health"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_POLL_READ_TIMEOUT_SECONDS = 30.0
# Result statuses of the bundled app's job interface (_modal_app.result).
POLL_STATUS_PENDING = "pending"
POLL_STATUS_DONE = "done"
POLL_STATUS_FAILED = "failed"

# Structured reason markers recorded in a failure's details. RUNTIME_UNAVAILABLE
# covers both an unreachable endpoint and a misconfigured runtime — conditions
# with opposite operator fixes — and the FailureCategory vocabulary is shared
# with foundation convergence reporting and must not grow a new member, so the
# disambiguation lives in the details instead.
FAILURE_REASON_KEY = "reason"
FAILURE_REASON_ENDPOINT_UNREACHABLE = "endpoint_unreachable"
FAILURE_REASON_CONFIGURATION = "configuration"
# A poll-transport pass that ran out of budget while the generation is still
# running server-side: not a fault — the persisted call resumes next pass.
FAILURE_REASON_GENERATION_IN_PROGRESS = "generation_in_progress"

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


def probe_endpoint_ready(
    endpoint_url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS,
    http_client: httpx.Client | None = None,
) -> bool:
    """Whether the inference endpoint actually serves right now.

    Probes SGLang's ``/health`` (served at the endpoint root, above the ``/v1``
    API prefix). Any transport failure, timeout, or non-2xx answer reads as not
    ready — the caller polls, so a false negative only costs one interval. On a
    scaled-to-zero Modal endpoint the probe itself triggers the container cold
    start, which is exactly what a warm-up pass wants.
    """

    base = (endpoint_url or "").strip().rstrip("/")
    if not base:
        raise InferenceConfigError("an inference endpoint URL is required")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}/{HEALTH_SUFFIX}"

    client = http_client or httpx.Client(
        timeout=inference_timeout(timeout_seconds), follow_redirects=True
    )
    try:
        response = client.get(url, headers=headers or {})
    except httpx.HTTPError:
        return False
    finally:
        if http_client is None:
            client.close()
    return 200 <= response.status_code < 300


def modal_proxy_auth_headers(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """The Modal proxy-auth headers from the environment, or ``None`` when the
    proxy credentials are not configured (the endpoint cannot be probed)."""

    env = os.environ if environ is None else environ
    key = (env.get(ENV_MODAL_KEY) or "").strip()
    secret = (env.get(ENV_MODAL_SECRET) or "").strip()
    if not key or not secret:
        return None
    return {PROXY_AUTH_KEY_HEADER: key, PROXY_AUTH_SECRET_HEADER: secret}


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
                details={FAILURE_REASON_KEY: FAILURE_REASON_ENDPOINT_UNREACHABLE},
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


@dataclass(frozen=True)
class _PollTransport:
    """The job-interface pieces of a Modal backend, present as a unit.

    Existing at all means both URLs were recorded at deploy time, so the
    fields are non-optional by construction — the poll path never has to
    re-check them.
    """

    client: httpx.Client
    owns_client: bool
    submit_url: str
    result_url: str


class ModalBackend(_ChatCompletionsBackend):
    """Calls an operator-deployed Modal SGLang endpoint over its OpenAI API.

    With ``submit_url`` and ``result_url`` (the app's job interface, recorded
    at deploy time) the backend uses the submit-and-poll transport: it submits
    the frozen request, receives a call identifier, and polls with short
    requests until the generation completes — no long-lived connection for a
    VPN, firewall, or NAT to kill, and an interrupted client resumes the same
    server-side call via ``pending_call_id`` instead of paying for a new one.
    Without them it falls back to the direct chat-completions request, so a
    deployment predating the job interface keeps working. ``timeout_seconds``
    bounds the direct request's read; for the poll transport it is the overall
    budget across submit and polls.
    """

    backend_mode = BACKEND_MODE_MODAL

    def __init__(
        self,
        endpoint_url: str,
        *,
        proxy_auth_key: str | None,
        proxy_auth_secret: str | None,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        submit_url: str | None = None,
        result_url: str | None = None,
        pending_call_id: str | None = None,
        on_call_submitted: Callable[[str], None] | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
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
        submit = (submit_url or "").strip()
        result = (result_url or "").strip()
        # Poll requests use short read timeouts of their own; an injected test
        # client is reused for them, a real one is owned and closed here.
        self._poll_transport: _PollTransport | None = (
            _PollTransport(
                client=http_client
                or httpx.Client(
                    timeout=inference_timeout(DEFAULT_POLL_READ_TIMEOUT_SECONDS),
                    follow_redirects=True,
                ),
                owns_client=http_client is None,
                submit_url=submit,
                result_url=result,
            )
            if submit and result
            else None
        )
        self._pending_call_id = pending_call_id
        self._on_call_submitted = on_call_submitted
        self._poll_interval = poll_interval_seconds
        self._budget_seconds = timeout_seconds

    def _uses_poll_transport(self) -> bool:
        return self._poll_transport is not None

    def close(self) -> None:
        transport = self._poll_transport
        if transport is not None and transport.owns_client:
            transport.client.close()
        super().close()

    def run(self, model_request: dict[str, Any]) -> InferenceResult:
        transport = self._poll_transport
        if transport is None:
            return super().run(model_request)
        return self._run_polling(transport, model_request)

    def _run_polling(
        self, transport: _PollTransport, model_request: dict[str, Any]
    ) -> InferenceResult:
        body = _build_request_body(model_request)
        deadline = time.monotonic() + self._budget_seconds
        call_id = self._pending_call_id
        submitted_this_run = call_id is None
        if call_id is None:
            call_id = self._submit(transport, body)

        while True:
            try:
                status, payload = self._poll(transport, call_id)
            except InferenceError:
                # A single failed poll is a transport blip, not a verdict on
                # the generation; keep polling while budget remains.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self._poll_interval)
                continue
            if status == POLL_STATUS_DONE:
                return InferenceResult(
                    content=_extract_content(payload),
                    response_payload=payload,
                )
            if status == POLL_STATUS_FAILED:
                # A failed resumed call may simply have expired server-side;
                # one fresh submit recovers that without looping on a
                # genuinely failing generation, and only while budget remains
                # — a resubmit starts a billed generation.
                if submitted_this_run or time.monotonic() >= deadline:
                    raise InferenceError(
                        FailureCategory.INFERENCE_ERROR,
                        f"inference job {call_id} failed: {payload}",
                        details={"call_id": call_id},
                    )
                call_id = self._submit(transport, body)
                submitted_this_run = True
                continue
            if time.monotonic() >= deadline:
                # The generation keeps running server-side and the call id is
                # persisted, so the next pass resumes it rather than paying
                # for a new one.
                raise InferenceError(
                    FailureCategory.INFERENCE_TIMEOUT,
                    f"inference job {call_id} is still generating after this "
                    f"pass's {self._budget_seconds:.0f}s budget; the persisted "
                    "call resumes on the next pass",
                    details={
                        "call_id": call_id,
                        FAILURE_REASON_KEY: FAILURE_REASON_GENERATION_IN_PROGRESS,
                    },
                )
            time.sleep(self._poll_interval)

    def _submit(self, transport: _PollTransport, body: dict[str, Any]) -> str:
        payload = self._poll_request(
            lambda: transport.client.post(
                transport.submit_url, json=body, headers=self._headers
            ),
            "submit inference job",
        )
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise InferenceError(
                FailureCategory.INFERENCE_ERROR,
                f"inference submit returned no call_id: {payload}",
            )
        if self._on_call_submitted is not None:
            self._on_call_submitted(call_id)
        return call_id

    def _poll(self, transport: _PollTransport, call_id: str) -> tuple[str, Any]:
        payload = self._poll_request(
            lambda: transport.client.get(
                transport.result_url,
                params={"call_id": call_id},
                headers=self._headers,
            ),
            f"poll inference job {call_id}",
        )
        status = payload.get("status")
        if status == POLL_STATUS_DONE:
            return POLL_STATUS_DONE, payload.get("response")
        if status == POLL_STATUS_PENDING:
            return POLL_STATUS_PENDING, None
        return POLL_STATUS_FAILED, payload.get("error", payload)

    def _poll_request(
        self, send: Callable[[], httpx.Response], action: str
    ) -> dict[str, Any]:
        try:
            response = send()
        except httpx.TimeoutException as exc:
            raise InferenceError(
                FailureCategory.INFERENCE_TIMEOUT,
                f"could not {action}: {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise InferenceError(
                FailureCategory.RUNTIME_UNAVAILABLE,
                f"could not {action}: {exc}",
                details={FAILURE_REASON_KEY: FAILURE_REASON_ENDPOINT_UNREACHABLE},
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise InferenceError(
                FailureCategory.INFERENCE_ERROR,
                f"could not {action}: HTTP {response.status_code}",
                details={"status_code": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise InferenceError(
                FailureCategory.INFERENCE_ERROR,
                f"could not {action}: response is not valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise InferenceError(
                FailureCategory.INFERENCE_ERROR,
                f"could not {action}: response is not a JSON object",
            )
        return payload

    @classmethod
    def from_environment(
        cls,
        endpoint_url: str,
        *,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        environ: Mapping[str, str] | None = None,
        submit_url: str | None = None,
        result_url: str | None = None,
        pending_call_id: str | None = None,
        on_call_submitted: Callable[[str], None] | None = None,
    ) -> "ModalBackend":
        """Build a backend, reading proxy-auth credentials only from the env."""

        env = os.environ if environ is None else environ
        return cls(
            endpoint_url,
            proxy_auth_key=env.get(ENV_MODAL_KEY),
            proxy_auth_secret=env.get(ENV_MODAL_SECRET),
            timeout_seconds=timeout_seconds,
            http_client=http_client,
            submit_url=submit_url,
            result_url=result_url,
            pending_call_id=pending_call_id,
            on_call_submitted=on_call_submitted,
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
