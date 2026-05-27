import httpx
import pytest

from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.scoring_client import (
    ScoringClient,
    ScoringHTTPError,
    ScoringNetworkError,
    ScoringResponseError,
)


def _config():
    return load_config(base_url="https://scoring.example.org/", environ={})


def test_round_url_uses_scoring_endpoint_path():
    client = ScoringClient(_config(), http_client=httpx.Client())

    assert (
        client.round_url(123)
        == "https://scoring.example.org/api/scoring/rounds/123"
    )


def test_rounds_url_uses_scoring_endpoint_path_and_query():
    client = ScoringClient(_config(), http_client=httpx.Client())

    assert client.rounds_url(limit=20, offset=5) == (
        "https://scoring.example.org/api/scoring/rounds?limit=20&offset=5"
    )


def test_input_package_file_url_uses_input_namespace():
    client = ScoringClient(_config(), http_client=httpx.Client())

    assert client.input_package_file_url(456, "inputs/model_request.json") == (
        "https://scoring.example.org/api/scoring/rounds/456"
        "/input/inputs/model_request.json"
    )


def test_ipfs_package_file_url_uses_gateway_cid_and_file_path():
    client = ScoringClient(_config(), http_client=httpx.Client())

    assert client.ipfs_package_file_url(
        "https://ipfs.example.org/ipfs",
        "QmInput",
        "inputs/model_request.json",
    ) == "https://ipfs.example.org/ipfs/QmInput/inputs/model_request.json"


def test_fetch_round_returns_json_payload():
    def handler(request):
        assert request.url.path == "/api/scoring/rounds/123"
        return httpx.Response(200, json={"id": 123})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ScoringClient(_config(), http_client=http_client)

    assert client.fetch_round(123) == {"id": 123}


def test_fetch_rounds_returns_round_payloads():
    def handler(request):
        assert request.url.path == "/api/scoring/rounds"
        assert request.url.params["limit"] == "20"
        assert request.url.params["offset"] == "0"
        return httpx.Response(200, json={"rounds": [{"id": 123}], "total": 1})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ScoringClient(_config(), http_client=http_client)

    assert client.fetch_rounds(limit=20) == [{"id": 123}]


def test_fetch_rounds_rejects_malformed_payload():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": []})
        )
    )
    client = ScoringClient(_config(), http_client=http_client)

    with pytest.raises(ScoringResponseError, match="missing a rounds list"):
        client.fetch_rounds(limit=20)


def test_fetch_round_raises_for_http_error():
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    )
    client = ScoringClient(_config(), http_client=http_client)

    with pytest.raises(ScoringHTTPError) as exc_info:
        client.fetch_round(123)

    assert exc_info.value.status_code == 404


def test_fetch_round_raises_for_network_error():
    def handler(request):
        raise httpx.ConnectError("connection failed", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ScoringClient(_config(), http_client=http_client)

    with pytest.raises(ScoringNetworkError):
        client.fetch_round(123)
