"""SQLite-backed local sidecar round state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validator_scoring_sidecar.input_package import FetchedInputPackage
from validator_scoring_sidecar.round_metadata import RoundMetadata

STATE_DB_FILENAME = "sidecar.db"
STATE_DISCOVERED = "DISCOVERED"
STATE_INPUT_PACKAGE_VERIFIED = "INPUT_PACKAGE_VERIFIED"
SCHEMA_VERSION = 1


class SidecarStateError(RuntimeError):
    """Raised when local sidecar state cannot be read or written."""


@dataclass(frozen=True)
class RoundStateRecord:
    """Local state for one scoring round on one network."""

    network: str
    round_id: int
    round_number: int
    scoring_status: str
    sidecar_state: str
    input_package_cid: str
    input_package_hash: str
    input_frozen_at: str
    local_package_path: str | None
    fetch_source: str | None
    verified_file_count: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RoundStateRecord":
        return cls(
            network=row["network"],
            round_id=row["round_id"],
            round_number=row["round_number"],
            scoring_status=row["scoring_status"],
            sidecar_state=row["sidecar_state"],
            input_package_cid=row["input_package_cid"],
            input_package_hash=row["input_package_hash"],
            input_frozen_at=row["input_frozen_at"],
            local_package_path=row["local_package_path"],
            fetch_source=row["fetch_source"],
            verified_file_count=row["verified_file_count"],
        )

    def matches_frozen_input(self, metadata: RoundMetadata) -> bool:
        return (
            self.input_package_cid == metadata.input_package_cid
            and self.input_package_hash == metadata.input_package_hash
            and self.input_frozen_at == metadata.input_frozen_at
        )


class SidecarState:
    """SQLite state store scoped to one sidecar data directory."""

    def __init__(self, data_dir: Path):
        self._db_path = data_dir / STATE_DB_FILENAME
        self._connection: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    def __enter__(self) -> "SidecarState":
        self.open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def open(self) -> None:
        if self._connection is not None:
            return
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._db_path)
            connection.row_factory = sqlite3.Row
            self._connection = connection
            self._ensure_schema()
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise SidecarStateError(
                f"Failed to open sidecar state database at {self._db_path}: {exc}"
            ) from exc

    def close(self) -> None:
        if self._connection is None:
            return
        self._connection.close()
        self._connection = None

    def get_round(self, network: str, round_id: int) -> RoundStateRecord | None:
        row = self._execute_one(
            """
            SELECT network, round_id, round_number, scoring_status, sidecar_state,
                   input_package_cid, input_package_hash, input_frozen_at,
                   local_package_path, fetch_source, verified_file_count
            FROM sidecar_rounds
            WHERE network = ? AND round_id = ?
            """,
            (network, round_id),
        )
        return RoundStateRecord.from_row(row) if row is not None else None

    def is_input_package_verified(
        self,
        network: str,
        metadata: RoundMetadata,
        *,
        cache_path: Path,
    ) -> bool:
        record = self.get_round(network, metadata.round_id)
        return (
            record is not None
            and record.sidecar_state == STATE_INPUT_PACKAGE_VERIFIED
            and record.matches_frozen_input(metadata)
            and cache_path.is_dir()
        )

    def record_discovered(self, network: str, metadata: RoundMetadata) -> None:
        now = _utc_now()
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                discovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                round_number = excluded.round_number,
                scoring_status = excluded.scoring_status,
                sidecar_state = excluded.sidecar_state,
                input_package_cid = excluded.input_package_cid,
                input_package_hash = excluded.input_package_hash,
                input_frozen_at = excluded.input_frozen_at,
                local_package_path = NULL,
                fetch_source = NULL,
                verified_file_count = NULL,
                input_verified_at = NULL,
                updated_at = excluded.updated_at
            """,
            (
                network,
                metadata.round_id,
                metadata.round_number,
                metadata.status,
                STATE_DISCOVERED,
                metadata.input_package_cid,
                metadata.input_package_hash,
                metadata.input_frozen_at,
                now,
                now,
            ),
        )

    def record_input_verified(
        self,
        network: str,
        metadata: RoundMetadata,
        fetched_package: FetchedInputPackage,
    ) -> None:
        now = _utc_now()
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                local_package_path, fetch_source, verified_file_count,
                discovered_at, input_verified_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                round_number = excluded.round_number,
                scoring_status = excluded.scoring_status,
                sidecar_state = excluded.sidecar_state,
                input_package_cid = excluded.input_package_cid,
                input_package_hash = excluded.input_package_hash,
                input_frozen_at = excluded.input_frozen_at,
                local_package_path = excluded.local_package_path,
                fetch_source = excluded.fetch_source,
                verified_file_count = excluded.verified_file_count,
                input_verified_at = excluded.input_verified_at,
                updated_at = excluded.updated_at
            """,
            (
                network,
                metadata.round_id,
                metadata.round_number,
                metadata.status,
                STATE_INPUT_PACKAGE_VERIFIED,
                metadata.input_package_cid,
                metadata.input_package_hash,
                metadata.input_frozen_at,
                str(fetched_package.local_path),
                fetched_package.source,
                fetched_package.verified_file_count,
                now,
                now,
                now,
            ),
        )

    def _ensure_schema(self) -> None:
        current_version = self._schema_version()
        if current_version > SCHEMA_VERSION:
            raise SidecarStateError(
                "Sidecar state database schema version "
                f"{current_version} is newer than supported version "
                f"{SCHEMA_VERSION}; upgrade validator-scoring-sidecar"
            )
        if current_version == SCHEMA_VERSION:
            return

        self._execute_write(
            """
            CREATE TABLE IF NOT EXISTS sidecar_rounds (
                network TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                scoring_status TEXT NOT NULL,
                sidecar_state TEXT NOT NULL,
                input_package_cid TEXT NOT NULL,
                input_package_hash TEXT NOT NULL,
                input_frozen_at TEXT NOT NULL,
                local_package_path TEXT,
                fetch_source TEXT,
                verified_file_count INTEGER,
                discovered_at TEXT NOT NULL,
                input_verified_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (network, round_id)
            )
            """,
            (),
        )
        self._execute_write(
            """
            CREATE INDEX IF NOT EXISTS idx_sidecar_rounds_state
            ON sidecar_rounds(network, sidecar_state, round_number DESC)
            """,
            (),
        )
        self._execute_write(f"PRAGMA user_version = {SCHEMA_VERSION}", ())

    def _schema_version(self) -> int:
        connection = self._require_connection()
        try:
            cursor = connection.execute("PRAGMA user_version")
            row = cursor.fetchone()
        except sqlite3.Error as exc:
            raise SidecarStateError(
                f"Failed to read sidecar state schema version: {exc}"
            ) from exc
        if row is None:
            return 0
        return int(row[0])

    def _execute_one(
        self,
        sql: str,
        parameters: tuple[Any, ...],
    ) -> sqlite3.Row | None:
        connection = self._require_connection()
        try:
            cursor = connection.execute(sql, parameters)
            return cursor.fetchone()
        except sqlite3.Error as exc:
            raise SidecarStateError(f"Failed to read sidecar state: {exc}") from exc

    def _execute_write(self, sql: str, parameters: tuple[Any, ...]) -> None:
        connection = self._require_connection()
        try:
            connection.execute(sql, parameters)
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise SidecarStateError(f"Failed to write sidecar state: {exc}") from exc

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SidecarStateError("Sidecar state database is not open")
        return self._connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
