import json
import sqlite3
from datetime import datetime, timezone

import pytest
from xrpl.core import keypairs
from xrpl.core.addresscodec import encode_node_public_key

from validator_scoring_sidecar.chain import PftlInsufficientFundsError
from validator_scoring_sidecar.commit import submit_commit
from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.reveal import (
    REVEAL_STATUS_ALREADY_REVEALED,
    REVEAL_STATUS_NOT_COMMITTED,
    REVEAL_STATUS_SKIPPED_LOW_BALANCE,
    REVEAL_STATUS_SUBMITTED,
    REVEAL_STATUS_WINDOW_MISSED,
    REVEAL_STATUS_WINDOW_NOT_OPEN,
    RevealError,
    submit_reveal,
)
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.state import (
    STATE_COMMITTED,
    STATE_DB_FILENAME,
    STATE_REVEALED,
    STATE_SCORED,
    ScoreOutcome,
    SidecarState,
)
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    HASH_SELECTED_UNL,
    HASH_VALIDATOR_SCORES,
)

NETWORK = "testnet"
PUBLISHER = "rFoundationPublisher"
CID = "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"
INPUT_HASH = "a" * 64
ROUND_NUMBER = 456
ROUND_ID = 123
OUTPUT_HASHES = {
    HASH_MODEL_RESPONSE: "1" * 64,
    HASH_VALIDATOR_SCORES: "2" * 64,
    HASH_SELECTED_UNL: "3" * 64,
}

# commit window [00:00, 00:30); reveal window [00:30, 01:00).
COMMIT_TIME = datetime(2026, 5, 25, 0, 15, tzinfo=timezone.utc)
BEFORE_REVEAL = datetime(2026, 5, 25, 0, 20, tzinfo=timezone.utc)
IN_REVEAL = datetime(2026, 5, 25, 0, 45, tzinfo=timezone.utc)
AFTER_REVEAL = datetime(2026, 5, 25, 1, 5, tzinfo=timezone.utc)


class FakeSigner:
    """Real-crypto signer over a generated node keypair (no validator-keys tool)."""

    def __init__(self):
        seed = keypairs.generate_seed()
        self._public, self._private = keypairs.derive_keypair(seed)
        self._master_key = encode_node_public_key(bytes.fromhex(self._public))

    @property
    def master_key(self) -> str:
        return self._master_key

    def sign(self, message: bytes) -> str:
        return keypairs.sign(message, self._private)


class FakeRpc:
    def __init__(
        self, *, close_time, transactions=None, submit_error=None, submit_hash="REVEALTX456"
    ):
        self.close_time = close_time
        self.transactions = transactions or []
        self.submit_error = submit_error
        self.submit_hash = submit_hash
        self.submitted = []

    def latest_validated_ledger_close_time(self):
        return self.close_time

    def account_tx(self, *, account, ledger_index_min, ledger_index_max, forward, limit, marker):
        return {"transactions": self.transactions}

    def submit_memo(self, *, wallet_seed, destination, memo_type, memo_data):
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(
            {"destination": destination, "memo_type": memo_type, "memo_data": memo_data}
        )
        return self.submit_hash


def _announcement():
    return commit_reveal.build_round_announcement(
        network=NETWORK,
        round_number=ROUND_NUMBER,
        input_package_cid=CID,
        input_package_hash=INPUT_HASH,
        commit_opens_at="2026-05-25T00:00:00+00:00",
        commit_closes_at="2026-05-25T00:30:00+00:00",
        reveal_opens_at="2026-05-25T00:30:00+00:00",
        reveal_closes_at="2026-05-25T01:00:00+00:00",
    )


def _metadata():
    return RoundMetadata(
        round_id=ROUND_ID,
        round_number=ROUND_NUMBER,
        status="INPUT_FROZEN",
        input_package_cid=CID,
        input_package_hash=INPUT_HASH,
        input_frozen_at="2026-05-25T00:00:00+00:00",
        final_bundle_cid=None,
    )


