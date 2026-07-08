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
    STATE_COMMITTED,
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


def _seed_scored(state, metadata):
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


class KillAfterBroadcastRpc(FakeRpc):
    """The commit reaches the ledger, then the process is killed before its tx
    hash is persisted — the exact crash window the pass watchdog opened live.
    ``submit_memo`` records the landed transaction (so a later account_tx scan
    finds it) and then raises to model the abrupt termination."""

    def submit_memo(self, *, wallet_seed, destination, memo_type, memo_data):
        self.transactions.append(
            {
                "tx_json": {
                    "Account": "rRelay",
                    "Memos": [
                        {
                            "Memo": {
                                "MemoType": memo_type.encode("utf-8").hex(),
                                "MemoData": memo_data.encode("utf-8").hex(),
                            }
                        }
                    ],
                },
                "hash": "LANDEDTX",
            }
        )
        raise RuntimeError("process killed by watchdog before persist")


def test_commit_persists_salt_before_broadcast_surviving_crash(tmp_path):
    # A kill between broadcast and persist must not lose the reveal secret.
    rpc = KillAfterBroadcastRpc(close_time=_in_window())
    metadata = _metadata()
    with SidecarState(tmp_path) as state:
        _seed_scored(state, metadata)
        with pytest.raises(RuntimeError):
            submit_commit(
                _announcement(), OUTPUT_HASHES, _config(tmp_path), metadata,
                rpc_client=rpc, signer=FakeSigner(), state=state,
                foundation_publisher_address=PUBLISHER,
            )
        record = state.get_round(NETWORK, ROUND_ID)

    # Salt + commitment are durable even though the tx hash never persisted.
    assert record.salt is not None and len(record.salt) == 64
    assert record.commitment_hash is not None
    assert record.commit_tx_hash is None
    assert record.sidecar_state == STATE_SCORED  # still pending commit → retried
    assert len(rpc.transactions) == 1  # the commit did reach the ledger


def test_commit_recovers_landed_commit_on_later_pass(tmp_path):
    signer = FakeSigner()
    announcement = _announcement()
    metadata = _metadata()

    kill_rpc = KillAfterBroadcastRpc(close_time=_in_window())
    with SidecarState(tmp_path) as state:
        _seed_scored(state, metadata)
        with pytest.raises(RuntimeError):
            submit_commit(
                announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
                rpc_client=kill_rpc, signer=signer, state=state,
                foundation_publisher_address=PUBLISHER,
            )
        salt_before = state.get_round(NETWORK, ROUND_ID).salt

    # Next pass: the landed commit is visible on chain; recovery attaches its
    # hash and advances to COMMITTED with the salt preserved, no re-broadcast.
    recover_rpc = FakeRpc(close_time=_in_window(), transactions=kill_rpc.transactions)
    with SidecarState(tmp_path) as state:
        result = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
            rpc_client=recover_rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == COMMIT_STATUS_ALREADY_COMMITTED
    assert result.commit_tx_hash == "LANDEDTX"
    assert recover_rpc.submitted == []  # no duplicate broadcast
    assert record.sidecar_state == STATE_COMMITTED
    assert record.commit_tx_hash == "LANDEDTX"
    assert record.salt == salt_before  # preserved → the reveal can be built


def test_commit_retry_reuses_persisted_salt_when_broadcast_did_not_land(tmp_path):
    signer = FakeSigner()
    announcement = _announcement()
    metadata = _metadata()

    # Pass 1: pending salt persisted, but the broadcast fails before landing.
    fail_rpc = FakeRpc(
        close_time=_in_window(), submit_error=RuntimeError("network drop")
    )
    with SidecarState(tmp_path) as state:
        _seed_scored(state, metadata)
        with pytest.raises(RuntimeError):
            submit_commit(
                announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
                rpc_client=fail_rpc, signer=signer, state=state,
                foundation_publisher_address=PUBLISHER,
            )
        row = state.get_round(NETWORK, ROUND_ID)
        salt_before, commitment_before = row.salt, row.commitment_hash
    assert fail_rpc.submitted == []  # never landed

    # Pass 2: nothing on chain; retry reuses the salt so the rebuilt commitment
    # is byte-identical to what any prior attempt would have produced.
    ok_rpc = FakeRpc(close_time=_in_window())
    with SidecarState(tmp_path) as state:
        result = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
            rpc_client=ok_rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == COMMIT_STATUS_SUBMITTED
    assert record.salt == salt_before
    assert record.commitment_hash == commitment_before
    assert record.sidecar_state == STATE_COMMITTED
    submitted = commit_reveal.validate_commit_payload(
        json.loads(ok_rpc.submitted[0]["memo_data"])
    )
    assert submitted.commitment_hash == commitment_before
    assert commit_reveal.verify_commit_signature(submitted)


