from pathlib import Path

import pytest

from validator_scoring_sidecar.config import (
    DEFAULT_DATA_DIR_ROOT,
    ConfigError,
    ENV_SCORING_BASE_URL,
    NETWORK_SCORING_BASE_URLS,
    load_config,
)


def test_load_config_uses_defaults_when_unset():
    config = load_config(environ={})

    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["testnet"]
    assert config.network == "testnet"
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/testnet").expanduser()
    assert config.timeout_seconds == 30.0


def test_load_config_uses_network_scoped_defaults():
    config = load_config(network="devnet", environ={})

    assert config.network == "devnet"
    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["devnet"]
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/devnet").expanduser()


def test_load_config_normalizes_network_before_resolving_defaults():
    config = load_config(network="DevNet", environ={})

    assert config.network == "devnet"
    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["devnet"]
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/devnet").expanduser()


def test_load_config_uses_environment_values():
    config = load_config(
        environ={
            "POSTFIAT_SCORING_BASE_URL": "https://scoring-devnet.postfiat.org/",
            "POSTFIAT_SIDECAR_DATA_DIR": "/tmp/sidecar",
            "POSTFIAT_SIDECAR_NETWORK": "devnet",
            "POSTFIAT_SIDECAR_TIMEOUT_SECONDS": "12.5",
        }
    )

    assert config.scoring_base_url == "https://scoring-devnet.postfiat.org"
    assert config.data_dir == Path("/tmp/sidecar")
    assert config.network == "devnet"
    assert config.timeout_seconds == 12.5


def test_load_config_environment_url_overrides_default_url_without_cli_network():
    config = load_config(
        environ={ENV_SCORING_BASE_URL: "https://custom-scoring.example.org"},
    )

    assert config.network == "testnet"
    assert config.scoring_base_url == "https://custom-scoring.example.org"


def test_load_config_cli_network_overrides_environment_url_default():
    config = load_config(
        network="devnet",
        environ={ENV_SCORING_BASE_URL: "https://custom-scoring.example.org"},
    )

    assert config.network == "devnet"
    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["devnet"]


def test_load_config_cli_values_override_environment():
    config = load_config(
        base_url="https://example.org/",
        data_dir="/var/lib/sidecar",
        network="testnet",
        timeout_seconds=7,
        environ={
            "POSTFIAT_SCORING_BASE_URL": "https://ignored.example.org",
            "POSTFIAT_SIDECAR_DATA_DIR": "/ignored",
            "POSTFIAT_SIDECAR_NETWORK": "ignored",
            "POSTFIAT_SIDECAR_TIMEOUT_SECONDS": "99",
        },
    )

    assert config.scoring_base_url == "https://example.org"
    assert config.data_dir == Path("/var/lib/sidecar")
    assert config.network == "testnet"
    assert config.timeout_seconds == 7.0


@pytest.mark.parametrize(
    "base_url",
    ["", "not-a-url", "ftp://example.org"],
)
def test_load_config_rejects_invalid_base_url(base_url):
    with pytest.raises(ConfigError):
        load_config(base_url=base_url, environ={})


def test_load_config_rejects_invalid_timeout():
    with pytest.raises(ConfigError, match="greater than zero"):
        load_config(timeout_seconds=0, environ={})


def test_load_config_requires_base_url_for_unknown_network():
    with pytest.raises(ConfigError, match="no default scoring service base URL"):
        load_config(network="customnet", environ={})


def test_load_config_allows_unknown_network_with_explicit_base_url():
    config = load_config(
        network="customnet",
        base_url="https://custom-scoring.example.org",
        environ={},
    )

    assert config.network == "customnet"
    assert config.scoring_base_url == "https://custom-scoring.example.org"
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/customnet").expanduser()
