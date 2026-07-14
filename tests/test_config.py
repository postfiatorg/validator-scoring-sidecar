from pathlib import Path

import pytest

from validator_scoring_sidecar.config import (
    DEFAULT_DATA_DIR_ROOT,
    DEFAULT_INFERENCE_TIMEOUT_SECONDS,
    ConfigError,
    ENV_INFERENCE_TIMEOUT_SECONDS,
    ENV_IPFS_GATEWAY_URL,
    ENV_SCORING_BASE_URL,
    NETWORK_IPFS_GATEWAY_URLS,
    NETWORK_SCORING_BASE_URLS,
    load_config,
)


def test_load_config_uses_defaults_when_unset():
    config = load_config(environ={})

    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["testnet"]
    assert config.ipfs_gateway_url == NETWORK_IPFS_GATEWAY_URLS["testnet"]
    assert config.network == "testnet"
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/testnet").expanduser()
    assert config.timeout_seconds == 30.0


def test_load_config_uses_network_scoped_defaults():
    config = load_config(network="devnet", environ={})

    assert config.network == "devnet"
    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["devnet"]
    assert config.ipfs_gateway_url == NETWORK_IPFS_GATEWAY_URLS["devnet"]
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/devnet").expanduser()


def test_load_config_normalizes_network_before_resolving_defaults():
    config = load_config(network="DevNet", environ={})

    assert config.network == "devnet"
    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["devnet"]
    assert config.ipfs_gateway_url == NETWORK_IPFS_GATEWAY_URLS["devnet"]
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/devnet").expanduser()


def test_load_config_uses_environment_values():
    config = load_config(
        environ={
            "POSTFIAT_SCORING_BASE_URL": "https://scoring-devnet.postfiat.org/",
            "POSTFIAT_SIDECAR_DATA_DIR": "/tmp/sidecar",
            "POSTFIAT_SIDECAR_IPFS_GATEWAY_URL": "https://ipfs-testnet.postfiat.org/ipfs/",
            "POSTFIAT_SIDECAR_NETWORK": "devnet",
            "POSTFIAT_SIDECAR_TIMEOUT_SECONDS": "12.5",
        }
    )

    assert config.scoring_base_url == "https://scoring-devnet.postfiat.org"
    assert config.data_dir == Path("/tmp/sidecar")
    assert config.ipfs_gateway_url == "https://ipfs-testnet.postfiat.org/ipfs"
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
        environ={
            ENV_SCORING_BASE_URL: "https://custom-scoring.example.org",
            ENV_IPFS_GATEWAY_URL: "https://custom-ipfs.example.org/ipfs",
        },
    )

    assert config.network == "devnet"
    assert config.scoring_base_url == NETWORK_SCORING_BASE_URLS["devnet"]
    assert config.ipfs_gateway_url == NETWORK_IPFS_GATEWAY_URLS["devnet"]


def test_load_config_cli_values_override_environment():
    config = load_config(
        base_url="https://example.org/",
        data_dir="/var/lib/sidecar",
        ipfs_gateway_url="https://ipfs.example.org/ipfs/",
        network="testnet",
        timeout_seconds=7,
        environ={
            "POSTFIAT_SCORING_BASE_URL": "https://ignored.example.org",
            "POSTFIAT_SIDECAR_DATA_DIR": "/ignored",
            "POSTFIAT_SIDECAR_IPFS_GATEWAY_URL": "https://ignored.example.org/ipfs",
            "POSTFIAT_SIDECAR_NETWORK": "ignored",
            "POSTFIAT_SIDECAR_TIMEOUT_SECONDS": "99",
        },
    )

    assert config.scoring_base_url == "https://example.org"
    assert config.data_dir == Path("/var/lib/sidecar")
    assert config.ipfs_gateway_url == "https://ipfs.example.org/ipfs"
    assert config.network == "testnet"
    assert config.timeout_seconds == 7.0


@pytest.mark.parametrize(
    "base_url",
    ["", "not-a-url", "ftp://example.org"],
)
def test_load_config_rejects_invalid_base_url(base_url):
    with pytest.raises(ConfigError):
        load_config(base_url=base_url, environ={})


def test_load_config_rejects_invalid_ipfs_gateway_url():
    with pytest.raises(ConfigError):
        load_config(ipfs_gateway_url="not-a-url", environ={})


def test_load_config_rejects_invalid_timeout():
    with pytest.raises(ConfigError, match="greater than zero"):
        load_config(timeout_seconds=0, environ={})


def test_inference_timeout_defaults_when_unset():
    config = load_config(environ={})

    assert config.inference_timeout_seconds == DEFAULT_INFERENCE_TIMEOUT_SECONDS


