"""SQLite-backed local sidecar round state."""

from __future__ import annotations

import json
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
STATE_SCORED = "SCORED"
STATE_COMMITTED = "COMMITTED"
STATE_REVEALED = "REVEALED"
STATE_SCORING_FAILED = "SCORING_FAILED"
STATE_SKIPPED = "SKIPPED"
SCHEMA_VERSION = 5

# A round in any of these states already has its verified input package, so
# sync must treat it as handled and not re-fetch it.
INPUT_READY_STATES = frozenset(
    {
        STATE_INPUT_PACKAGE_VERIFIED,
        STATE_SCORED,
        STATE_COMMITTED,
        STATE_REVEALED,
        STATE_SCORING_FAILED,
        STATE_SKIPPED,
    }
)

# A round at or beyond SCORED has produced its output fingerprints: rescoring is
# unnecessary, and the round is eligible to commit/reveal.
SCORED_OR_FURTHER_STATES = frozenset({STATE_SCORED, STATE_COMMITTED, STATE_REVEALED})

_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("scored_at", "TEXT"),
    ("backend_mode", "TEXT"),
    ("model_response_hash", "TEXT"),
    ("validator_scores_hash", "TEXT"),
    ("selected_unl_hash", "TEXT"),
    ("comparison_levels_matched", "TEXT"),
    ("error_category", "TEXT"),
    ("error_details", "TEXT"),
)

_V4_COLUMNS: tuple[tuple[str, str], ...] = (
    ("salt", "TEXT"),
    ("commit_tx_hash", "TEXT"),
    ("validator_master_key", "TEXT"),
    ("commit_opens_at", "TEXT"),
    ("commit_closes_at", "TEXT"),
    ("reveal_opens_at", "TEXT"),
    ("reveal_closes_at", "TEXT"),
)

_V5_COLUMNS: tuple[tuple[str, str], ...] = (
    ("reveal_tx_hash", "TEXT"),
    ("commitment_hash", "TEXT"),
    ("reveal_error_category", "TEXT"),
)

# The full sidecar_rounds column list, in RoundStateRecord.from_row order, shared
# by every round read so the SELECT and the row mapping cannot drift apart.
_ROUND_COLUMNS = (
    "network, round_id, round_number, scoring_status, sidecar_state, "
    "input_package_cid, input_package_hash, input_frozen_at, "
    "local_package_path, fetch_source, verified_file_count, "
    "scored_at, backend_mode, model_response_hash, "
    "validator_scores_hash, selected_unl_hash, "
    "comparison_levels_matched, error_category, error_details, "
    "salt, commit_tx_hash, validator_master_key, "
    "commit_opens_at, commit_closes_at, reveal_opens_at, "
    "reveal_closes_at, reveal_tx_hash, commitment_hash, "
    "reveal_error_category"
)


class SidecarStateError(RuntimeError):
    """Raised when local sidecar state cannot be read or written."""


