"""HTTP client for the public Dynamic UNL scoring service."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from validator_scoring_sidecar.config import SidecarConfig


class ScoringClientError(RuntimeError):
    """Base error for scoring service client failures."""


class ScoringHTTPError(ScoringClientError):
    """Raised when the scoring service returns a non-success HTTP status."""

    def __init__(
        self,
        status_code: int,
        url: str,
        *,
        service_name: str = "Scoring service",
    ):
        self.status_code = status_code
        self.url = url
        super().__init__(f"{service_name} returned HTTP {status_code} for {url}")


class ScoringNetworkError(ScoringClientError):
    """Raised when the scoring service cannot be reached."""


class ScoringResponseError(ScoringClientError):
    """Raised when the scoring service returns an unexpected response body."""


class ScoringClient:
    """Client for scoring service public round metadata endpoints."""

    def __init__(
        self,
        config: SidecarConfig,
        *,
        http_client: httpx.Client | None = None,
    ):
        self._config = config
        self._http = http_client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def round_url(self, round_id: int) -> str:
        if round_id <= 0:
            raise ValueError("round_id must be greater than zero")
        return f"{self._config.scoring_base_url}/api/scoring/rounds/{round_id}"

    def input_package_file_url(self, round_number: int, file_path: str) -> str:
        if round_number <= 0:
            raise ValueError("round_number must be greater than zero")
        return (
            f"{self._config.scoring_base_url}/api/scoring/rounds/{round_number}"
            f"/input/{quote(file_path, safe='/')}"
        )

    def ipfs_package_file_url(
        self,
        gateway_url: str,
        cid: str,
        file_path: str,
    ) -> str:
        return f"{gateway_url}/{quote(cid, safe='')}/{quote(file_path, safe='/')}"

    def fetch_round(self, round_id: int) -> dict[str, Any]:
        """Fetch one public scoring round metadata record."""

        payload = self._fetch_json(
            self.round_url(round_id),
            service_name="Scoring service",
        )
        if not isinstance(payload, dict):
            raise ScoringResponseError(
                "Scoring service returned a non-object JSON body for "
                f"{self.round_url(round_id)}"
            )
        return payload

    def fetch_input_package_file(
        self,
        round_number: int,
        file_path: str,
    ) -> dict[str, Any] | list[Any]:
        """Fetch one frozen input package file from scoring-service HTTPS fallback."""

        return self._fetch_json(
            self.input_package_file_url(round_number, file_path),
            service_name="Scoring service",
        )

    def fetch_ipfs_package_file(
        self,
        gateway_url: str,
        cid: str,
        file_path: str,
    ) -> dict[str, Any] | list[Any]:
        """Fetch one package file from an IPFS gateway path."""

        return self._fetch_json(
            self.ipfs_package_file_url(gateway_url, cid, file_path),
            service_name="IPFS gateway",
        )

    def _fetch_json(
        self,
        url: str,
        *,
        service_name: str,
    ) -> dict[str, Any] | list[Any]:
        try:
            response = self._http.get(url)
        except httpx.RequestError as exc:
            raise ScoringNetworkError(
                f"Could not reach {service_name.lower()} at {url}: {exc}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise ScoringHTTPError(
                response.status_code,
                url,
                service_name=service_name,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ScoringResponseError(
                f"{service_name} returned invalid JSON for {url}"
            ) from exc

        if not isinstance(payload, (dict, list)):
            raise ScoringResponseError(
                f"{service_name} returned a non-JSON-object/array body for {url}"
            )
        return payload
