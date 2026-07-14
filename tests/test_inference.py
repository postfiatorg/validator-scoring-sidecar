import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.inference import (
    BACKEND_MODE_LOCAL,
    BACKEND_MODE_MODAL,
    DEFAULT_INFERENCE_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_INFERENCE_POOL_TIMEOUT_SECONDS,
    DEFAULT_INFERENCE_TIMEOUT_SECONDS,
    DEFAULT_INFERENCE_WRITE_TIMEOUT_SECONDS,
    FAILURE_REASON_ENDPOINT_UNREACHABLE,
    FAILURE_REASON_KEY,
    InferenceConfigError,
    InferenceError,
    LocalSglangBackend,
    ModalBackend,
    ModelRequestError,
    load_model_request,
    modal_proxy_auth_headers,
    probe_endpoint_ready,
)

ENDPOINT = "https://operator--app.modal.run"


def _model_request(**overrides):
    request = {
        "method": "chat.completions.create",
        "model": "Qwen/Qwen3.6-27B-FP8",
        "messages": [{"role": "user", "content": "score validators"}],
        "temperature": 0,
        "max_tokens": 16384,
        "response_format": {"type": "json_object"},
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    request.update(overrides)
    return request


def _completion(content='{"v1": 80}'):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _backend(handler, endpoint=ENDPOINT):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ModalBackend(
        endpoint,
        proxy_auth_key="key-value",
        proxy_auth_secret="secret-value",
        http_client=client,
    )


def test_backend_mode_is_modal():
    assert ModalBackend(
        ENDPOINT,
        proxy_auth_key="k",
        proxy_auth_secret="s",
        http_client=httpx.Client(),
    ).backend_mode == BACKEND_MODE_MODAL


def test_run_forwards_frozen_request_verbatim():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("PARSED OUTPUT"))

    result = _backend(handler).run(_model_request())

    assert captured["path"] == "/v1/chat/completions"
    assert captured["headers"]["Modal-Key"] == "key-value"
    assert captured["headers"]["Modal-Secret"] == "secret-value"
    # method dropped; extra_body merged at the top level; nothing else added.
    assert captured["body"] == {
        "model": "Qwen/Qwen3.6-27B-FP8",
        "messages": [{"role": "user", "content": "score validators"}],
        "temperature": 0,
        "max_tokens": 16384,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert result.content == "PARSED OUTPUT"
    assert result.response_payload["choices"][0]["message"]["content"] == "PARSED OUTPUT"


def test_run_does_not_default_fill_missing_fields():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    minimal = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    _backend(handler).run(minimal)

    assert captured["body"] == {"model": "m", "messages": [{"role": "user", "content": "x"}]}


def test_endpoint_url_without_v1_gets_v1_suffix():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_completion())

    _backend(handler, endpoint="https://x.modal.run").run(_model_request())

    assert captured["url"] == "https://x.modal.run/v1/chat/completions"


def test_endpoint_url_with_v1_is_not_doubled():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_completion())

    _backend(handler, endpoint="https://x.modal.run/v1/").run(_model_request())

    assert captured["url"] == "https://x.modal.run/v1/chat/completions"


def test_missing_credentials_raises_config_error():
    with pytest.raises(InferenceConfigError):
        ModalBackend(ENDPOINT, proxy_auth_key="", proxy_auth_secret="s")


def test_missing_endpoint_raises_config_error():
    with pytest.raises(InferenceConfigError):
        ModalBackend("   ", proxy_auth_key="k", proxy_auth_secret="s")


def test_from_environment_requires_credentials():
    with pytest.raises(InferenceConfigError):
        ModalBackend.from_environment(ENDPOINT, environ={})


def test_from_environment_forwards_env_credentials():
    captured = {}

    def handler(request):
        captured["headers"] = request.headers
        return httpx.Response(200, json=_completion())

    backend = ModalBackend.from_environment(
        ENDPOINT,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        environ={
            "POSTFIAT_SIDECAR_MODAL_KEY": "env-key",
            "POSTFIAT_SIDECAR_MODAL_SECRET": "env-secret",
        },
    )
    backend.run(_model_request())

    assert captured["headers"]["Modal-Key"] == "env-key"
    assert captured["headers"]["Modal-Secret"] == "env-secret"


def test_timeout_maps_to_inference_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(InferenceError) as exc_info:
        _backend(handler).run(_model_request())

    assert exc_info.value.category == FailureCategory.INFERENCE_TIMEOUT


def test_default_client_uses_bounded_explicit_timeout():
    backend = LocalSglangBackend()
    timeout = backend._http.timeout

    assert timeout.connect == DEFAULT_INFERENCE_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == DEFAULT_INFERENCE_TIMEOUT_SECONDS
    assert timeout.write == DEFAULT_INFERENCE_WRITE_TIMEOUT_SECONDS
    assert timeout.pool == DEFAULT_INFERENCE_POOL_TIMEOUT_SECONDS
    backend.close()


