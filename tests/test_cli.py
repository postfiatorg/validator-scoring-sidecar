import json

import pytest

from validator_scoring_sidecar import cli
from validator_scoring_sidecar.scoring_client import (
    ScoringHTTPError,
    ScoringNetworkError,
)


class FakeClient:
    payload = None
    error = None
    last_config = None

    def __init__(self, config):
        self.config = config
        FakeClient.last_config = config

    def fetch_round(self, round_id):
        if self.error is not None:
            raise self.error
        return dict(self.payload)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.payload = _payload()
    FakeClient.error = None
    FakeClient.last_config = None
    monkeypatch.setattr(cli, "ScoringClient", FakeClient)


def _payload(**overrides):
    payload = {
        "id": 123,
        "round_number": 456,
        "status": "COMPLETE",
        "input_package_cid": "QmInput",
        "input_package_hash": "a" * 64,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": "QmFinal",
    }
    payload.update(overrides)
    return payload


def test_inspect_round_human_output(capsys):
    exit_code = cli.main(["inspect-round", "--round-id", "123"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Round ID: 123" in captured.out
    assert "Round number: 456" in captured.out
    assert "Input package CID: QmInput" in captured.out
    assert "Input package hash: " + "a" * 64 in captured.out
    assert "Input frozen at: 2026-05-25T00:00:00+00:00" in captured.out
    assert "Final bundle CID: QmFinal" in captured.out
    assert captured.err == ""


def test_inspect_round_json_output(capsys):
    exit_code = cli.main(["inspect-round", "--round-id", "123", "--json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "round_id": 123,
        "round_number": 456,
        "status": "COMPLETE",
        "input_package_cid": "QmInput",
        "input_package_hash": "a" * 64,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": "QmFinal",
    }


@pytest.mark.parametrize(
    "field",
    ["input_package_cid", "input_package_hash", "input_frozen_at"],
)
def test_inspect_round_missing_frozen_metadata_fails(capsys, field):
    FakeClient.payload = _payload(**{field: None})

    exit_code = cli.main(["inspect-round", "--round-id", "123"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "does not expose frozen input package metadata" in captured.err
    assert field in captured.err
    assert captured.out == ""


def test_inspect_round_http_error_fails(capsys):
    FakeClient.error = ScoringHTTPError(
        404,
        "https://scoring.example.org/api/scoring/rounds/123",
    )

    exit_code = cli.main(["inspect-round", "--round-id", "123"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "HTTP 404" in captured.err


def test_inspect_round_network_error_fails(capsys):
    FakeClient.error = ScoringNetworkError("Could not reach scoring service")

    exit_code = cli.main(["inspect-round", "--round-id", "123"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "Could not reach scoring service" in captured.err


def test_invalid_config_exits_usage_error(capsys):
    exit_code = cli.main(
        [
            "inspect-round",
            "--round-id",
            "123",
            "--base-url",
            "not-a-url",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Configuration error" in captured.err


def test_cli_network_flag_selects_matching_default_url(capsys, monkeypatch):
    monkeypatch.setenv(
        "POSTFIAT_SCORING_BASE_URL",
        "https://scoring-testnet.postfiat.org",
    )

    exit_code = cli.main(["inspect-round", "--round-id", "123", "--network", "devnet"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert FakeClient.last_config.scoring_base_url == "https://scoring-devnet.postfiat.org"
    assert str(FakeClient.last_config.data_dir).endswith(
        ".postfiat/validator-scoring-sidecar/devnet"
    )
    assert captured.err == ""