def _config(tmp_path, *, with_seed=True):
    environ = {"POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED": "sEdTESTseed"} if with_seed else {}
    return load_config(network=NETWORK, data_dir=tmp_path, environ=environ)


def _commit_round(state, tmp_path, signer):
    """Drive the real score -> commit path so the round is COMMITTED with a
    stored commitment and the output fingerprints the reveal replays."""
    state.record_score(
        NETWORK,
        _metadata(),
        ScoreOutcome(
            sidecar_state=STATE_SCORED,
            backend_mode="modal",
            model_response_hash=OUTPUT_HASHES[HASH_MODEL_RESPONSE],
            validator_scores_hash=OUTPUT_HASHES[HASH_VALIDATOR_SCORES],
            selected_unl_hash=OUTPUT_HASHES[HASH_SELECTED_UNL],
        ),
    )
    rpc = FakeRpc(close_time=COMMIT_TIME, submit_hash="COMMITTX123")
    result = submit_commit(
        _announcement(),
        OUTPUT_HASHES,
        _config(tmp_path),
        _metadata(),
        rpc_client=rpc,
        signer=signer,
        state=state,
        foundation_publisher_address=PUBLISHER,
    )
    assert result.commit_tx_hash == "COMMITTX123"


def _reveal_memo(signer, *, salt="e" * 64):
    output_hashes = commit_reveal.OutputHashes(
        model_response_hash=OUTPUT_HASHES[HASH_MODEL_RESPONSE],
        validator_scores_hash=OUTPUT_HASHES[HASH_VALIDATOR_SCORES],
        selected_unl_hash=OUTPUT_HASHES[HASH_SELECTED_UNL],
    )
    signing_bytes = commit_reveal.build_reveal_signing_bytes(
        protocol_version=commit_reveal.PROTOCOL_VERSION,
        network=NETWORK,
        round_number=ROUND_NUMBER,
        validator_master_key=signer.master_key,
        input_package_hash=INPUT_HASH,
        output_hashes=output_hashes,
        salt=salt,
    )
    payload = commit_reveal.build_reveal_payload(
        protocol_version=commit_reveal.PROTOCOL_VERSION,
        network=NETWORK,
        round_number=ROUND_NUMBER,
        validator_master_key=signer.master_key,
        input_package_hash=INPUT_HASH,
        output_hashes=output_hashes,
        salt=salt,
        signature=signer.sign(signing_bytes),
    )
    data = commit_reveal.canonical_json_bytes(payload).decode("utf-8")
    return {
        "Memo": {
            "MemoType": commit_reveal.VALIDATOR_REVEAL_TYPE.encode("utf-8").hex(),
            "MemoData": data.encode("utf-8").hex(),
        }
    }


