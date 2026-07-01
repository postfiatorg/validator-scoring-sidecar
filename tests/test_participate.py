from datetime import datetime, timezone
from pathlib import Path

import pytest
from xrpl.core import keypairs
from xrpl.core.addresscodec import encode_node_public_key

from validator_scoring_sidecar.chain import AnnouncementError, VerifiedAnnouncement
from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.deployment import (
    ModalDeploymentResult,
    NoEligibleRoundError,
    deployment_record_path,
)
from validator_scoring_sidecar.input_package import FetchedInputPackage
from validator_scoring_sidecar.participate import (
    ParticipationConfigError,
    modal_runtime_provisioner,
    participate,
)
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.score import ScoreResult
from validator_scoring_sidecar.scoring import commit_reveal
from validator_scoring_sidecar.state import (
    STATE_COMMITTED,
    STATE_REVEALED,
    STATE_SCORED,
    STATE_SCORING_FAILED,
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
REVEAL_TIME = datetime(2026, 5, 25, 0, 45, tzinfo=timezone.utc)
AFTER_REVEAL = datetime(2026, 5, 25, 1, 5, tzinfo=timezone.utc)

ANNOUNCEMENT_TX = {
    "validated": True,
    "tx_json": {"Account": PUBLISHER, "Memos": []},
    "hash": "ANNTX",
    "ledger_index": 100,
}


class FakeSigner:
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
    def __init__(self, *, close_time, transactions=None):
        self.close_time = close_time
        self.transactions = transactions or []
        self.submitted = []
        self._counter = 0

    def latest_validated_ledger_close_time(self):
        return self.close_time

    def account_tx(self, *, account, ledger_index_min, ledger_index_max, forward, limit, marker):
        return {"transactions": self.transactions}

    def submit_memo(self, *, wallet_seed, destination, memo_type, memo_data):
        self._counter += 1
        tx_hash = f"TX{self._counter}"
        self.submitted.append({"memo_type": memo_type, "destination": destination, "hash": tx_hash})
        return tx_hash


class FakeClient:
    def fetch_config(self):
        return {
            "foundation_publisher_address": PUBLISHER,
            "announcement_memo_type": commit_reveal.ROUND_ANNOUNCEMENT_TYPE,
            "announcement_commit_window_seconds": 1800,
            "announcement_reveal_window_seconds": 1800,
            "announcement_reveal_gap_seconds": 0,
        }


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


def _package():
    return FetchedInputPackage(
        round_id=ROUND_ID,
        round_number=ROUND_NUMBER,
        network=NETWORK,
        input_package_cid=CID,
        input_package_hash=INPUT_HASH,
        input_frozen_at="2026-05-25T00:00:00+00:00",
        source="https",
        cached=False,
        local_path=Path("/unused"),
        verified_file_count=3,
    )


def fake_decoder(transaction, config, client, *, package_fetcher=None, round_limit=None):
    if transaction.tx_hash == "ANNTX":
        return VerifiedAnnouncement(announcement=_announcement(), package=_package())
    return None


def fake_score_runner(
    config, client, *, source=None, round_limit=None, runtime_provisioner=None
):
    return ScoreResult(
        status="already_scored",
        network=NETWORK,
        round_id=ROUND_ID,
        round_number=ROUND_NUMBER,
        sidecar_state=STATE_SCORED,
        backend_mode="modal",
        compared=True,
        matched_levels=[],
        error_category=None,
    )


def _no_score(*args, **kwargs):
    raise AssertionError("must not score before the config gate passes")


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


def _config(tmp_path, *, with_seed=True, with_keys=True):
    environ = {}
    if with_seed:
        environ["POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED"] = "sEdTESTseed"
    if with_keys:
        environ["POSTFIAT_SIDECAR_VALIDATOR_KEYS_PATH"] = "/keys/validator-keys.json"
    return load_config(network=NETWORK, data_dir=tmp_path, environ=environ)


def _seed_scored(tmp_path):
    with SidecarState(tmp_path) as state:
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


def _participate(tmp_path, signer, rpc, *, score_runner=fake_score_runner):
    return participate(
        _config(tmp_path),
        FakeClient(),
        rpc_client=rpc,
        signer=signer,
        score_runner=score_runner,
        announcement_decoder=fake_decoder,
    )


def test_participate_commits_in_commit_window(tmp_path):
    _seed_scored(tmp_path)
    rpc = FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX])
    result = _participate(tmp_path, FakeSigner(), rpc)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)

    assert record.sidecar_state == STATE_COMMITTED
    assert record.commit_tx_hash is not None
    assert any(entry["status"] == "committed" for entry in result.commits)
    assert len(rpc.submitted) == 1
    assert rpc.submitted[0]["memo_type"] == commit_reveal.VALIDATOR_COMMIT_TYPE
    # The reveal window has not opened, so no reveal is broadcast.
    assert all(entry["status"] != "revealed" for entry in result.reveals)


