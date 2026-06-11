import json
import subprocess
from datetime import datetime, timezone

import pytest
from xrpl.core import keypairs
from xrpl.core.addresscodec import encode_node_public_key

from validator_scoring_sidecar.chain import PftlInsufficientFundsError
from validator_scoring_sidecar.commit import (
    COMMIT_STATUS_ALREADY_COMMITTED,
    COMMIT_STATUS_SKIPPED_LOW_BALANCE,
    COMMIT_STATUS_SUBMITTED,
    COMMIT_STATUS_WINDOW_CLOSED,
    COMMIT_STATUS_WINDOW_NOT_OPEN,
    CommitError,
    ValidatorKeysSigner,
    submit_commit,
)
from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.state import (
    STATE_SCORED,
    STATE_SKIPPED,
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
    def __init__(self, *, close_time, transactions=None, submit_error=None):
        self.close_time = close_time
        self.transactions = transactions or []
        self.submit_error = submit_error
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
        return "COMMITTX123"


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


def _commit_memo(announcement, signer, *, salt="d" * 64):
    output_hashes = commit_reveal.OutputHashes(
        model_response_hash=OUTPUT_HASHES[HASH_MODEL_RESPONSE],
        validator_scores_hash=OUTPUT_HASHES[HASH_VALIDATOR_SCORES],
        selected_unl_hash=OUTPUT_HASHES[HASH_SELECTED_UNL],
    )
    commitment_hash = commit_reveal.compute_commitment_hash(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=signer.master_key,
        input_package_hash=announcement.input_package_hash,
        output_hashes=output_hashes,
        salt=salt,
    )
    signing_bytes = commit_reveal.build_commit_signing_bytes(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=signer.master_key,
        input_package_hash=announcement.input_package_hash,
        commitment_hash=commitment_hash,
    )
    payload = commit_reveal.build_commit_payload(
        protocol_version=announcement.protocol_version,
        network=announcement.network,
        round_number=announcement.round_number,
        validator_master_key=signer.master_key,
        input_package_hash=announcement.input_package_hash,
        commitment_hash=commitment_hash,
        signature=signer.sign(signing_bytes),
    )
    data = commit_reveal.canonical_json_bytes(payload).decode("utf-8")
    return {
        "Memo": {
            "MemoType": commit_reveal.VALIDATOR_COMMIT_TYPE.encode("utf-8").hex(),
            "MemoData": data.encode("utf-8").hex(),
        }
    }


def _in_window():
    return datetime(2026, 5, 25, 0, 15, tzinfo=timezone.utc)


def test_submit_commit_success(tmp_path):
    rpc = FakeRpc(close_time=_in_window())
    with SidecarState(tmp_path) as state:
        result = submit_commit(
            _announcement(),
            OUTPUT_HASHES,
            _config(tmp_path),
            _metadata(),
            rpc_client=rpc,
            signer=FakeSigner(),
            state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == COMMIT_STATUS_SUBMITTED
    assert result.commit_tx_hash == "COMMITTX123"
    assert len(rpc.submitted) == 1
    assert rpc.submitted[0]["memo_type"] == commit_reveal.VALIDATOR_COMMIT_TYPE
    assert rpc.submitted[0]["destination"] == PUBLISHER
    assert record.commit_tx_hash == "COMMITTX123"
    assert record.salt is not None and len(record.salt) == 64
    assert record.reveal_opens_at == "2026-05-25T00:30:00+00:00"
    # The submitted memo is a protocol-valid commit signed by the master key.
    payload = commit_reveal.validate_commit_payload(
        __import__("json").loads(rpc.submitted[0]["memo_data"])
    )
    assert commit_reveal.verify_commit_signature(payload)


def test_submit_commit_window_not_open(tmp_path):
    rpc = FakeRpc(close_time=datetime(2026, 5, 24, 23, 0, tzinfo=timezone.utc))
    with SidecarState(tmp_path) as state:
        result = submit_commit(
            _announcement(), OUTPUT_HASHES, _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=FakeSigner(), state=state,
            foundation_publisher_address=PUBLISHER,
        )
    assert result.status == COMMIT_STATUS_WINDOW_NOT_OPEN
    assert rpc.submitted == []


def test_submit_commit_window_closed(tmp_path):
    rpc = FakeRpc(close_time=datetime(2026, 5, 25, 0, 30, tzinfo=timezone.utc))
    with SidecarState(tmp_path) as state:
        result = submit_commit(
            _announcement(), OUTPUT_HASHES, _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=FakeSigner(), state=state,
            foundation_publisher_address=PUBLISHER,
        )
    assert result.status == COMMIT_STATUS_WINDOW_CLOSED
    assert rpc.submitted == []


def test_submit_commit_low_balance_marks_opt_out_and_preserves_scores(tmp_path):
    rpc = FakeRpc(
        close_time=_in_window(),
        submit_error=PftlInsufficientFundsError("tecUNFUNDED_PAYMENT"),
    )
    metadata = _metadata()
    with SidecarState(tmp_path) as state:
        state.record_score(
            NETWORK,
            metadata,
            ScoreOutcome(
                sidecar_state=STATE_SCORED,
                backend_mode="modal",
                model_response_hash=OUTPUT_HASHES[HASH_MODEL_RESPONSE],
                validator_scores_hash=OUTPUT_HASHES[HASH_VALIDATOR_SCORES],
                selected_unl_hash=OUTPUT_HASHES[HASH_SELECTED_UNL],
            ),
        )
        result = submit_commit(
            _announcement(), OUTPUT_HASHES, _config(tmp_path), metadata,
            rpc_client=rpc, signer=FakeSigner(), state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == COMMIT_STATUS_SKIPPED_LOW_BALANCE
    assert record.sidecar_state == STATE_SKIPPED
    assert record.error_category == "SKIPPED_OPERATOR_OPT_OUT"
    # The commit-skip must not wipe the scored fingerprints.
    assert record.model_response_hash == OUTPUT_HASHES[HASH_MODEL_RESPONSE]
    assert record.selected_unl_hash == OUTPUT_HASHES[HASH_SELECTED_UNL]
    assert record.backend_mode == "modal"


def test_submit_commit_is_locally_idempotent(tmp_path):
    rpc = FakeRpc(close_time=_in_window())
    signer = FakeSigner()
    announcement = _announcement()
    with SidecarState(tmp_path) as state:
        first = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        second = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )

    assert first.status == COMMIT_STATUS_SUBMITTED
    assert second.status == COMMIT_STATUS_ALREADY_COMMITTED
    assert len(rpc.submitted) == 1  # no second submission


def test_submit_commit_detects_existing_onchain_commit(tmp_path):
    signer = FakeSigner()
    announcement = _announcement()
    existing = {
        "tx_json": {"Account": "rRelay", "Memos": [_commit_memo(announcement, signer)]},
        "hash": "ONCHAINTX",
    }
    rpc = FakeRpc(close_time=_in_window(), transactions=[existing])
    with SidecarState(tmp_path) as state:
        result = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), _metadata(),
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )

    assert result.status == COMMIT_STATUS_ALREADY_COMMITTED
    assert result.commit_tx_hash == "ONCHAINTX"
    assert rpc.submitted == []