@dataclass(frozen=True)
class ScoreOutcome:
    """The result of a scoring attempt, persisted by ``record_score``.

    ``comparison_levels_matched`` is ``None`` while a scored round still awaits
    the foundation's hashes; it becomes a (possibly empty) list once a
    comparison has run. ``selected_unl_hash`` is set once the previous UNL is
    frozen into the input package and the level is reproduced.
    """

    sidecar_state: str
    backend_mode: str | None = None
    model_response_hash: str | None = None
    validator_scores_hash: str | None = None
    selected_unl_hash: str | None = None
    comparison_levels_matched: list[str] | None = None
    error_category: str | None = None
    error_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class CommitOutcome:
    """The on-chain commit a validator published for a round.

    Persisted by ``record_commit`` alongside the round's already-stored output
    hashes so the later reveal step has the salt and windows it needs.
    """

    validator_master_key: str
    salt: str
    commit_tx_hash: str
    commitment_hash: str
    commit_opens_at: str
    commit_closes_at: str
    reveal_opens_at: str
    reveal_closes_at: str


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
    scored_at: str | None
    backend_mode: str | None
    model_response_hash: str | None
    validator_scores_hash: str | None
    selected_unl_hash: str | None
    comparison_levels_matched: str | None
    error_category: str | None
    error_details: str | None
    salt: str | None
    commit_tx_hash: str | None
    validator_master_key: str | None
    commit_opens_at: str | None
    commit_closes_at: str | None
    reveal_opens_at: str | None
    reveal_closes_at: str | None
    reveal_tx_hash: str | None
    commitment_hash: str | None
    reveal_error_category: str | None

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
            scored_at=row["scored_at"],
            backend_mode=row["backend_mode"],
            model_response_hash=row["model_response_hash"],
            validator_scores_hash=row["validator_scores_hash"],
            selected_unl_hash=row["selected_unl_hash"],
            comparison_levels_matched=row["comparison_levels_matched"],
            error_category=row["error_category"],
            error_details=row["error_details"],
            salt=row["salt"],
            commit_tx_hash=row["commit_tx_hash"],
            validator_master_key=row["validator_master_key"],
            commit_opens_at=row["commit_opens_at"],
            commit_closes_at=row["commit_closes_at"],
            reveal_opens_at=row["reveal_opens_at"],
            reveal_closes_at=row["reveal_closes_at"],
            reveal_tx_hash=row["reveal_tx_hash"],
            commitment_hash=row["commitment_hash"],
            reveal_error_category=row["reveal_error_category"],
        )

    def matches_frozen_input(self, metadata: RoundMetadata) -> bool:
        return (
            self.input_package_cid == metadata.input_package_cid
            and self.input_package_hash == metadata.input_package_hash
            and self.input_frozen_at == metadata.input_frozen_at
        )


