"""Configuration loading for the validator scoring sidecar."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DATA_DIR_ROOT = "~/.postfiat/validator-scoring-sidecar"
DEFAULT_NETWORK = "testnet"
DEFAULT_TIMEOUT_SECONDS = 30.0
NETWORK_SCORING_BASE_URLS = {
    "devnet": "https://scoring-devnet.postfiat.org",
    "testnet": "https://scoring-testnet.postfiat.org",
}

ENV_SCORING_BASE_URL = "POSTFIAT_SCORING_BASE_URL"
ENV_DATA_DIR = "POSTFIAT_SIDECAR_DATA_DIR"
ENV_NETWORK = "POSTFIAT_SIDECAR_NETWORK"
ENV_TIMEOUT_SECONDS = "POSTFIAT_SIDECAR_TIMEOUT_SECONDS"


class ConfigError(ValueError):
    """Raised when sidecar configuration is invalid."""


@dataclass(frozen=True)
class SidecarConfig:
    """Resolved runtime configuration."""

    scoring_base_url: str
    data_dir: Path
    network: str
    timeout_seconds: float


def load_config(
    *,
    base_url: str | None = None,
    data_dir: str | Path | None = None,
    network: str | None = None,
    timeout_seconds: float | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SidecarConfig:
    """Load configuration using CLI overrides, environment, then defaults."""

    env = os.environ if environ is None else environ
    resolved_network = _resolve_value(
        cli_value=network,
        env_value=env.get(ENV_NETWORK),
        default=DEFAULT_NETWORK,
    )
    normalized_network = _normalize_network(resolved_network)
    resolved_base_url = _resolve_base_url(
        cli_value=base_url,
        env_value=env.get(ENV_SCORING_BASE_URL),
        network=normalized_network,
        prefer_network_default=network is not None,
    )
    resolved_data_dir = _resolve_data_dir(
        cli_value=str(data_dir) if data_dir is not None else None,
        env_value=env.get(ENV_DATA_DIR),
        network=normalized_network,
    )
    resolved_timeout = _resolve_value(
        cli_value=str(timeout_seconds) if timeout_seconds is not None else None,
        env_value=env.get(ENV_TIMEOUT_SECONDS),
        default=str(DEFAULT_TIMEOUT_SECONDS),
    )

    return SidecarConfig(
        scoring_base_url=_normalize_base_url(resolved_base_url),
        data_dir=Path(_require_non_empty("data_dir", resolved_data_dir)).expanduser(),
        network=normalized_network,
        timeout_seconds=_parse_timeout(resolved_timeout),
    )


def _resolve_value(
    *,
    cli_value: str | None,
    env_value: str | None,
    default: str,
) -> str:
    if cli_value is not None:
        return cli_value
    if env_value is not None and env_value.strip():
        return env_value
    return default


def _resolve_data_dir(
    *,
    cli_value: str | None,
    env_value: str | None,
    network: str,
) -> str:
    if cli_value is not None:
        return cli_value
    if env_value is not None and env_value.strip():
        return env_value
    return f"{DEFAULT_DATA_DIR_ROOT}/{network}"


def _resolve_base_url(
    *,
    cli_value: str | None,
    env_value: str | None,
    network: str,
    prefer_network_default: bool,
) -> str:
    if cli_value is not None:
        return cli_value
    if prefer_network_default:
        return _network_default_base_url(network)
    if env_value is not None and env_value.strip():
        return env_value
    return _network_default_base_url(network)


def _network_default_base_url(network: str) -> str:
    try:
        return NETWORK_SCORING_BASE_URLS[network]
    except KeyError as exc:
        raise ConfigError(
            "no default scoring service base URL is configured for network "
            f"{network!r}; pass --base-url or set {ENV_SCORING_BASE_URL}"
        ) from exc


def _require_non_empty(name: str, value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ConfigError(f"{name} must not be empty")
    return stripped


def _normalize_network(value: str) -> str:
    return _require_non_empty("network", value).lower()


def _normalize_base_url(value: str) -> str:
    base_url = _require_non_empty("scoring_base_url", value).rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("scoring_base_url must be an http(s) URL")
    return base_url


def _parse_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ConfigError("timeout_seconds must be a number") from exc
    if timeout <= 0:
        raise ConfigError("timeout_seconds must be greater than zero")
    return timeout