def test_participate_reveals_in_reveal_window(tmp_path):
    _seed_scored(tmp_path)
    signer = FakeSigner()
    _participate(tmp_path, signer, FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX]))

    rpc = FakeRpc(close_time=REVEAL_TIME, transactions=[ANNOUNCEMENT_TX])
    result = _participate(tmp_path, signer, rpc)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)

    assert record.sidecar_state == STATE_REVEALED
    assert record.reveal_tx_hash is not None
    assert any(entry["status"] == "revealed" for entry in result.reveals)
    assert any(s["memo_type"] == commit_reveal.VALIDATOR_REVEAL_TYPE for s in rpc.submitted)


def test_participate_records_reveal_miss_after_window(tmp_path):
    _seed_scored(tmp_path)
    signer = FakeSigner()
    _participate(tmp_path, signer, FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX]))

    rpc = FakeRpc(close_time=AFTER_REVEAL, transactions=[])
    result = _participate(tmp_path, signer, rpc)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)

    assert record.sidecar_state == STATE_COMMITTED
    assert record.reveal_error_category == "REVEAL_WINDOW_MISSED"
    assert any(entry["status"] == "reveal_window_missed" for entry in result.reveals)
    assert rpc.submitted == []


def test_participate_restart_does_not_double_commit(tmp_path):
    _seed_scored(tmp_path)
    signer = FakeSigner()
    _participate(tmp_path, signer, FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX]))
    with SidecarState(tmp_path) as state:
        first_commit = state.get_round(NETWORK, ROUND_ID).commit_tx_hash

    # A clean restart: the chain cursor advanced past the announcement, so it is
    # not reprocessed and nothing is re-submitted.
    rpc = FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX])
    _participate(tmp_path, signer, rpc)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)
    assert rpc.submitted == []
    assert record.sidecar_state == STATE_COMMITTED
    assert record.commit_tx_hash == first_commit


def test_participate_reprocessed_announcement_is_idempotent(tmp_path):
    _seed_scored(tmp_path)
    signer = FakeSigner()
    _participate(tmp_path, signer, FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX]))
    with SidecarState(tmp_path) as state:
        first_commit = state.get_round(NETWORK, ROUND_ID).commit_tx_hash

    # Rewind the cursor so the announcement re-surfaces. The round is already
    # committed, so it is no longer pending commit and is not re-submitted.
    with SidecarState(tmp_path) as state:
        state.set_chain_cursor(NETWORK, PUBLISHER, 99, "OLDER")

    rpc = FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX])
    _participate(tmp_path, signer, rpc)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)
    assert rpc.submitted == []
    assert record.sidecar_state == STATE_COMMITTED
    assert record.commit_tx_hash == first_commit


def test_participate_records_windows_for_unscored_round(tmp_path):
    # The announced round is not yet scored, so nothing commits — but its windows
    # are persisted so a later pass can commit once it is scored.
    rpc = FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX])
    result = _participate(tmp_path, FakeSigner(), rpc)

    assert rpc.submitted == []
    assert all(entry["status"] != "committed" for entry in result.commits)
    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)
    assert record is not None
    assert record.sidecar_state != STATE_COMMITTED
    assert record.commit_opens_at is not None
    assert record.commit_closes_at is not None


def test_participate_reveals_when_no_eligible_round(tmp_path):
    _seed_scored(tmp_path)
    signer = FakeSigner()
    _participate(tmp_path, signer, FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX]))

    def _raises(config, client, **kwargs):
        raise NoEligibleRoundError("no eligible round")

    rpc = FakeRpc(close_time=REVEAL_TIME, transactions=[])
    result = participate(
        _config(tmp_path),
        FakeClient(),
        rpc_client=rpc,
        signer=signer,
        score_runner=_raises,
        announcement_decoder=fake_decoder,
    )

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)

    assert result.score_status == "no_eligible_round"
    assert record.sidecar_state == STATE_REVEALED
    assert any(s["memo_type"] == commit_reveal.VALIDATOR_REVEAL_TYPE for s in rpc.submitted)