def test_submit_reveal_success(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)
        rpc = FakeRpc(close_time=IN_REVEAL)
        result = submit_reveal(
            _config(tmp_path),
            _metadata(),
            rpc_client=rpc,
            signer=signer,
            state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == REVEAL_STATUS_SUBMITTED
    assert result.reveal_tx_hash == "REVEALTX456"
    assert record.sidecar_state == STATE_REVEALED
    assert record.reveal_tx_hash == "REVEALTX456"
    assert len(rpc.submitted) == 1
    assert rpc.submitted[0]["memo_type"] == commit_reveal.VALIDATOR_REVEAL_TYPE
    assert rpc.submitted[0]["destination"] == PUBLISHER
    payload = commit_reveal.validate_reveal_payload(json.loads(rpc.submitted[0]["memo_data"]))
    assert commit_reveal.verify_reveal_signature(payload)
    # The reveal opens the commitment stored at commit time.
    assert commit_reveal.compute_reveal_commitment_hash(payload) == record.commitment_hash


def test_submit_reveal_is_locally_idempotent(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)
        first = submit_reveal(
            _config(tmp_path), _metadata(),
            rpc_client=FakeRpc(close_time=IN_REVEAL), signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        second_rpc = FakeRpc(close_time=IN_REVEAL)
        second = submit_reveal(
            _config(tmp_path), _metadata(),
            rpc_client=second_rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )

    assert first.status == REVEAL_STATUS_SUBMITTED
    assert second.status == REVEAL_STATUS_ALREADY_REVEALED
    assert second.reveal_tx_hash == "REVEALTX456"
    assert second_rpc.submitted == []


def test_submit_reveal_detects_existing_onchain_reveal(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)
        existing = {
            "tx_json": {"Account": "rRelay", "Memos": [_reveal_memo(signer)]},
            "hash": "ONCHAINREVEAL",
        }
        rpc = FakeRpc(close_time=IN_REVEAL, transactions=[existing])
        result = submit_reveal(
            _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == REVEAL_STATUS_ALREADY_REVEALED
    assert result.reveal_tx_hash == "ONCHAINREVEAL"
    assert record.sidecar_state == STATE_REVEALED
    assert rpc.submitted == []


def test_submit_reveal_window_not_open(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)
        rpc = FakeRpc(close_time=BEFORE_REVEAL)
        result = submit_reveal(
            _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == REVEAL_STATUS_WINDOW_NOT_OPEN
    assert rpc.submitted == []
    assert record.sidecar_state == STATE_COMMITTED
    assert record.reveal_tx_hash is None


def test_submit_reveal_window_missed_keeps_committed(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)
        rpc = FakeRpc(close_time=AFTER_REVEAL)
        result = submit_reveal(
            _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == REVEAL_STATUS_WINDOW_MISSED
    assert rpc.submitted == []
    assert record.sidecar_state == STATE_COMMITTED
    assert record.reveal_tx_hash is None
    assert record.reveal_error_category == "REVEAL_WINDOW_MISSED"


def test_submit_reveal_refuses_on_local_corruption(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)

    # Corrupt a stored fingerprint so it no longer opens the committed commitment.
    connection = sqlite3.connect(tmp_path / STATE_DB_FILENAME)
    try:
        connection.execute(
            "UPDATE sidecar_rounds SET model_response_hash = ? WHERE round_id = ?",
            ("9" * 64, ROUND_ID),
        )
        connection.commit()
    finally:
        connection.close()

    with SidecarState(tmp_path) as state:
        rpc = FakeRpc(close_time=IN_REVEAL)
        with pytest.raises(RevealError):
            submit_reveal(
                _config(tmp_path), _metadata(),
                rpc_client=rpc, signer=signer, state=state,
                foundation_publisher_address=PUBLISHER,
            )
        assert rpc.submitted == []


def test_submit_reveal_not_committed(tmp_path):
    rpc = FakeRpc(close_time=IN_REVEAL)
    with SidecarState(tmp_path) as state:
        result = submit_reveal(
            _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=FakeSigner(), state=state,
            foundation_publisher_address=PUBLISHER,
        )
    assert result.status == REVEAL_STATUS_NOT_COMMITTED
    assert rpc.submitted == []


def test_submit_reveal_requires_wallet_seed(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)
        rpc = FakeRpc(close_time=IN_REVEAL)
        with pytest.raises(RevealError):
            submit_reveal(
                _config(tmp_path, with_seed=False), _metadata(),
                rpc_client=rpc, signer=signer, state=state,
                foundation_publisher_address=PUBLISHER,
            )
    assert rpc.submitted == []


def test_submit_reveal_low_balance_keeps_committed(tmp_path):
    signer = FakeSigner()
    with SidecarState(tmp_path) as state:
        _commit_round(state, tmp_path, signer)
        rpc = FakeRpc(
            close_time=IN_REVEAL,
            submit_error=PftlInsufficientFundsError("tecUNFUNDED_PAYMENT"),
        )
        result = submit_reveal(
            _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == REVEAL_STATUS_SKIPPED_LOW_BALANCE
    assert record.sidecar_state == STATE_COMMITTED
    assert record.reveal_tx_hash is None
