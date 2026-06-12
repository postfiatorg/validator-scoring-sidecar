import httpx
import pytest

from validator_scoring_sidecar.chain import (
    ChainWatcherError,
    FoundationConfig,
    PftlAccountWatcher,
    WatchedTransaction,
    resolve_foundation_publisher_address,
)
from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.scoring_client import ScoringClient
from validator_scoring_sidecar.state import SidecarState

PUBLISHER = "rFoundationPublisher"
NETWORK = "testnet"


def _entry(ledger_index, tx_hash, *, account=PUBLISHER, validated=True, memos=None, api="v2"):
    inner = {"Account": account}
    if memos is not None:
        inner["Memos"] = memos
    if api == "v2":
        return {
            "validated": validated,
            "tx_json": inner,
            "hash": tx_hash,
            "ledger_index": ledger_index,
        }
    inner = {**inner, "hash": tx_hash, "ledger_index": ledger_index}
    return {"validated": validated, "tx": inner}


def _entry_ledger(entry):
    if entry.get("ledger_index") is not None:
        return entry["ledger_index"]
    tx = entry.get("tx_json") or entry.get("tx")
    return tx["ledger_index"]


class FakeRpc:
    """Marker-paginated account_tx fake, ascending by ledger index."""

    def __init__(self, entries, *, page_size=10):
        self.entries = list(entries)
        self.page_size = page_size
        self.calls = []

    def account_tx(self, *, account, ledger_index_min, ledger_index_max, forward, limit, marker):
        self.calls.append({"min": ledger_index_min, "max": ledger_index_max, "marker": marker})
        assert account == PUBLISHER
        assert forward is True
        assert ledger_index_max == -1
        if ledger_index_min == -1:
            pool = list(self.entries)
        else:
            pool = [e for e in self.entries if _entry_ledger(e) >= ledger_index_min]
        start = marker or 0
        page = pool[start : start + self.page_size]
        next_start = start + self.page_size
        next_marker = next_start if next_start < len(pool) else None
        return {"transactions": page, "marker": next_marker}


def _watcher(state, rpc, *, page_limit=10):
    return PftlAccountWatcher(
        rpc_client=rpc,
        state=state,
        network=NETWORK,
        publisher_address=PUBLISHER,
        page_limit=page_limit,
    )


def test_poll_first_run_returns_only_validated_trusted_transactions(tmp_path):
    entries = [
        _entry(100, "A"),
        _entry(101, "B", account="rSomeoneElse"),
        _entry(102, "C", validated=False),
        _entry(103, "D"),
    ]
    with SidecarState(tmp_path) as state:
        rpc = FakeRpc(entries)
        result = _watcher(state, rpc).poll()

    assert [t.tx_hash for t in result] == ["A", "D"]
    assert all(isinstance(t, WatchedTransaction) for t in result)
    assert all(t.account == PUBLISHER for t in result)
    assert rpc.calls[0]["min"] == -1


def test_poll_paginates_until_marker_exhausted(tmp_path):
    entries = [_entry(100 + i, f"H{i}") for i in range(5)]
    with SidecarState(tmp_path) as state:
        rpc = FakeRpc(entries, page_size=2)
        result = _watcher(state, rpc, page_limit=2).poll()

    assert [t.tx_hash for t in result] == [f"H{i}" for i in range(5)]
    assert len(rpc.calls) == 3  # 2 + 2 + 1


def test_poll_resumes_from_cursor(tmp_path):
    entries = [_entry(100, "A"), _entry(101, "B"), _entry(102, "C")]
    with SidecarState(tmp_path) as state:
        state.set_chain_cursor(NETWORK, PUBLISHER, 101, "B")
        rpc = FakeRpc(entries)
        result = _watcher(state, rpc).poll()

    assert [t.tx_hash for t in result] == ["C"]
    assert rpc.calls[0]["min"] == 101


def test_poll_dedups_boundary_ledger_via_tx_hash(tmp_path):
    entries = [
        _entry(101, "B1"),
        _entry(101, "B2"),
        _entry(101, "B3"),
        _entry(102, "C"),
    ]
    with SidecarState(tmp_path) as state:
        state.set_chain_cursor(NETWORK, PUBLISHER, 101, "B2")
        rpc = FakeRpc(entries)
        result = _watcher(state, rpc).poll()

    assert [t.tx_hash for t in result] == ["B3", "C"]


def test_poll_raises_when_cursor_tx_missing_from_its_returned_ledger(tmp_path):
    # The cursor's ledger comes back but without the cursor transaction:
    # dropping it would silently skip unprocessed transactions, so fail loudly.
    entries = [_entry(101, "B1"), _entry(101, "B3"), _entry(102, "C")]
    with SidecarState(tmp_path) as state:
        state.set_chain_cursor(NETWORK, PUBLISHER, 101, "B2")
        rpc = FakeRpc(entries)
        with pytest.raises(ChainWatcherError):
            _watcher(state, rpc).poll()


def test_poll_does_not_raise_when_boundary_ledger_is_absent(tmp_path):
    # Node returns only ledgers newer than the cursor (e.g. boundary pruned);
    # there is nothing to dedup, so newer transactions surface without error.
    entries = [_entry(105, "E"), _entry(106, "F")]
    with SidecarState(tmp_path) as state:
        state.set_chain_cursor(NETWORK, PUBLISHER, 101, "B2")
        rpc = FakeRpc(entries)
        result = _watcher(state, rpc).poll()

    assert [t.tx_hash for t in result] == ["E", "F"]


