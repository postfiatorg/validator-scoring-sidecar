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

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.input_package import (
    SOURCE_AUTO,
    FetchedInputPackage,
    fetch_input_package,
)
from validator_scoring_sidecar.round_metadata import (
    MissingFrozenInputMetadata,
    RoundMetadata,
    RoundMetadataError,
    round_identifier,
)
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.scoring_client import ScoringClient
from validator_scoring_sidecar.state import ChainCursor, SidecarState
from validator_scoring_sidecar.sync import DEFAULT_SYNC_ROUND_LIMIT

DEFAULT_ACCOUNT_TX_PAGE_LIMIT = 200
# xrpl uses -1 to mean "the latest validated ledger" for the upper bound and
# "the earliest available ledger" for the lower bound on a first scan.
VALIDATED_LEDGER_INDEX = -1
# Bounds the clamp-and-retry loop when the cursor is below the node's retained
# history. Each retry re-reads the floor, so this only needs to absorb the
# pruning-boundary race (the floor advancing mid-recovery), not a deep walk.
MAX_PRUNED_LEDGER_RETRIES = 3


class ChainWatcherError(RuntimeError):
    """Base error for chain watcher operations."""


class PftlRpcError(ChainWatcherError):
    """Raised when the PFTL RPC endpoint cannot be reached or returns an error."""


class PftlInsufficientFundsError(PftlRpcError):
    """Raised when a transaction is rejected for insufficient balance or fee."""


class PftlPrunedLedgerError(PftlRpcError):
    """Raised when an ``account_tx`` lower bound is below the node's retained
    history (``lgrIdxMalformed``) — i.e. the requested ledgers have been pruned
    off a non-archive node. Distinct from a generic RPC error so the watcher can
    recover by clamping the floor forward rather than failing the pass."""


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

    def earliest_validated_ledger(self) -> int: ...

    def latest_validated_ledger_close_time(self) -> datetime: ...

    def account_balance_drops(self, address: str) -> int | None: ...

    def submit_memo(
        self,
        *,
        wallet_seed: str,
        destination: str,
        memo_type: str,
        memo_data: str,
    ) -> str: ...