def test_participate_commits_when_window_opens_on_later_pass(tmp_path):
    _seed_scored(tmp_path)
    signer = FakeSigner()
    before_commit = datetime(2026, 5, 24, 23, 0, tzinfo=timezone.utc)

    rpc1 = FakeRpc(close_time=before_commit, transactions=[ANNOUNCEMENT_TX])
    first = _participate(tmp_path, signer, rpc1)

    assert rpc1.submitted == []
    assert any(entry["status"] == "commit_window_not_open" for entry in first.commits)
    with SidecarState(tmp_path) as state:
        assert state.get_round(NETWORK, ROUND_ID).sidecar_state == STATE_SCORED

    # The announcement's windows are recorded and the cursor advances past it, so
    # it is no longer in the feed; the commit is replayed from local state once
    # the window opens.
    rpc2 = FakeRpc(close_time=COMMIT_TIME, transactions=[])
    _participate(tmp_path, signer, rpc2)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)
    assert record.sidecar_state == STATE_COMMITTED
    assert len(rpc2.submitted) == 1


def _failing_score_runner(
    config, client, *, source=None, round_limit=None, runtime_provisioner=None
):
    return ScoreResult(
        status="scoring_failed",
        network=NETWORK,
        round_id=ROUND_ID,
        round_number=ROUND_NUMBER,
        sidecar_state=STATE_SCORING_FAILED,
        backend_mode="modal",
        compared=False,
        matched_levels=[],
        error_category="INFERENCE_ERROR",
    )


def test_participate_commits_after_transient_score_failure(tmp_path):
    # The exact production bug: scoring fails transiently on the pass that first
    # sees the announcement, then succeeds on a later pass. The round must still
    # commit, even though the announcement has scrolled past the chain cursor.
    signer = FakeSigner()

    # Pass 1: scoring fails. The announcement's windows are recorded and the
    # cursor advances past it; nothing commits because the round is not scored.
    rpc1 = FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX])
    _participate(tmp_path, signer, rpc1, score_runner=_failing_score_runner)
    assert rpc1.submitted == []
    with SidecarState(tmp_path) as state:
        recorded = state.get_round(NETWORK, ROUND_ID)
    assert recorded is not None
    assert recorded.commit_opens_at is not None  # windows persisted despite failure
    assert recorded.sidecar_state != STATE_COMMITTED

    # Scoring succeeds on a later pass.
    _seed_scored(tmp_path)

    # Pass 2: the announcement is no longer in the watcher feed, yet the commit
    # still happens, driven from local state.
    rpc2 = FakeRpc(close_time=COMMIT_TIME, transactions=[])
    result = _participate(tmp_path, signer, rpc2)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)
    assert record.sidecar_state == STATE_COMMITTED
    assert record.commit_tx_hash is not None
    assert len(rpc2.submitted) == 1
    assert any(entry["status"] == "committed" for entry in result.commits)


def test_participate_does_not_commit_after_window_closes(tmp_path):
    _seed_scored(tmp_path)
    signer = FakeSigner()
    # By the time the round is processed, the commit window has already closed.
    rpc = FakeRpc(close_time=AFTER_REVEAL, transactions=[ANNOUNCEMENT_TX])
    result = _participate(tmp_path, signer, rpc)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)
    assert record.sidecar_state == STATE_SCORED
    assert record.commit_tx_hash is None
    assert rpc.submitted == []
    assert any(entry["status"] == "commit_window_closed" for entry in result.commits)


def test_participate_commit_survives_restart(tmp_path):
    # State persisted before a restart: the round is scored and its windows are
    # recorded, but it has not committed yet. A fresh pass commits it from state,
    # with no announcement in the feed.
    _seed_scored(tmp_path)
    with SidecarState(tmp_path) as state:
        state.record_announcement_windows(
            NETWORK,
            _metadata(),
            commit_opens_at="2026-05-25T00:00:00+00:00",
            commit_closes_at="2026-05-25T00:30:00+00:00",
            reveal_opens_at="2026-05-25T00:30:00+00:00",
            reveal_closes_at="2026-05-25T01:00:00+00:00",
        )

    rpc = FakeRpc(close_time=COMMIT_TIME, transactions=[])
    result = _participate(tmp_path, FakeSigner(), rpc)

    with SidecarState(tmp_path) as state:
        record = state.get_round(NETWORK, ROUND_ID)
    assert record.sidecar_state == STATE_COMMITTED
    assert len(rpc.submitted) == 1
    assert any(entry["status"] == "committed" for entry in result.commits)


def test_participate_surfaces_announcement_error(tmp_path):
    # A malformed announcement from the trusted sender is recorded and skipped
    # (the cursor still advances), and the error is surfaced in the result rather
    # than silently dropped.
    def raising_decoder(
        transaction, config, client, *, package_fetcher=None, round_limit=None
    ):
        raise AnnouncementError("malformed announcement")

    rpc = FakeRpc(close_time=COMMIT_TIME, transactions=[ANNOUNCEMENT_TX])
    result = participate(
        _config(tmp_path),
        FakeClient(),
        rpc_client=rpc,
        signer=FakeSigner(),
        score_runner=fake_score_runner,
        announcement_decoder=raising_decoder,
    )

    assert rpc.submitted == []
    assert any(
        entry["status"] == "announcement_error" for entry in result.announcements
    )


