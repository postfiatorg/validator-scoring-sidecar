"""HTTP client for the public Dynamic UNL scoring service."""

from __future__ import annotations

from typing import Any

import httpx

from validator_scoring_sidecar.config import SidecarConfig


class ScoringClientError(RuntimeError):
    """Base error for scoring service client failures."""


class ScoringHTTPError(ScoringClientError):
    """Raised when the scoring service returns a non-success HTTP status."""

    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        self.url = url
        super().__init__(
            f"Scoring service returned HTTP {status_code} for {url}"
        )


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

    def fetch_round(self, round_id: int) -> dict[str, Any]:
        """Fetch one public scoring round metadata record."""

        url = self.round_url(round_id)
        try:
            response = self._http.get(url)
        except httpx.RequestError as exc:
            raise ScoringNetworkError(
                f"Could not reach scoring service at {url}: {exc}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise ScoringHTTPError(response.status_code, url)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ScoringResponseError(
                f"Scoring service returned invalid JSON for {url}"
            ) from exc

        if not isinstance(payload, dict):
            raise ScoringResponseError(
                f"Scoring service returned a non-object JSON body for {url}"
            )
        return payload