def test_advance_cursor_persists_and_makes_next_poll_empty(tmp_path):
    entries = [_entry(100, "A"), _entry(101, "B")]
    with SidecarState(tmp_path) as state:
        rpc = FakeRpc(entries)
        watcher = _watcher(state, rpc)
        for transaction in watcher.poll():
            watcher.advance_cursor(transaction)

        cursor = state.get_chain_cursor(NETWORK, PUBLISHER)
        assert cursor.last_processed_ledger_index == 101
        assert cursor.last_processed_tx_hash == "B"
        assert watcher.poll() == []


def test_poll_tolerates_api_version_1_shape(tmp_path):
    entries = [_entry(100, "A", api="v1")]
    with SidecarState(tmp_path) as state:
        result = _watcher(state, FakeRpc(entries)).poll()

    assert [t.tx_hash for t in result] == ["A"]


def test_poll_surfaces_raw_memos_for_the_decoder(tmp_path):
    memos = [{"Memo": {"MemoType": "616263", "MemoData": "646566"}}]
    entries = [_entry(100, "A", memos=memos)]
    with SidecarState(tmp_path) as state:
        result = _watcher(state, FakeRpc(entries)).poll()

    assert result[0].memos == memos


def test_foundation_config_parses_discovery_fields():
    config = FoundationConfig.from_api_payload(
        {
            "cadence_hours": 168.0,
            "foundation_publisher_address": "rPub",
            "announcement_memo_type": "pf_dynamic_unl_round_announcement_v1",
            "announcement_commit_window_seconds": 1800,
            "announcement_reveal_window_seconds": 1800,
            "announcement_reveal_gap_seconds": 0,
        }
    )
    assert config.foundation_publisher_address == "rPub"
    assert config.announcement_memo_type == "pf_dynamic_unl_round_announcement_v1"
    assert config.commit_window_seconds == 1800
    assert config.reveal_window_seconds == 1800
    assert config.reveal_gap_seconds == 0


def test_foundation_config_tolerates_missing_fields():
    config = FoundationConfig.from_api_payload({})
    assert config.foundation_publisher_address is None
    assert config.announcement_memo_type is None
    assert config.commit_window_seconds is None


def test_fetch_config_reads_scoring_config_endpoint():
    def handler(request):
        assert request.url.path == "/api/scoring/config"
        return httpx.Response(200, json={"foundation_publisher_address": "rPub"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ScoringClient(
        load_config(base_url="https://scoring.example.org", environ={}),
        http_client=http_client,
    )

    payload = client.fetch_config()
    assert FoundationConfig.from_api_payload(payload).foundation_publisher_address == "rPub"


def test_resolve_publisher_address_prefers_override():
    config = load_config(
        network=NETWORK, foundation_publisher_address="rOverride", environ={}
    )
    fetched = FoundationConfig(
        foundation_publisher_address="rFetched",
        announcement_memo_type=None,
        commit_window_seconds=None,
        reveal_window_seconds=None,
        reveal_gap_seconds=None,
    )
    assert resolve_foundation_publisher_address(config, fetched) == "rOverride"


def test_resolve_publisher_address_falls_back_to_fetched():
    config = load_config(network=NETWORK, environ={})
    fetched = FoundationConfig(
        foundation_publisher_address="rFetched",
        announcement_memo_type=None,
        commit_window_seconds=None,
        reveal_window_seconds=None,
        reveal_gap_seconds=None,
    )
    assert resolve_foundation_publisher_address(config, fetched) == "rFetched"


def test_resolve_publisher_address_raises_when_unavailable():
    config = load_config(network=NETWORK, environ={})
    with pytest.raises(ChainWatcherError):
        resolve_foundation_publisher_address(config, None)


class _FakeXrplResponse:
    def __init__(self, result):
        self.result = result

    def is_successful(self):
        return True


class _FakeXrplClient:
    """Stands in for xrpl-py's JsonRpcClient under XrplPftlRpcClient."""

    def __init__(self, network_id=2024):
        self.network_id = network_id
        self.server_info_requests = 0

    def request(self, request):
        self.server_info_requests += 1
        return _FakeXrplResponse({"info": {"network_id": self.network_id}})


def test_submit_memo_stamps_discovered_network_id(monkeypatch):
    import xrpl.transaction
    from xrpl.core import keypairs

    from validator_scoring_sidecar.chain import XrplPftlRpcClient

    submitted = []

    def fake_submit_and_wait(transaction, client, wallet):
        submitted.append(transaction)
        return _FakeXrplResponse(
            {"meta": {"TransactionResult": "tesSUCCESS"}, "hash": "TX" * 32}
        )

    monkeypatch.setattr(xrpl.transaction, "submit_and_wait", fake_submit_and_wait)
    rpc = XrplPftlRpcClient("https://rpc.example.org")
    rpc._client = _FakeXrplClient(network_id=2024)
    seed = keypairs.generate_seed()

    for _ in range(2):
        tx_hash = rpc.submit_memo(
            wallet_seed=seed,
            destination="rDestination",
            memo_type="pf_test",
            memo_data="{}",
        )
        assert tx_hash == "TX" * 32

    # PFTL networks (id > 1024) reject transactions without NetworkID with
    # telREQUIRES_NETWORK_ID, and xrpl-py's autofill cannot supply it against
    # postfiatd's fork build version — the client must stamp it itself.
    assert all(tx.network_id == 2024 for tx in submitted)
    # Discovered once via server_info, then cached across submissions.
    assert rpc._client.server_info_requests == 1