def test_non_responding_endpoint_returns_within_read_timeout():
    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(0.3)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(_completion()).encode("utf-8"))

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = LocalSglangBackend(
        f"http://127.0.0.1:{server.server_port}/v1",
        timeout_seconds=0.05,
    )

    started = time.monotonic()
    try:
        with pytest.raises(InferenceError) as exc_info:
            backend.run(_model_request())
    finally:
        elapsed = time.monotonic() - started
        backend.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert exc_info.value.category == FailureCategory.INFERENCE_TIMEOUT
    assert elapsed < 0.5


def test_connection_error_maps_to_runtime_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(InferenceError) as exc_info:
        _backend(handler).run(_model_request())

    assert exc_info.value.category == FailureCategory.RUNTIME_UNAVAILABLE
    assert exc_info.value.failure.details == {
        FAILURE_REASON_KEY: FAILURE_REASON_ENDPOINT_UNREACHABLE
    }
    assert "connection refused" in str(exc_info.value)


def test_http_error_status_maps_to_inference_error():
    with pytest.raises(InferenceError) as exc_info:
        _backend(lambda request: httpx.Response(500, json={"error": "boom"})).run(
            _model_request()
        )

    assert exc_info.value.category == FailureCategory.INFERENCE_ERROR
    assert exc_info.value.failure.details["status_code"] == 500


def test_invalid_json_response_maps_to_inference_error():
    with pytest.raises(InferenceError) as exc_info:
        _backend(lambda request: httpx.Response(200, text="not json")).run(
            _model_request()
        )

    assert exc_info.value.category == FailureCategory.INFERENCE_ERROR


def test_empty_choices_maps_to_inference_error():
    with pytest.raises(InferenceError) as exc_info:
        _backend(lambda request: httpx.Response(200, json={"choices": []})).run(
            _model_request()
        )

    assert exc_info.value.category == FailureCategory.INFERENCE_ERROR


def test_missing_message_content_maps_to_inference_error():
    with pytest.raises(InferenceError) as exc_info:
        _backend(
            lambda request: httpx.Response(200, json={"choices": [{"message": {}}]})
        ).run(_model_request())

    assert exc_info.value.category == FailureCategory.INFERENCE_ERROR


def _probe_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_endpoint_ready_true_on_healthy_endpoint():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("Modal-Key")
        return httpx.Response(200, text="ok")

    ready = probe_endpoint_ready(
        f"{ENDPOINT}/v1",
        headers={"Modal-Key": "k", "Modal-Secret": "s"},
        http_client=_probe_client(handler),
    )

    assert ready is True
    # /health is served at the endpoint root, above the /v1 API prefix.
    assert captured["url"] == f"{ENDPOINT}/health"
    assert captured["key"] == "k"


@pytest.mark.parametrize("error", [None, httpx.ConnectError, httpx.ReadTimeout])
def test_probe_endpoint_ready_false_when_not_serving(error):
    def handler(request):
        if error is not None:
            raise error("not serving", request=request)
        return httpx.Response(503, text="starting")

    assert (
        probe_endpoint_ready(ENDPOINT, http_client=_probe_client(handler)) is False
    )


def test_modal_proxy_auth_headers_reads_env():
    headers = modal_proxy_auth_headers(
        environ={
            "POSTFIAT_SIDECAR_MODAL_KEY": "k",
            "POSTFIAT_SIDECAR_MODAL_SECRET": "s",
        }
    )
    assert headers == {"Modal-Key": "k", "Modal-Secret": "s"}
    assert modal_proxy_auth_headers(environ={}) is None


SUBMIT_URL = f"{ENDPOINT}/submit"
RESULT_URL = f"{ENDPOINT}/result"
POLL_RAW_RESPONSE = json.dumps({"v1": {"score": 80}})
POLL_DONE_PAYLOAD = {
    "status": "done",
    "response": {"choices": [{"message": {"content": POLL_RAW_RESPONSE}}]},
}


def _poll_backend(handler, **kwargs):
    return ModalBackend(
        ENDPOINT,
        proxy_auth_key="key",
        proxy_auth_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        submit_url=SUBMIT_URL,
        result_url=RESULT_URL,
        poll_interval_seconds=0,
        **kwargs,
    )


