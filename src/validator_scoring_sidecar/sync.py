"""Automation-first frozen input package sync."""

from __future__ import annotations

import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.input_package import (
    SOURCE_AUTO,
    FetchedInputPackage,
    InputPackageVerificationError,
    PackageSource,
    fetch_input_package,
    package_cache_path,
    verify_cached_input_package,
)
from validator_scoring_sidecar.round_metadata import (
    MissingFrozenInputMetadata,
    RoundMetadata,
    round_identifier,
)
from validator_scoring_sidecar.scoring_client import ScoringClient
from validator_scoring_sidecar.state import SidecarState

DEFAULT_SYNC_ROUND_LIMIT = 5
MAX_SYNC_ROUND_LIMIT = 20
LOCK_FILE_NAME = "sidecar.lock"
SYNC_STATUS_INPUT_PACKAGE_READY = "input_package_ready"
SYNC_STATUS_NO_ELIGIBLE_ROUND = "no_eligible_round"


class SyncLockError(RuntimeError):
    """Raised when another sidecar sync already owns the local lock."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        super().__init__(f"sidecar sync is already running: {lock_path}")


class SyncSetupError(RuntimeError):
    """Raised when sync cannot prepare local filesystem state."""


@dataclass(frozen=True)
class SyncResult:
    """Result from one sync execution."""

    status: str
    network: str
    scanned_rounds: int
    package: FetchedInputPackage | None = None

    @property
    def action(self) -> str | None:
        if self.package is None:
            return None
        return "cache_reused" if self.package.cached else "fetched"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "network": self.network,
            "scanned_rounds": self.scanned_rounds,
        }
        if self.package is not None:
            payload["action"] = self.action
            payload["round_id"] = self.package.round_id
            payload["round_number"] = self.package.round_number
            payload["package"] = self.package.as_dict()
        return payload


class SidecarLock:
    """Non-blocking advisory lock scoped to one data directory."""

    def __init__(self, data_dir: Path):
        self.lock_path = data_dir / LOCK_FILE_NAME
        self._file = None

    def __enter__(self) -> "SidecarLock":
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.lock_path.open("a+", encoding="utf-8")
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if self._file is not None:
                self._file.close()
            self._file = None
            raise SyncLockError(self.lock_path) from exc
        except OSError as exc:
            if self._file is not None:
                self._file.close()
            self._file = None
            raise SyncSetupError(
                f"Failed to prepare sidecar sync lock at {self.lock_path}: {exc}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def sync_input_package(
    config: SidecarConfig,
    client: ScoringClient,
    *,
    source: PackageSource = SOURCE_AUTO,
    round_limit: int = DEFAULT_SYNC_ROUND_LIMIT,
    package_fetcher=fetch_input_package,
) -> SyncResult:
    """Discover and verify the newest unhandled frozen input package."""

    with SidecarLock(config.data_dir), SidecarState(config.data_dir) as state:
        round_payloads = client.fetch_rounds(limit=round_limit)
        scanned_rounds = 0
        for payload in round_payloads:
            scanned_rounds += 1
            try:
                metadata = RoundMetadata.from_api_payload(
                    payload,
                    requested_round_id=round_identifier(payload),
                )
            except MissingFrozenInputMetadata:
                continue

            cache_path = package_cache_path(config, metadata.input_package_hash)
            if state.is_input_package_verified(
                config.network,
                metadata,
                cache_path=cache_path,
            ):
                try:
                    verify_cached_input_package(cache_path, metadata, config)
                except InputPackageVerificationError as exc:
                    raise InputPackageVerificationError(
                        f"Previously verified input package at {cache_path} "
                        f"failed cache verification: {exc}"
                    ) from exc
                continue

            state.record_discovered(config.network, metadata)
            fetched_package = package_fetcher(
                metadata,
                config,
                client,
                source=source,
                force=False,
            )
            state.record_input_verified(
                config.network,
                metadata,
                fetched_package,
            )
            return SyncResult(
                status=SYNC_STATUS_INPUT_PACKAGE_READY,
                network=config.network,
                scanned_rounds=scanned_rounds,
                package=fetched_package,
            )

    return SyncResult(
        status=SYNC_STATUS_NO_ELIGIBLE_ROUND,
        network=config.network,
        scanned_rounds=len(round_payloads),
    )