def _after_window():
    return datetime(2026, 5, 25, 0, 45, tzinfo=timezone.utc)


def test_commit_recovers_landed_commit_after_window_closed(tmp_path):
    # The incident shape: the pass that broadcast was killed late in the window,
    # and the recovery pass only runs after the commit window has closed. The
    # landed commit must still be recovered — the window-closed gate must not
    # pre-empt the recovery scan.
    signer = FakeSigner()
    announcement = _announcement()
    metadata = _metadata()

    kill_rpc = KillAfterBroadcastRpc(close_time=_in_window())
    with SidecarState(tmp_path) as state:
        _seed_scored(state, metadata)
        with pytest.raises(RuntimeError):
            submit_commit(
                announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
                rpc_client=kill_rpc, signer=signer, state=state,
                foundation_publisher_address=PUBLISHER,
            )

    recover_rpc = FakeRpc(close_time=_after_window(), transactions=kill_rpc.transactions)
    with SidecarState(tmp_path) as state:
        result = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
            rpc_client=recover_rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == COMMIT_STATUS_ALREADY_COMMITTED
    assert result.commit_tx_hash == "LANDEDTX"
    assert record.sidecar_state == STATE_COMMITTED
    assert record.commit_tx_hash == "LANDEDTX"


def test_commit_recovery_skips_round_without_local_salt(tmp_path):
    # A pre-fix orphan (or a cross-host commit): the commit is on chain but no
    # salt was ever persisted locally. Advancing to COMMITTED would create a
    # round that can never reveal, so recovery must leave it untouched.
    signer = FakeSigner()
    announcement = _announcement()
    metadata = _metadata()
    onchain = {
        "tx_json": {"Account": "rRelay", "Memos": [_commit_memo(announcement, signer)]},
        "hash": "ORPHANTX",
    }
    rpc = FakeRpc(close_time=_in_window(), transactions=[onchain])
    with SidecarState(tmp_path) as state:
        _seed_scored(state, metadata)  # SCORED, but no salt persisted
        result = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == COMMIT_STATUS_ALREADY_COMMITTED
    assert result.commit_tx_hash == "ORPHANTX"
    # Not advanced: no salt means it could never reveal, so it must not become a
    # COMMITTED round that loops the reveal integrity guard forever.
    assert record.sidecar_state == STATE_SCORED
    assert record.commit_tx_hash is None
    assert rpc.submitted == []


def test_commit_recovery_rejects_commitment_mismatch(tmp_path):
    # Local pending state holds salt B (commitment B), but the commit found on
    # chain was authored with a different salt A (commitment A). Recording it
    # would make the sidecar reveal H(B) against an on-chain H(A) the foundation
    # rejects, so recovery must refuse to advance.
    signer = FakeSigner()
    announcement = _announcement()
    metadata = _metadata()
    onchain_A = {
        "tx_json": {
            "Account": "rRelay",
            "Memos": [_commit_memo(announcement, signer, salt="a" * 64)],
        },
        "hash": "SALTA_TX",
    }
    rpc = FakeRpc(close_time=_in_window(), transactions=[onchain_A])
    with SidecarState(tmp_path) as state:
        _seed_scored(state, metadata)
        # Persist a DIFFERENT local pending salt B.
        state.record_commit_pending(
            NETWORK, metadata,
            validator_master_key=signer.master_key,
            salt="b" * 64,
            commitment_hash="deadbeef" * 8,
            commit_opens_at="2026-05-25T00:00:00+00:00",
            commit_closes_at="2026-05-25T00:30:00+00:00",
            reveal_opens_at="2026-05-25T00:30:00+00:00",
            reveal_closes_at="2026-05-25T01:00:00+00:00",
        )
        result = submit_commit(
            announcement, OUTPUT_HASHES, _config(tmp_path), metadata,
            rpc_client=rpc, signer=signer, state=state,
            foundation_publisher_address=PUBLISHER,
        )
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.status == COMMIT_STATUS_ALREADY_COMMITTED
    # Not advanced: local commitment B != on-chain commitment A.
    assert record.sidecar_state == STATE_SCORED
    assert record.commit_tx_hash is None


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
        _seed_scored(state, metadata)
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