def test_poll_transport_submits_then_polls_to_completion():
    calls = {"submit": 0, "result": 0}
    submitted_ids = []

    def handler(request):
        if str(request.url) == SUBMIT_URL:
            calls["submit"] += 1
            assert request.headers.get("Modal-Key") == "key"
            return httpx.Response(200, json={"call_id": "fc-123"})
        calls["result"] += 1
        assert request.url.params["call_id"] == "fc-123"
        if calls["result"] == 1:
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json=POLL_DONE_PAYLOAD)

    backend = _poll_backend(handler, on_call_submitted=submitted_ids.append)
    result = backend.run(_model_request())

    assert result.content == POLL_RAW_RESPONSE
    assert calls == {"submit": 1, "result": 2}
    assert submitted_ids == ["fc-123"]


def test_poll_transport_resumes_pending_call_without_submitting():
    def handler(request):
        assert str(request.url) != SUBMIT_URL, "resume must not submit a new job"
        assert request.url.params["call_id"] == "fc-resume"
        return httpx.Response(200, json=POLL_DONE_PAYLOAD)

    backend = _poll_backend(handler, pending_call_id="fc-resume")
    result = backend.run(_model_request())

    assert result.content == POLL_RAW_RESPONSE


def test_poll_transport_resubmits_once_when_resumed_call_expired():
    submitted_ids = []

    def handler(request):
        if str(request.url) == SUBMIT_URL:
            return httpx.Response(200, json={"call_id": "fc-fresh"})
        if request.url.params["call_id"] == "fc-expired":
            return httpx.Response(
                200, json={"status": "failed", "error": "expired"}
            )
        return httpx.Response(200, json=POLL_DONE_PAYLOAD)

    backend = _poll_backend(
        handler,
        pending_call_id="fc-expired",
        on_call_submitted=submitted_ids.append,
    )
    result = backend.run(_model_request())

    assert result.content == POLL_RAW_RESPONSE
    assert submitted_ids == ["fc-fresh"]


def test_poll_transport_raises_when_fresh_job_fails():
    def handler(request):
        if str(request.url) == SUBMIT_URL:
            return httpx.Response(200, json={"call_id": "fc-bad"})
        return httpx.Response(200, json={"status": "failed", "error": "boom"})

    with pytest.raises(InferenceError) as exc_info:
        _poll_backend(handler).run(_model_request())

    assert exc_info.value.category == FailureCategory.INFERENCE_ERROR
    assert "boom" in str(exc_info.value)


def test_poll_transport_times_out_on_exhausted_budget():
    def handler(request):
        if str(request.url) == SUBMIT_URL:
            return httpx.Response(200, json={"call_id": "fc-slow"})
        return httpx.Response(200, json={"status": "pending"})

    with pytest.raises(InferenceError) as exc_info:
        _poll_backend(handler, timeout_seconds=0).run(_model_request())

    assert exc_info.value.category == FailureCategory.INFERENCE_TIMEOUT
    assert exc_info.value.failure.details == {
        "call_id": "fc-slow",
        FAILURE_REASON_KEY: "generation_in_progress",
    }


def test_poll_transport_survives_transient_poll_failures():
    calls = {"result": 0}

    def handler(request):
        if str(request.url) == SUBMIT_URL:
            return httpx.Response(200, json={"call_id": "fc-flaky"})
        calls["result"] += 1
        if calls["result"] == 1:
            raise httpx.ConnectError("blip", request=request)
        return httpx.Response(200, json=POLL_DONE_PAYLOAD)

    result = _poll_backend(handler).run(_model_request())

    assert result.content == POLL_RAW_RESPONSE
    assert calls["result"] == 2


def test_from_environment_forwards_job_interface_parameters():
    def on_submitted(call_id):
        pass

    backend = ModalBackend.from_environment(
        ENDPOINT,
        environ={
            "POSTFIAT_SIDECAR_MODAL_KEY": "k",
            "POSTFIAT_SIDECAR_MODAL_SECRET": "s",
        },
        submit_url=SUBMIT_URL,
        result_url=RESULT_URL,
        pending_call_id="fc-resume",
        on_call_submitted=on_submitted,
    )
    try:
        assert backend._uses_poll_transport() is True
        assert backend._pending_call_id == "fc-resume"
        assert backend._on_call_submitted is on_submitted
    finally:
        backend.close()


def test_poll_transport_network_failure_marks_endpoint_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(InferenceError) as exc_info:
        _poll_backend(handler).run(_model_request())

    assert exc_info.value.category == FailureCategory.RUNTIME_UNAVAILABLE
    assert exc_info.value.failure.details == {
        FAILURE_REASON_KEY: FAILURE_REASON_ENDPOINT_UNREACHABLE
    }