class XrplPftlRpcClient:
    """``PftlRpcClient`` backed by xrpl-py's JSON-RPC client.

    xrpl-py is imported lazily so the pure watcher logic stays unit-testable
    against a fake transport without importing the dependency.
    """

    def __init__(self, rpc_url: str):
        self._rpc_url = rpc_url
        self._client: Any | None = None
        self._cached_network_id: int | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from xrpl.clients import JsonRpcClient

            self._client = JsonRpcClient(self._rpc_url)
        return self._client

    def _resolve_network_id(self) -> int | None:
        """Fetch and cache the chain's network ID for transaction stamping.

        PFTL networks use IDs above 1024, where every transaction must carry an
        explicit ``NetworkID`` or be rejected with ``telREQUIRES_NETWORK_ID``.
        xrpl-py's autofill cannot supply it against postfiatd, because the fork
        reports its own build version (pre-1.11 by rippled numbering) and the
        autofill version gate skips the field. Discovered from ``server_info``
        so the operator never configures it.
        """

        if self._cached_network_id is None:
            from xrpl.models.requests import ServerInfo

            try:
                response = self._ensure_client().request(ServerInfo())
            except Exception as exc:  # noqa: BLE001 - surface any transport failure uniformly
                raise PftlRpcError(f"PFTL server_info request failed: {exc}") from exc
            if not response.is_successful():
                raise PftlRpcError(
                    f"PFTL server_info returned an error: {response.result}"
                )
            network_id = response.result.get("info", {}).get("network_id")
            self._cached_network_id = int(network_id) if network_id is not None else 0
        return self._cached_network_id or None

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
            if _is_pruned_ledger(response.result):
                raise PftlPrunedLedgerError(
                    f"PFTL account_tx lower bound {ledger_index_min} is below the "
                    f"node's retained history for {account}: {response.result}"
                )
            raise PftlRpcError(
                f"PFTL account_tx returned an error for {account}: {response.result}"
            )
        return response.result

    def earliest_validated_ledger(self) -> int:
        from xrpl.models.requests import ServerInfo

        try:
            response = self._ensure_client().request(ServerInfo())
        except Exception as exc:  # noqa: BLE001 - surface any transport failure uniformly
            raise PftlRpcError(f"PFTL server_info request failed: {exc}") from exc
        if not response.is_successful():
            raise PftlRpcError(
                f"PFTL server_info returned an error: {response.result}"
            )
        complete = response.result.get("info", {}).get("complete_ledgers")
        return _earliest_complete_ledger(complete)

    def latest_validated_ledger_close_time(self) -> datetime:
        from xrpl.models.requests import Ledger
        from xrpl.utils import ripple_time_to_datetime

        try:
            response = self._ensure_client().request(Ledger(ledger_index="validated"))
        except Exception as exc:  # noqa: BLE001 - surface any transport failure uniformly
            raise PftlRpcError(f"PFTL ledger request failed: {exc}") from exc
        if not response.is_successful():
            raise PftlRpcError(
                f"PFTL ledger request returned an error: {response.result}"
            )
        try:
            close_time = response.result["ledger"]["close_time"]
        except (KeyError, TypeError) as exc:
            raise PftlRpcError("PFTL ledger response has no close_time") from exc
        return ripple_time_to_datetime(int(close_time))

    def account_balance_drops(self, address: str) -> int | None:
        """Validated balance of an account in drops, or ``None`` when the account
        does not exist on the ledger yet (never funded). Raises ``PftlRpcError``
        on transport or other RPC failures so a reachability problem is distinct
        from an unfunded account."""
        from xrpl.models.requests import AccountInfo

        try:
            response = self._ensure_client().request(
                AccountInfo(account=address, ledger_index="validated")
            )
        except Exception as exc:  # noqa: BLE001 - surface any transport failure uniformly
            raise PftlRpcError(
                f"PFTL account_info request failed for {address}: {exc}"
            ) from exc
        if not response.is_successful():
            if response.result.get("error") == "actNotFound":
                return None
            raise PftlRpcError(
                f"PFTL account_info returned an error for {address}: {response.result}"
            )
        try:
            return int(response.result["account_data"]["Balance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PftlRpcError(
                f"PFTL account_info response has no Balance for {address}"
            ) from exc

    def submit_memo(
        self,
        *,
        wallet_seed: str,
        destination: str,
        memo_type: str,
        memo_data: str,
    ) -> str:
        from xrpl.models.transactions import Memo, Payment
        from xrpl.transaction import submit_and_wait
        from xrpl.utils import str_to_hex

        from validator_scoring_sidecar.wallet import relay_wallet_from_secret

        wallet = relay_wallet_from_secret(wallet_seed)
        payment = Payment(
            account=wallet.classic_address,
            destination=destination,
            amount="1",
            network_id=self._resolve_network_id(),
            memos=[
                Memo(
                    memo_type=str_to_hex(memo_type),
                    memo_data=str_to_hex(memo_data),
                )
            ],
        )
        try:
            response = submit_and_wait(payment, self._ensure_client(), wallet)
        except Exception as exc:  # noqa: BLE001 - normalize xrpl submission failures
            if _is_insufficient_funds(str(exc)):
                raise PftlInsufficientFundsError(str(exc)) from exc
            raise PftlRpcError(f"PFTL commit submission failed: {exc}") from exc
        result = response.result
        engine_result = result.get("meta", {}).get("TransactionResult") or result.get(
            "engine_result"
        )
        if engine_result not in (None, "tesSUCCESS"):
            if _is_insufficient_funds(str(engine_result)):
                raise PftlInsufficientFundsError(f"PFTL commit rejected: {engine_result}")
            raise PftlRpcError(f"PFTL commit rejected: {engine_result}")
        tx_hash = result.get("hash") or result.get("tx_json", {}).get("hash")
        if not isinstance(tx_hash, str):
            raise PftlRpcError("PFTL commit submission returned no transaction hash")
        return tx_hash


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
        """Page through ``account_tx`` from ``ledger_index_min``.

        If the stored cursor has fallen below the node's retained history (a
        pruning, non-archive node after an idle gap), the node rejects the
        lower bound with ``lgrIdxMalformed``; recover by clamping the floor
        forward to the node's earliest available ledger and retrying. Skipped
        ledgers only ever hold announcements whose commit windows have already
        closed — a node retains far more history than any commit window is long
        — so the clamp never costs a committable round. Re-reading the floor on
        each retry absorbs the boundary race where the node prunes further
        mid-recovery.
        """

        retries = 0
        while True:
            try:
                return self._fetch_pages(ledger_index_min)
            except PftlPrunedLedgerError:
                retries += 1
                if retries > MAX_PRUNED_LEDGER_RETRIES:
                    raise
                floor = self._rpc.earliest_validated_ledger()
                if ledger_index_min != VALIDATED_LEDGER_INDEX:
                    floor = max(floor, ledger_index_min + 1)
                ledger_index_min = floor

    def _fetch_pages(self, ledger_index_min: int) -> list[dict[str, Any]]:
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


ANNOUNCEMENT_MEMO_FIELD = "Memo"
MEMO_TYPE_FIELD = "MemoType"
MEMO_DATA_FIELD = "MemoData"


class AnnouncementError(ChainWatcherError):
    """A round announcement could not be decoded, validated, or content-bound.

    Carries a ``FailureCategory`` (``MANIFEST_UNSUPPORTED`` by default) so the
    caller can record the round as skipped under the shared failure taxonomy.
    """

    def __init__(
        self,
        message: str,
        *,
        category: FailureCategory = FailureCategory.MANIFEST_UNSUPPORTED,
    ):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class VerifiedAnnouncement:
    """A decoded round announcement bound to a hash-verified input package."""

    announcement: commit_reveal.RoundAnnouncement
    package: FetchedInputPackage


def decode_round_announcement(
    transaction: WatchedTransaction,
) -> commit_reveal.RoundAnnouncement | None:
    """Decode and validate the round-announcement memo from a transaction.

    Returns ``None`` when the transaction carries no round-announcement memo
    (the watcher surfaces every trusted-sender transaction, not only
    announcements). Raises ``AnnouncementError`` when an announcement memo is
    present but its payload is malformed or fails protocol validation.
    """

    payload = _select_announcement_payload(transaction)
    if payload is None:
        return None
    try:
        return commit_reveal.validate_round_announcement(payload)
    except commit_reveal.CommitRevealValidationError as exc:
        raise AnnouncementError(f"invalid round announcement: {exc}") from exc


def verify_announced_package(
    announcement: commit_reveal.RoundAnnouncement,
    config: SidecarConfig,
    client: ScoringClient,
    *,
    package_fetcher=fetch_input_package,
    round_limit: int = DEFAULT_SYNC_ROUND_LIMIT,
) -> FetchedInputPackage:
    """Bind an announcement to a frozen input package the sidecar verifies.

    The memo carries only pointers; the package itself lives in IPFS. This
    resolves the announced round by ``input_package_hash`` against recent rounds,
    confirms the ``input_package_cid`` and network agree, and fetches-and-verifies
    the package by hash through the existing M2.4 retrieval path. Raises
    ``AnnouncementError`` on any mismatch.
    """

    if announcement.network != config.network:
        raise AnnouncementError(
            f"announcement network {announcement.network!r} does not match "
            f"configured network {config.network!r}"
        )
    metadata = _resolve_announced_round(announcement, client, round_limit)
    if metadata.input_package_cid != announcement.input_package_cid:
        raise AnnouncementError(
            "announcement input_package_cid does not match the round's "
            "input_package_cid"
        )
    return package_fetcher(metadata, config, client, source=SOURCE_AUTO, force=False)


def decode_and_verify_announcement(
    transaction: WatchedTransaction,
    config: SidecarConfig,
    client: ScoringClient,
    *,
    package_fetcher=fetch_input_package,
    round_limit: int = DEFAULT_SYNC_ROUND_LIMIT,
) -> VerifiedAnnouncement | None:
    """Decode, validate, and content-bind a watcher-surfaced transaction.

    Returns ``None`` if the transaction is not a round announcement, a
    ``VerifiedAnnouncement`` when it decodes and binds to a verified package, and
    raises ``AnnouncementError`` on a malformed or unbindable announcement.
    """

    announcement = decode_round_announcement(transaction)
    if announcement is None:
        return None
    package = verify_announced_package(
        announcement,
        config,
        client,
        package_fetcher=package_fetcher,
        round_limit=round_limit,
    )
    return VerifiedAnnouncement(announcement=announcement, package=package)


def _select_announcement_payload(
    transaction: WatchedTransaction,
) -> dict[str, Any] | None:
    for memo in transaction.memos:
        inner = memo.get(ANNOUNCEMENT_MEMO_FIELD) if isinstance(memo, dict) else None
        if not isinstance(inner, dict):
            continue
        memo_type_hex = inner.get(MEMO_TYPE_FIELD)
        if not isinstance(memo_type_hex, str):
            continue
        if _decode_hex_text(memo_type_hex) != commit_reveal.ROUND_ANNOUNCEMENT_TYPE:
            continue
        return _decode_announcement_memo_data(inner.get(MEMO_DATA_FIELD))
    return None


def _decode_announcement_memo_data(memo_data_hex: Any) -> dict[str, Any]:
    if not isinstance(memo_data_hex, str):
        raise AnnouncementError("round announcement memo is missing MemoData")
    text = _decode_hex_text(memo_data_hex)
    if text is None:
        raise AnnouncementError("round announcement MemoData is not valid hex/UTF-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnnouncementError(
            f"round announcement MemoData is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AnnouncementError("round announcement MemoData must be a JSON object")
    return payload


def _decode_hex_text(value: str) -> str | None:
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _is_insufficient_funds(text: str) -> bool:
    lowered = text.lower()
    return "insuf" in lowered or "unfunded" in lowered


def _is_pruned_ledger(result: Any) -> bool:
    """True when an account_tx error means the lower bound predates retained
    history (``lgrIdxMalformed`` / error code 58).

    Code 58 is rippled's general malformed-ledger-index error — it also covers a
    non-integer index or ``min > max``. It unambiguously means "pruned" only
    because the watcher always sends a valid integer lower bound with
    ``max == -1``; do not reuse this helper for calls without that guarantee."""

    if not isinstance(result, dict):
        return False
    return result.get("error") == "lgrIdxMalformed" or result.get("error_code") == 58


def _earliest_complete_ledger(value: Any) -> int:
    """Earliest validated ledger the node still retains, parsed from the
    ``complete_ledgers`` range string (e.g. ``"1850169-1906374"``; ranges are
    comma-separated and the first one's start is the earliest available)."""

    if not isinstance(value, str) or not value.strip() or value.strip() == "empty":
        raise PftlRpcError(
            f"PFTL server_info has no usable complete_ledgers range: {value!r}"
        )
    low = value.split(",")[0].split("-")[0].strip()
    try:
        return int(low)
    except ValueError as exc:
        raise PftlRpcError(
            f"PFTL complete_ledgers is not a ledger range: {value!r}"
        ) from exc


def find_memo_payload(
    memos: list[Any],
    memo_type: str,
) -> dict[str, Any] | None:
    """Return the JSON payload of the first memo of ``memo_type``, leniently.

    Malformed or non-matching memos are skipped (no raise), so this is safe for
    scanning historical transactions — e.g. checking whether a commit already
    exists for a round before submitting another.
    """

    for memo in memos:
        decoded = _decode_memo(memo)
        if decoded is not None and decoded[0] == memo_type:
            return decoded[1]
    return None


def find_authored_memo(
    rpc_client: PftlRpcClient,
    *,
    account: str,
    memo_type: str,
    validate: Callable[[dict[str, Any]], Any],
    network: str,
    round_number: int,
    input_package_hash: str,
    validator_master_key: str,
    limit: int,
) -> tuple[str, Any] | None:
    """Return the ``(tx_hash, validated_payload)`` of the most recent ``account``
    transaction carrying a ``memo_type`` memo that binds to this round and
    validator, or ``None`` when there is none.

    Shared by commit and reveal idempotency: before submitting, scan the
    foundation publisher account's recent validated history for a payload this
    validator already authored for the round. ``validate`` is the protocol
    validator for the memo kind (``validate_commit_payload`` /
    ``validate_reveal_payload``); a payload that fails validation is skipped. The
    validated payload is returned alongside the hash so a caller can compare
    protocol fields it does not match on here (the commit recovery path checks
    the on-chain ``commitment_hash`` against local state before revealing).
    """

    result = rpc_client.account_tx(
        account=account,
        ledger_index_min=-1,
        ledger_index_max=-1,
        forward=False,
        limit=limit,
        marker=None,
    )
    entries = result.get("transactions")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tx = entry.get("tx_json") or entry.get("tx")
        memos = tx.get("Memos") if isinstance(tx, dict) else None
        if not isinstance(memos, list):
            continue
        payload = find_memo_payload(memos, memo_type)
        if payload is None:
            continue
        try:
            authored = validate(payload)
        except commit_reveal.CommitRevealValidationError:
            continue
        if (
            authored.network == network
            and authored.round_number == round_number
            and authored.input_package_hash == input_package_hash
            and authored.validator_master_key == validator_master_key
        ):
            tx_hash = entry.get("hash") or (
                tx.get("hash") if isinstance(tx, dict) else None
            )
            if isinstance(tx_hash, str):
                return tx_hash, authored
    return None


def _decode_memo(memo: Any) -> tuple[str, dict[str, Any]] | None:
    inner = memo.get(ANNOUNCEMENT_MEMO_FIELD) if isinstance(memo, dict) else None
    if not isinstance(inner, dict):
        return None
    memo_type_hex = inner.get(MEMO_TYPE_FIELD)
    memo_data_hex = inner.get(MEMO_DATA_FIELD)
    if not isinstance(memo_type_hex, str) or not isinstance(memo_data_hex, str):
        return None
    memo_type = _decode_hex_text(memo_type_hex)
    text = _decode_hex_text(memo_data_hex)
    if memo_type is None or text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return memo_type, payload


def _resolve_announced_round(
    announcement: commit_reveal.RoundAnnouncement,
    client: ScoringClient,
    round_limit: int,
) -> RoundMetadata:
    for payload in client.fetch_rounds(limit=round_limit):
        try:
            metadata = RoundMetadata.from_api_payload(
                payload,
                requested_round_id=round_identifier(payload),
            )
        except (MissingFrozenInputMetadata, RoundMetadataError):
            continue
        if metadata.input_package_hash == announcement.input_package_hash:
            return metadata
    raise AnnouncementError(
        "no recent round matches announced input_package_hash "
        f"{announcement.input_package_hash}"
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
