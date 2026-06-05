"""PFTL chain watcher for the Dynamic UNL sidecar (milestone M2.5.1).

Observes the foundation publisher account's validated ``account_tx`` history and
surfaces trusted-sender transactions so a later step can decode the round
announcement. The watcher is a window/anchor provider for the existing
API-driven score path, not a round trigger.

Announcement authenticity comes from the validated-ledger sender: PFTL signs and
validates every transaction's sending account, so a transaction seen in
validated history from the foundation publisher account is provably
foundation-authored. No application-level signature is checked here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.state import ChainCursor, SidecarState

DEFAULT_ACCOUNT_TX_PAGE_LIMIT = 200
# xrpl uses -1 to mean "the latest validated ledger" for the upper bound and
# "the earliest available ledger" for the lower bound on a first scan.
VALIDATED_LEDGER_INDEX = -1


class ChainWatcherError(RuntimeError):
    """Base error for chain watcher operations."""


class PftlRpcError(ChainWatcherError):
    """Raised when the PFTL RPC endpoint cannot be reached or returns an error."""


@dataclass(frozen=True)
class FoundationConfig:
    """Chain-discovery values read from the foundation ``/api/scoring/config``."""

    foundation_publisher_address: str | None
    announcement_memo_type: str | None
    commit_window_seconds: int | None
    reveal_window_seconds: int | None
    reveal_gap_seconds: int | None

    @classmethod
    def from_api_payload(cls, payload: dict[str, Any]) -> "FoundationConfig":
        return cls(
            foundation_publisher_address=_optional_str(
                payload, "foundation_publisher_address"
            ),
            announcement_memo_type=_optional_str(payload, "announcement_memo_type"),
            commit_window_seconds=_optional_int(
                payload, "announcement_commit_window_seconds"
            ),
            reveal_window_seconds=_optional_int(
                payload, "announcement_reveal_window_seconds"
            ),
            reveal_gap_seconds=_optional_int(
                payload, "announcement_reveal_gap_seconds"
            ),
        )


@dataclass(frozen=True)
class WatchedTransaction:
    """A trusted-sender transaction surfaced by the watcher, still undecoded.

    ``memos`` is the raw XRPL ``Memos`` array (hex-encoded fields); decoding it
    into a round announcement is the next milestone step (M2.5.2).
    """

    tx_hash: str
    ledger_index: int
    account: str
    memos: list[dict[str, Any]]
    tx: dict[str, Any]


class PftlRpcClient(Protocol):
    """Minimal ``account_tx`` transport, injectable so tests avoid a live node."""

    def account_tx(
        self,
        *,
        account: str,
        ledger_index_min: int,
        ledger_index_max: int,
        forward: bool,
        limit: int,
        marker: Any | None,
    ) -> dict[str, Any]: ...


class XrplPftlRpcClient:
    """``PftlRpcClient`` backed by xrpl-py's JSON-RPC client.

    xrpl-py is imported lazily so the pure watcher logic stays unit-testable
    against a fake transport without importing the dependency.
    """

    def __init__(self, rpc_url: str):
        self._rpc_url = rpc_url
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from xrpl.clients import JsonRpcClient

            self._client = JsonRpcClient(self._rpc_url)
        return self._client

    def account_tx(
        self,
        *,
        account: str,
        ledger_index_min: int,
        ledger_index_max: int,
        forward: bool,
        limit: int,
        marker: Any | None,
    ) -> dict[str, Any]:
        from xrpl.models.requests import AccountTx

        request = AccountTx(
            account=account,
            ledger_index_min=ledger_index_min,
            ledger_index_max=ledger_index_max,
            forward=forward,
            limit=limit,
            marker=marker,
        )
        try:
            response = self._ensure_client().request(request)
        except Exception as exc:  # noqa: BLE001 - surface any transport failure uniformly
            raise PftlRpcError(
                f"PFTL account_tx request failed for {account}: {exc}"
            ) from exc
        if not response.is_successful():
            raise PftlRpcError(
                f"PFTL account_tx returned an error for {account}: {response.result}"
            )
        return response.result


class PftlAccountWatcher:
    """Polls validated ``account_tx`` for the foundation publisher account.

    ``poll`` is a read: it returns trusted-sender transactions newer than the
    persisted cursor, in ascending ledger order, without advancing the cursor.
    Callers advance the cursor with ``advance_cursor`` only after a transaction
    has been durably handled, so a crash mid-handling re-surfaces it rather than
    skipping it (at-least-once delivery).
    """

    def __init__(
        self,
        *,
        rpc_client: PftlRpcClient,
        state: SidecarState,
        network: str,
        publisher_address: str,
        page_limit: int = DEFAULT_ACCOUNT_TX_PAGE_LIMIT,
    ):
        self._rpc = rpc_client
        self._state = state
        self._network = network
        self._account = publisher_address
        self._page_limit = page_limit

    def poll(self) -> list[WatchedTransaction]:
        cursor = self._state.get_chain_cursor(self._network, self._account)
        ledger_index_min = (
            cursor.last_processed_ledger_index
            if cursor is not None
            else VALIDATED_LEDGER_INDEX
        )
        entries = self._fetch_all(ledger_index_min)
        trusted = [
            transaction
            for entry in entries
            if (transaction := self._to_trusted(entry)) is not None
        ]
        trusted.sort(key=lambda transaction: transaction.ledger_index)
        return self._after_cursor(trusted, cursor)

    def advance_cursor(self, transaction: WatchedTransaction) -> None:
        self._state.set_chain_cursor(
            self._network,
            self._account,
            transaction.ledger_index,
            transaction.tx_hash,
        )

    def _fetch_all(self, ledger_index_min: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        marker: Any | None = None
        while True:
            result = self._rpc.account_tx(
                account=self._account,
                ledger_index_min=ledger_index_min,
                ledger_index_max=VALIDATED_LEDGER_INDEX,
                forward=True,
                limit=self._page_limit,
                marker=marker,
            )
            page = result.get("transactions")
            if isinstance(page, list):
                entries.extend(item for item in page if isinstance(item, dict))
            marker = result.get("marker")
            if marker is None:
                break
        return entries

    def _to_trusted(self, entry: dict[str, Any]) -> WatchedTransaction | None:
        if entry.get("validated") is not True:
            return None
        # Tolerate api_version 1 ("tx") and 2 ("tx_json") account_tx shapes.
        tx = entry.get("tx_json") or entry.get("tx")
        if not isinstance(tx, dict):
            return None
        if tx.get("Account") != self._account:
            return None
        tx_hash = entry.get("hash") or tx.get("hash")
        ledger_index = entry.get("ledger_index")
        if ledger_index is None:
            ledger_index = tx.get("ledger_index")
        if not isinstance(tx_hash, str) or not isinstance(ledger_index, int):
            return None
        memos_raw = tx.get("Memos")
        memos = (
            [memo for memo in memos_raw if isinstance(memo, dict)]
            if isinstance(memos_raw, list)
            else []
        )
        return WatchedTransaction(
            tx_hash=tx_hash,
            ledger_index=ledger_index,
            account=self._account,
            memos=memos,
            tx=tx,
        )

    def _after_cursor(
        self,
        transactions: list[WatchedTransaction],
        cursor: ChainCursor | None,
    ) -> list[WatchedTransaction]:
        if cursor is None:
            return list(transactions)
        result: list[WatchedTransaction] = []
        seen_cursor_tx = False
        saw_boundary_ledger = False
        for transaction in transactions:
            if transaction.ledger_index < cursor.last_processed_ledger_index:
                continue
            if transaction.ledger_index == cursor.last_processed_ledger_index:
                # Re-querying from the cursor ledger returns transactions already
                # processed in that ledger; drop them up to and including the
                # cursor transaction, then keep the rest of the ledger. This
                # relies on account_tx returning a stable within-ledger order
                # across polls (rippled orders by transaction index).
                saw_boundary_ledger = True
                if seen_cursor_tx:
                    result.append(transaction)
                elif transaction.tx_hash == cursor.last_processed_tx_hash:
                    seen_cursor_tx = True
                continue
            result.append(transaction)
        if saw_boundary_ledger and not seen_cursor_tx:
            # The cursor's ledger was returned but the cursor transaction itself
            # was not found in it. Dropping that ledger would silently skip
            # unprocessed transactions, so fail loudly: the PFTL node at the
            # configured RPC URL likely lacks the history needed to resume.
            raise ChainWatcherError(
                "cursor transaction "
                f"{cursor.last_processed_tx_hash} was not found in its ledger "
                f"{cursor.last_processed_ledger_index}; the PFTL node may lack "
                "the history required to resume safely"
            )
        return result


def resolve_foundation_publisher_address(
    config: SidecarConfig,
    foundation_config: FoundationConfig | None,
) -> str:
    """Effective publisher address: explicit override, then fetched config.

    Mirrors the rest of the sidecar's precedence: an operator override (CLI/env,
    carried on ``config``) wins, otherwise the value discovered from the
    foundation config endpoint is used. There is no hardcoded fallback address.
    """

    if config.foundation_publisher_address:
        return config.foundation_publisher_address
    if foundation_config is not None and foundation_config.foundation_publisher_address:
        return foundation_config.foundation_publisher_address
    raise ChainWatcherError(
        "No foundation publisher address available; set "
        "POSTFIAT_SIDECAR_FOUNDATION_PUBLISHER_ADDRESS / --foundation-publisher-address "
        "or ensure the scoring service /api/scoring/config exposes it"
    )


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