def test_modal_backend_without_job_interface_uses_direct_transport():
    def handler(request):
        assert str(request.url) == f"{ENDPOINT}/v1/chat/completions"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": POLL_RAW_RESPONSE}}]}
        )

    backend = ModalBackend(
        ENDPOINT,
        proxy_auth_key="key",
        proxy_auth_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = backend.run(_model_request())

    assert result.content == POLL_RAW_RESPONSE


def test_load_model_request_reads_inputs_file(tmp_path):
    inputs_dir = tmp_path / "packages" / ("a" * 64) / "inputs"
    inputs_dir.mkdir(parents=True)
    (inputs_dir / "model_request.json").write_text(
        json.dumps(_model_request()), encoding="utf-8"
    )

    loaded = load_model_request(tmp_path / "packages" / ("a" * 64))

    assert loaded["model"] == "Qwen/Qwen3.6-27B-FP8"
    assert loaded["messages"][0]["content"] == "score validators"


def test_load_model_request_missing_raises(tmp_path):
    with pytest.raises(ModelRequestError, match="not found"):
        load_model_request(tmp_path)


def test_load_model_request_invalid_json_raises(tmp_path):
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "model_request.json").write_text("{bad", encoding="utf-8")

    with pytest.raises(ModelRequestError, match="not valid JSON"):
        load_model_request(tmp_path)


# ---------------------------------------------------------------------------
# Local SGLang backend
# ---------------------------------------------------------------------------


def _local_backend(handler, endpoint=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    if endpoint is None:
        return LocalSglangBackend(http_client=client)
    return LocalSglangBackend(endpoint, http_client=client)


def test_local_backend_mode_is_local():
    assert (
        LocalSglangBackend(http_client=httpx.Client()).backend_mode
        == BACKEND_MODE_LOCAL
    )


def test_local_run_forwards_request_and_sends_no_proxy_auth():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("LOCAL OUTPUT"))

    result = _local_backend(handler).run(_model_request())

    assert captured["path"] == "/v1/chat/completions"
    # The local server is not proxy-auth protected; no Modal credentials are sent.
    assert "Modal-Key" not in captured["headers"]
    assert "Modal-Secret" not in captured["headers"]
    # Byte-identical request body to the Modal path: method dropped, extra_body merged.
    assert captured["body"] == {
        "model": "Qwen/Qwen3.6-27B-FP8",
        "messages": [{"role": "user", "content": "score validators"}],
        "temperature": 0,
        "max_tokens": 16384,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert result.content == "LOCAL OUTPUT"


def test_local_default_endpoint_targets_localhost():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_completion())

    _local_backend(handler).run(_model_request())

    assert captured["url"] == "http://localhost:8000/v1/chat/completions"


def test_default_client_follows_redirects():
    # Modal answers >150s requests (cold starts) with a 303 redirect chain to a
    # result-polling URL; the default client must follow it like the
    # foundation's OpenAI-SDK client does.
    backend = LocalSglangBackend()
    assert backend._http.follow_redirects is True
    backend.close()


def test_backend_follows_redirect_to_result_url():
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(
                303, headers={"Location": "http://localhost:8000/result/abc"}
            )
        return httpx.Response(200, json=_completion("REDIRECTED OUTPUT"))

    client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    result = LocalSglangBackend(
        "http://localhost:8000/v1", http_client=client
    ).run(_model_request())

    assert result.content == "REDIRECTED OUTPUT"
    assert calls == ["POST", "GET"]


def test_local_environment_endpoint_override_wins():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_completion())

    backend = LocalSglangBackend.from_environment(
        "http://localhost:8000/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        environ={
            "POSTFIAT_SIDECAR_LOCAL_ENDPOINT_URL": "http://host.docker.internal:8000/v1"
        },
    )
    backend.run(_model_request())

    assert captured["url"] == "http://host.docker.internal:8000/v1/chat/completions"


def test_local_environment_without_override_uses_record_endpoint():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_completion())

    backend = LocalSglangBackend.from_environment(
        "http://gpu-host:8000/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        environ={},
    )
    backend.run(_model_request())

    assert captured["url"] == "http://gpu-host:8000/v1/chat/completions"


def test_local_timeout_maps_to_inference_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(InferenceError) as exc_info:
        _local_backend(handler).run(_model_request())

    assert exc_info.value.category == FailureCategory.INFERENCE_TIMEOUT


def test_local_connection_error_maps_to_runtime_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(InferenceError) as exc_info:
        _local_backend(handler).run(_model_request())

    assert exc_info.value.category == FailureCategory.RUNTIME_UNAVAILABLE
    assert exc_info.value.failure.details == {
        FAILURE_REASON_KEY: FAILURE_REASON_ENDPOINT_UNREACHABLE
    }


def test_local_http_error_status_maps_to_inference_error():
    with pytest.raises(InferenceError) as exc_info:
        _local_backend(lambda request: httpx.Response(503, json={"error": "x"})).run(
            _model_request()
        )

    assert exc_info.value.category == FailureCategory.INFERENCE_ERROR