@dataclass(frozen=True)
class ChainCursor:
    """Last validated PFTL transaction the watcher has processed for an account."""

    network: str
    account: str
    last_processed_ledger_index: int
    last_processed_tx_hash: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChainCursor":
        return cls(
            network=row["network"],
            account=row["account"],
            last_processed_ledger_index=row["last_processed_ledger_index"],
            last_processed_tx_hash=row["last_processed_tx_hash"],
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
            f"""
            SELECT {_ROUND_COLUMNS}
            FROM sidecar_rounds
            WHERE network = ? AND round_id = ?
            """,
            (network, round_id),
        )
        return RoundStateRecord.from_row(row) if row is not None else None

    def get_rounds_pending_reveal(self, network: str) -> list[RoundStateRecord]:
        """Return committed rounds that have not yet revealed, oldest first."""

        rows = self._execute_all(
            f"""
            SELECT {_ROUND_COLUMNS}
            FROM sidecar_rounds
            WHERE network = ? AND sidecar_state = ? AND reveal_tx_hash IS NULL
            ORDER BY round_number ASC
            """,
            (network, STATE_COMMITTED),
        )
        return [RoundStateRecord.from_row(row) for row in rows]

    def get_rounds_pending_commit(self, network: str) -> list[RoundStateRecord]:
        """Return scored rounds with a known commit window that have not yet
        committed, oldest first.

        Mirrors ``get_rounds_pending_reveal``: the commit is driven from local
        state, not from a one-time chain-cursor pass, so a round scored on a
        later pass than its announcement still commits while its window is open.
        Only rounds carrying all three output fingerprints and the announced
        commit window are returned, so the caller can always build a commitment.
        """

        rows = self._execute_all(
            f"""
            SELECT {_ROUND_COLUMNS}
            FROM sidecar_rounds
            WHERE network = ? AND sidecar_state = ? AND commit_tx_hash IS NULL
              AND commit_opens_at IS NOT NULL AND commit_closes_at IS NOT NULL
              AND reveal_opens_at IS NOT NULL AND reveal_closes_at IS NOT NULL
              AND model_response_hash IS NOT NULL
              AND validator_scores_hash IS NOT NULL
              AND selected_unl_hash IS NOT NULL
            ORDER BY round_number ASC
            """,
            (network, STATE_SCORED),
        )
        return [RoundStateRecord.from_row(row) for row in rows]

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
            and record.sidecar_state in INPUT_READY_STATES
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

    def record_score(
        self,
        network: str,
        metadata: RoundMetadata,
        outcome: ScoreOutcome,
    ) -> None:
        """Persist a scoring outcome, preserving the input-fetch columns."""

        now = _utc_now()
        comparison = (
            ",".join(outcome.comparison_levels_matched)
            if outcome.comparison_levels_matched is not None
            else None
        )
        error_details = (
            json.dumps(outcome.error_details, sort_keys=True)
            if outcome.error_details is not None
            else None
        )
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                scored_at, backend_mode, model_response_hash,
                validator_scores_hash, selected_unl_hash,
                comparison_levels_matched, error_category, error_details,
                discovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                round_number = excluded.round_number,
                scoring_status = excluded.scoring_status,
                sidecar_state = excluded.sidecar_state,
                input_package_cid = excluded.input_package_cid,
                input_package_hash = excluded.input_package_hash,
                input_frozen_at = excluded.input_frozen_at,
                scored_at = excluded.scored_at,
                backend_mode = excluded.backend_mode,
                model_response_hash = excluded.model_response_hash,
                validator_scores_hash = excluded.validator_scores_hash,
                selected_unl_hash = excluded.selected_unl_hash,
                comparison_levels_matched = excluded.comparison_levels_matched,
                error_category = excluded.error_category,
                error_details = excluded.error_details,
                updated_at = excluded.updated_at
            """,
            (
                network,
                metadata.round_id,
                metadata.round_number,
                metadata.status,
                outcome.sidecar_state,
                metadata.input_package_cid,
                metadata.input_package_hash,
                metadata.input_frozen_at,
                now,
                outcome.backend_mode,
                outcome.model_response_hash,
                outcome.validator_scores_hash,
                outcome.selected_unl_hash,
                comparison,
                outcome.error_category,
                error_details,
                now,
                now,
            ),
        )

    def record_commit(
        self,
        network: str,
        metadata: RoundMetadata,
        outcome: CommitOutcome,
    ) -> None:
        """Advance a scored round to ``COMMITTED`` and persist its commit.

        Writes the commit columns and the committed ``commitment_hash`` (needed
        by the reveal integrity check), and moves the round to ``COMMITTED``. The
        scored output hashes set by ``record_score`` are left untouched.
        """

        now = _utc_now()
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                salt, commit_tx_hash, validator_master_key, commitment_hash,
                commit_opens_at, commit_closes_at, reveal_opens_at,
                reveal_closes_at, discovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                round_number = excluded.round_number,
                scoring_status = excluded.scoring_status,
                sidecar_state = excluded.sidecar_state,
                input_package_cid = excluded.input_package_cid,
                input_package_hash = excluded.input_package_hash,
                input_frozen_at = excluded.input_frozen_at,
                salt = excluded.salt,
                commit_tx_hash = excluded.commit_tx_hash,
                validator_master_key = excluded.validator_master_key,
                commitment_hash = excluded.commitment_hash,
                commit_opens_at = excluded.commit_opens_at,
                commit_closes_at = excluded.commit_closes_at,
                reveal_opens_at = excluded.reveal_opens_at,
                reveal_closes_at = excluded.reveal_closes_at,
                updated_at = excluded.updated_at
            """,
            (
                network,
                metadata.round_id,
                metadata.round_number,
                metadata.status,
                STATE_COMMITTED,
                metadata.input_package_cid,
                metadata.input_package_hash,
                metadata.input_frozen_at,
                outcome.salt,
                outcome.commit_tx_hash,
                outcome.validator_master_key,
                outcome.commitment_hash,
                outcome.commit_opens_at,
                outcome.commit_closes_at,
                outcome.reveal_opens_at,
                outcome.reveal_closes_at,
                now,
                now,
            ),
        )

    def record_announcement_windows(
        self,
        network: str,
        metadata: RoundMetadata,
        *,
        commit_opens_at: str,
        commit_closes_at: str,
        reveal_opens_at: str,
        reveal_closes_at: str,
    ) -> None:
        """Persist a round's announced commit/reveal windows.

        Stored when the foundation's on-chain announcement is first decoded and
        verified, so the state-driven commit can act on the round later
        regardless of the chain cursor. Only the window columns are written, and
        only until the round commits: an existing round's lifecycle state,
        scoring outcome, and fetch columns are left untouched, and an
        already-committed round's windows are left frozen because the reveal step
        enforces its reveal window from them. A round not yet in the store is
        inserted at ``DISCOVERED`` so its windows are not lost before it is
        scored.
        """

        now = _utc_now()
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                commit_opens_at, commit_closes_at, reveal_opens_at,
                reveal_closes_at, discovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                commit_opens_at = excluded.commit_opens_at,
                commit_closes_at = excluded.commit_closes_at,
                reveal_opens_at = excluded.reveal_opens_at,
                reveal_closes_at = excluded.reveal_closes_at,
                updated_at = excluded.updated_at
            WHERE commit_tx_hash IS NULL
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
                commit_opens_at,
                commit_closes_at,
                reveal_opens_at,
                reveal_closes_at,
                now,
                now,
            ),
        )

    def record_commit_skip(
        self,
        network: str,
        metadata: RoundMetadata,
        *,
        error_category: str,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Mark a round commit-skipped, flipping only the state/error columns.

        Used when a commit cannot be submitted (e.g. low balance). The scored
        output hashes, backend mode, and commit columns are left untouched.
        """

        now = _utc_now()
        details = (
            json.dumps(error_details, sort_keys=True)
            if error_details is not None
            else None
        )
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                error_category, error_details, discovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                sidecar_state = excluded.sidecar_state,
                error_category = excluded.error_category,
                error_details = excluded.error_details,
                updated_at = excluded.updated_at
            """,
            (
                network,
                metadata.round_id,
                metadata.round_number,
                metadata.status,
                STATE_SKIPPED,
                metadata.input_package_cid,
                metadata.input_package_hash,
                metadata.input_frozen_at,
                error_category,
                details,
                now,
                now,
            ),
        )

    def record_reveal(
        self,
        network: str,
        metadata: RoundMetadata,
        *,
        reveal_tx_hash: str,
    ) -> None:
        """Advance a committed round to ``REVEALED`` and store its reveal tx.

        Only the lifecycle state and ``reveal_tx_hash`` are written; the commit
        columns and scored output hashes are left untouched.
        """

        now = _utc_now()
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                reveal_tx_hash, discovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                sidecar_state = excluded.sidecar_state,
                reveal_tx_hash = excluded.reveal_tx_hash,
                updated_at = excluded.updated_at
            """,
            (
                network,
                metadata.round_id,
                metadata.round_number,
                metadata.status,
                STATE_REVEALED,
                metadata.input_package_cid,
                metadata.input_package_hash,
                metadata.input_frozen_at,
                reveal_tx_hash,
                now,
                now,
            ),
        )

    def record_reveal_miss(
        self,
        network: str,
        metadata: RoundMetadata,
        *,
        error_category: str,
    ) -> None:
        """Flag a committed round whose reveal window closed unrevealed.

        Leaves the round at ``COMMITTED`` and writes only ``reveal_error_category``.
        A missed reveal is a chain-participation miss, not a scoring failure, and
        is kept in its own column so a later foundation comparison — which owns
        ``error_category`` — cannot erase it.
        """

        now = _utc_now()
        self._execute_write(
            """
            INSERT INTO sidecar_rounds (
                network, round_id, round_number, scoring_status, sidecar_state,
                input_package_cid, input_package_hash, input_frozen_at,
                reveal_error_category, discovered_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, round_id) DO UPDATE SET
                reveal_error_category = excluded.reveal_error_category,
                updated_at = excluded.updated_at
            """,
            (
                network,
                metadata.round_id,
                metadata.round_number,
                metadata.status,
                STATE_COMMITTED,
                metadata.input_package_cid,
                metadata.input_package_hash,
                metadata.input_frozen_at,
                error_category,
                now,
                now,
            ),
        )

    def get_chain_cursor(self, network: str, account: str) -> ChainCursor | None:
        row = self._execute_one(
            """
            SELECT network, account, last_processed_ledger_index,
                   last_processed_tx_hash
            FROM chain_cursor
            WHERE network = ? AND account = ?
            """,
            (network, account),
        )
        return ChainCursor.from_row(row) if row is not None else None

    def set_chain_cursor(
        self,
        network: str,
        account: str,
        last_processed_ledger_index: int,
        last_processed_tx_hash: str,
    ) -> None:
        now = _utc_now()
        self._execute_write(
            """
            INSERT INTO chain_cursor (
                network, account, last_processed_ledger_index,
                last_processed_tx_hash, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(network, account) DO UPDATE SET
                last_processed_ledger_index = excluded.last_processed_ledger_index,
                last_processed_tx_hash = excluded.last_processed_tx_hash,
                updated_at = excluded.updated_at
            """,
            (
                network,
                account,
                last_processed_ledger_index,
                last_processed_tx_hash,
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

        if current_version == 0:
            self._create_schema()
        else:
            if current_version < 2:
                self._migrate_v1_to_v2()
            if current_version < 3:
                self._migrate_v2_to_v3()
            if current_version < 4:
                self._migrate_v3_to_v4()
            if current_version < 5:
                self._migrate_v4_to_v5()
        self._execute_write(f"PRAGMA user_version = {SCHEMA_VERSION}", ())

    def _create_schema(self) -> None:
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
                scored_at TEXT,
                backend_mode TEXT,
                model_response_hash TEXT,
                validator_scores_hash TEXT,
                selected_unl_hash TEXT,
                comparison_levels_matched TEXT,
                error_category TEXT,
                error_details TEXT,
                salt TEXT,
                commit_tx_hash TEXT,
                validator_master_key TEXT,
                commit_opens_at TEXT,
                commit_closes_at TEXT,
                reveal_opens_at TEXT,
                reveal_closes_at TEXT,
                reveal_tx_hash TEXT,
                commitment_hash TEXT,
                reveal_error_category TEXT,
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
        self._create_chain_cursor_table()

    def _migrate_v1_to_v2(self) -> None:
        existing = self._existing_columns("sidecar_rounds")
        for name, column_type in _V2_COLUMNS:
            if name not in existing:
                self._execute_write(
                    f"ALTER TABLE sidecar_rounds ADD COLUMN {name} {column_type}",
                    (),
                )

    def _migrate_v2_to_v3(self) -> None:
        self._create_chain_cursor_table()

    def _migrate_v3_to_v4(self) -> None:
        existing = self._existing_columns("sidecar_rounds")
        for name, column_type in _V4_COLUMNS:
            if name not in existing:
                self._execute_write(
                    f"ALTER TABLE sidecar_rounds ADD COLUMN {name} {column_type}",
                    (),
                )

    def _migrate_v4_to_v5(self) -> None:
        existing = self._existing_columns("sidecar_rounds")
        for name, column_type in _V5_COLUMNS:
            if name not in existing:
                self._execute_write(
                    f"ALTER TABLE sidecar_rounds ADD COLUMN {name} {column_type}",
                    (),
                )

    def _create_chain_cursor_table(self) -> None:
        self._execute_write(
            """
            CREATE TABLE IF NOT EXISTS chain_cursor (
                network TEXT NOT NULL,
                account TEXT NOT NULL,
                last_processed_ledger_index INTEGER NOT NULL,
                last_processed_tx_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (network, account)
            )
            """,
            (),
        )

    def _existing_columns(self, table: str) -> set[str]:
        connection = self._require_connection()
        try:
            cursor = connection.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in cursor.fetchall()}
        except sqlite3.Error as exc:
            raise SidecarStateError(
                f"Failed to read sidecar state schema for {table}: {exc}"
            ) from exc

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

    def _execute_all(
        self,
        sql: str,
        parameters: tuple[Any, ...],
    ) -> list[sqlite3.Row]:
        connection = self._require_connection()
        try:
            cursor = connection.execute(sql, parameters)
            return cursor.fetchall()
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