def test_participate_requires_wallet_seed(tmp_path):
    with pytest.raises(ParticipationConfigError):
        participate(
            _config(tmp_path, with_seed=False),
            FakeClient(),
            rpc_client=FakeRpc(close_time=COMMIT_TIME),
            signer=FakeSigner(),
            score_runner=_no_score,
            announcement_decoder=fake_decoder,
        )


def test_participate_requires_validator_keys(tmp_path):
    with pytest.raises(ParticipationConfigError):
        participate(
            _config(tmp_path, with_keys=False),
            FakeClient(),
            rpc_client=FakeRpc(close_time=COMMIT_TIME),
            signer=FakeSigner(),
            score_runner=_no_score,
            announcement_decoder=fake_decoder,
        )


MODAL_CREDS = {"MODAL_TOKEN_ID": "id", "MODAL_TOKEN_SECRET": "secret"}


def _runtime_manifest():
    return {
        "runtime": {
            "kind": "modal_sglang",
            "image": "lmsysorg/sglang:nightly-dev@sha256:" + "d" * 64,
            "gpu": "H100",
            "tensor_parallelism": 1,
            "launch_args": ["--enable-deterministic-inference"],
        },
        "model": {
            "provider": "huggingface",
            "repo_id": "Qwen/Qwen3.6-27B-FP8",
            "served_name": "Qwen/Qwen3.6-27B-FP8",
            "revision": "a" * 40,
        },
    }


def test_modal_runtime_provisioner_requires_both_credentials(tmp_path):
    config = _config(tmp_path)
    assert modal_runtime_provisioner(config, environ={}) is None
    assert (
        modal_runtime_provisioner(config, environ={"MODAL_TOKEN_ID": "id"}) is None
    )
    assert (
        modal_runtime_provisioner(config, environ={"MODAL_TOKEN_SECRET": "secret"})
        is None
    )


def test_modal_runtime_provisioner_deploys_and_records(tmp_path):
    config = _config(tmp_path)
    deployed = {}

    class FakeDeployer:
        def deploy(self, spec, *, app_name):
            deployed["app_name"] = app_name
            deployed["image"] = spec.image
            return ModalDeploymentResult(endpoint_url="https://operator--app.modal.run")

    provision = modal_runtime_provisioner(
        config, environ=MODAL_CREDS, deployer_factory=FakeDeployer
    )
    record = provision(_runtime_manifest())

    assert deployed["app_name"] == f"validator-scoring-sidecar-{NETWORK}"
    assert record["mode"] == "modal"
    assert record["endpoint_url"] == "https://operator--app.modal.run"
    assert deployment_record_path(config).is_file()


def test_modal_runtime_provisioner_uses_configured_app_name(tmp_path):
    config = load_config(
        network=NETWORK,
        data_dir=tmp_path,
        environ={"POSTFIAT_SIDECAR_MODAL_APP_NAME": "sidecar-devnet-nurgle"},
    )
    deployed = {}

    class FakeDeployer:
        def deploy(self, spec, *, app_name):
            deployed["app_name"] = app_name
            return ModalDeploymentResult(endpoint_url="https://operator--app.modal.run")

    provision = modal_runtime_provisioner(
        config, environ=MODAL_CREDS, deployer_factory=FakeDeployer
    )
    provision(_runtime_manifest())

    assert deployed["app_name"] == "sidecar-devnet-nurgle"


def test_participate_forwards_runtime_provisioner(tmp_path):
    captured = {}

    def runner(config, client, *, source=None, round_limit=None, runtime_provisioner=None):
        captured["provisioner"] = runtime_provisioner
        return fake_score_runner(config, client)

    def sentinel(manifest):
        raise AssertionError("never invoked in this test")

    participate(
        _config(tmp_path),
        FakeClient(),
        rpc_client=FakeRpc(close_time=COMMIT_TIME),
        signer=FakeSigner(),
        score_runner=runner,
        announcement_decoder=fake_decoder,
        runtime_provisioner=sentinel,
    )

    assert captured["provisioner"] is sentinel


def test_participate_without_modal_credentials_passes_no_provisioner(tmp_path, monkeypatch):
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    captured = {}

    def runner(config, client, *, source=None, round_limit=None, runtime_provisioner=None):
        captured["provisioner"] = runtime_provisioner
        return fake_score_runner(config, client)

    participate(
        _config(tmp_path),
        FakeClient(),
        rpc_client=FakeRpc(close_time=COMMIT_TIME),
        signer=FakeSigner(),
        score_runner=runner,
        announcement_decoder=fake_decoder,
    )

    assert captured["provisioner"] is None