def test_inference_timeout_from_environment():
    config = load_config(
        environ={ENV_INFERENCE_TIMEOUT_SECONDS: "600"},
    )

    assert config.inference_timeout_seconds == 600.0


def test_inference_timeout_cli_override_beats_environment():
    config = load_config(
        inference_timeout_seconds=450,
        environ={ENV_INFERENCE_TIMEOUT_SECONDS: "600"},
    )

    assert config.inference_timeout_seconds == 450.0


@pytest.mark.parametrize("bad_value", ["0", "-1", "abc", ""])
def test_load_config_rejects_invalid_inference_timeout(bad_value):
    with pytest.raises(ConfigError, match="inference_timeout_seconds"):
        load_config(inference_timeout_seconds=bad_value, environ={})


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
    assert config.ipfs_gateway_url is None
    assert config.data_dir == Path(f"{DEFAULT_DATA_DIR_ROOT}/customnet").expanduser()


def test_load_config_allows_unknown_network_with_explicit_ipfs_gateway_url():
    config = load_config(
        network="customnet",
        base_url="https://custom-scoring.example.org",
        ipfs_gateway_url="https://custom-ipfs.example.org/ipfs",
        environ={},
    )

    assert config.ipfs_gateway_url == "https://custom-ipfs.example.org/ipfs"


def test_pftl_rpc_url_defaults_per_network():
    assert (
        load_config(network="devnet", environ={}).pftl_rpc_url
        == "https://rpc.devnet.postfiat.org"
    )
    assert (
        load_config(network="testnet", environ={}).pftl_rpc_url
        == "https://rpc.testnet.postfiat.org"
    )


def test_pftl_rpc_url_env_override_without_explicit_network():
    config = load_config(environ={"POSTFIAT_SIDECAR_PFTL_RPC_URL": "https://custom.rpc"})
    assert config.pftl_rpc_url == "https://custom.rpc"


def test_pftl_rpc_url_cli_override_wins():
    config = load_config(network="testnet", pftl_rpc_url="https://cli.rpc", environ={})
    assert config.pftl_rpc_url == "https://cli.rpc"


def test_foundation_publisher_address_defaults_none_and_reads_env():
    assert load_config(environ={}).foundation_publisher_address is None
    config = load_config(
        environ={"POSTFIAT_SIDECAR_FOUNDATION_PUBLISHER_ADDRESS": "rEnvPub"}
    )
    assert config.foundation_publisher_address == "rEnvPub"


def test_chain_poll_interval_default_and_env():
    assert load_config(environ={}).chain_poll_interval_seconds == 60.0
    config = load_config(
        environ={"POSTFIAT_SIDECAR_CHAIN_POLL_INTERVAL_SECONDS": "30"}
    )
    assert config.chain_poll_interval_seconds == 30.0


def test_validator_wallet_seed_read_from_env_only():
    assert load_config(environ={}).validator_wallet_seed is None
    config = load_config(
        environ={"POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED": "sEdTESTseed"}
    )
    assert config.validator_wallet_seed == "sEdTESTseed"


def test_validator_keys_path_read_from_env():
    assert load_config(environ={}).validator_keys_path is None
    config = load_config(
        environ={"POSTFIAT_SIDECAR_VALIDATOR_KEYS_PATH": "/keys/validator-keys.json"}
    )
    assert config.validator_keys_path == "/keys/validator-keys.json"


def test_modal_app_name_defaults_none_and_reads_env():
    assert load_config(environ={}).modal_app_name is None
    config = load_config(
        environ={
            "POSTFIAT_SIDECAR_MODAL_APP_NAME": "validator-scoring-sidecar-devnet-nurgle"
        }
    )
    assert config.modal_app_name == "validator-scoring-sidecar-devnet-nurgle"


def test_modal_app_name_rejects_invalid_value():
    with pytest.raises(ConfigError, match="modal_app_name"):
        load_config(environ={"POSTFIAT_SIDECAR_MODAL_APP_NAME": "bad name!"})


def test_modal_app_name_blank_treated_as_unset():
    config = load_config(environ={"POSTFIAT_SIDECAR_MODAL_APP_NAME": "   "})
    assert config.modal_app_name is None


def test_modal_scaledown_minutes_defaults_and_reads_env():
    assert load_config(environ={}).modal_scaledown_minutes == 5
    config = load_config(
        environ={"POSTFIAT_SIDECAR_MODAL_SCALEDOWN_MINUTES": "12"}
    )
    assert config.modal_scaledown_minutes == 12


@pytest.mark.parametrize("value", ["0", "-3", "2.5", "soon", "25"])
def test_modal_scaledown_minutes_rejects_invalid_value(value):
    with pytest.raises(ConfigError, match="modal_scaledown_minutes"):
        load_config(environ={"POSTFIAT_SIDECAR_MODAL_SCALEDOWN_MINUTES": value})