def test_submit_commit_requires_all_three_hashes(tmp_path):
    rpc = FakeRpc(close_time=_in_window())
    incomplete = {HASH_MODEL_RESPONSE: "1" * 64, HASH_VALIDATOR_SCORES: "2" * 64}
    with SidecarState(tmp_path) as state:
        with pytest.raises(CommitError):
            submit_commit(
                _announcement(), incomplete, _config(tmp_path), _metadata(),
                rpc_client=rpc, signer=FakeSigner(), state=state,
                foundation_publisher_address=PUBLISHER,
            )


def test_submit_commit_requires_wallet_seed(tmp_path):
    rpc = FakeRpc(close_time=_in_window())
    with SidecarState(tmp_path) as state:
        with pytest.raises(CommitError):
            submit_commit(
                _announcement(), OUTPUT_HASHES, _config(tmp_path, with_seed=False),
                _metadata(), rpc_client=rpc, signer=FakeSigner(), state=state,
                foundation_publisher_address=PUBLISHER,
            )


def _write_keys_file(tmp_path, public_key="nHTestMasterKey"):
    keys_path = tmp_path / "validator-keys.json"
    keys_path.write_text(json.dumps({"public_key": public_key}), encoding="utf-8")
    return keys_path


def test_validator_keys_signer_signs_with_configured_keyfile(tmp_path, monkeypatch):
    keys_path = _write_keys_file(tmp_path)
    invocations = []

    def fake_run(argv, **kwargs):
        invocations.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="SIG456\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    signer = ValidatorKeysSigner(validator_keys_path=str(keys_path))

    assert signer.sign(b"payload-bytes") == "SIG456"
    # The tool must be bound to the configured key file: without --keyfile it
    # would sign with its default keystore, decoupling identity from signature.
    assert invocations == [
        ["validator-keys", "--keyfile", str(keys_path), "sign", "payload-bytes"],
    ]


def test_validator_keys_signer_reads_master_key_from_same_keyfile(tmp_path):
    keys_path = _write_keys_file(tmp_path, public_key="  nHTestMasterKey  ")
    signer = ValidatorKeysSigner(validator_keys_path=str(keys_path))
    assert signer.master_key == "nHTestMasterKey"


def test_validator_keys_signer_rejects_keyfile_without_public_key(tmp_path):
    keys_path = tmp_path / "validator-keys.json"
    keys_path.write_text(json.dumps({"key_type": "ed25519"}), encoding="utf-8")
    signer = ValidatorKeysSigner(validator_keys_path=str(keys_path))
    with pytest.raises(CommitError):
        signer.master_key


def test_validator_keys_signer_wraps_tool_failure(tmp_path, monkeypatch):
    keys_path = _write_keys_file(tmp_path)

    def fake_run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="no keys")

    monkeypatch.setattr(subprocess, "run", fake_run)
    signer = ValidatorKeysSigner(validator_keys_path=str(keys_path))
    with pytest.raises(CommitError):
        signer.sign(b"payload-bytes")
