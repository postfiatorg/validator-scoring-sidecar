import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.deployment import DeploymentError
from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.inference import (
    BACKEND_MODE_MODAL,
    FAILURE_REASON_CONFIGURATION,
    FAILURE_REASON_ENDPOINT_UNREACHABLE,
    FAILURE_REASON_KEY,
    InferenceError,
    InferenceResult,
)
from validator_scoring_sidecar.input_package import FetchedInputPackage
from validator_scoring_sidecar.score import (
    INFERENCE_DEADLINE_SAFETY_MARGIN_SECONDS,
    MIN_INFERENCE_READ_TIMEOUT_SECONDS,
    SCORE_STATUS_ALREADY_SCORED,
    SCORE_STATUS_COMPARISON_PENDING,
    SCORE_STATUS_DIVERGENT,
    SCORE_STATUS_SCORED,
    SCORE_STATUS_SCORING_FAILED,
    SCORE_STATUS_SKIPPED,
    _effective_inference_timeout_seconds,
    score_round,
)
from validator_scoring_sidecar.scoring import (
    SUPPORTED_PARSER_CONTENT_HASHES,
    SUPPORTED_SCORE_FORMULA_CONTENT_HASHES,
    SUPPORTED_SELECTOR_CONTENT_HASHES,
)
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.state import (
    STATE_COMMITTED,
    STATE_SCORED,
    STATE_SCORING_FAILED,
    STATE_SKIPPED,
    CommitOutcome,
    SidecarState,
)
from validator_scoring_sidecar.verification import (
    HASH_MODEL_RESPONSE,
    compute_verification_hashes,
)

MODEL_ID = "Qwen/Qwen3.6-27B-FP8"
IMAGE_REF = "lmsysorg/sglang:nightly-dev@sha256:" + "d" * 64
MODEL_REVISION = "a" * 40
PACKAGE_HASH = "a" * 64
VALIDATOR_MAP = {"v1": {"master_key": "MK1"}}
RAW_RESPONSE = json.dumps(
    {
        "v1": {
            "score": 80,
            "consensus": 81,
            "reliability": 82,
            "software": 83,
            "diversity": 84,
            "identity": 85,
            "reasoning": "solid",
        },
        "network_summary": "healthy",
    }
)
MODEL_REQUEST = {
    "model": MODEL_ID,
    "messages": [{"role": "user", "content": "score"}],
    "temperature": 0,
    "max_tokens": 16384,
    "response_format": {"type": "json_object"},
}
LAUNCH_ARGS = [
    "--model-path",
    MODEL_ID,
    "--served-model-name",
    MODEL_ID,
    "--tp",
    "1",
    "--enable-deterministic-inference",
]
EXPECTED_HASHES = compute_verification_hashes(RAW_RESPONSE, VALIDATOR_MAP)


def _manifest():
    return {
        "schema_version": 1,
        "round": {
            "kind": "normal",
            "network": "testnet",
            "round_number": 456,
            "inference_performed": True,
        },
        "model": {
            "provider": "huggingface",
            "repo_id": MODEL_ID,
            "served_name": MODEL_ID,
            "revision": MODEL_REVISION,
        },
        "runtime": {
            "kind": "modal_sglang",
            "image": IMAGE_REF,
            "gpu": "H100",
            "tensor_parallelism": 1,
            "launch_command": ["python", "-m", "sglang.launch_server"],
            "launch_args": list(LAUNCH_ARGS),
            "environment": {"SGLANG_FLASHINFER_WORKSPACE_SIZE": "2147483648"},
        },
        "request": {
            "type": "openai_chat_completions",
            "method": "chat.completions.create",
            "model": MODEL_ID,
            "temperature": 0,
            "max_tokens": 16384,
            "response_format": {"type": "json_object"},
            "extra_body": {},
        },
        "code": {
            "repository": "postfiatorg/dynamic-unl-scoring",
            "commit": "c" * 40,
            "parser": {"content_sha256": next(iter(SUPPORTED_PARSER_CONTENT_HASHES))},
            "selector": {
                "content_sha256": next(iter(SUPPORTED_SELECTOR_CONTENT_HASHES))
            },
        },
        "canonicalization": {
            "hash_algorithm": "sha256",
            "text_encoding": "utf-8",
            "json_encoding": {"sort_keys": True, "separators": [",", ":"]},
        },
    }


def _deployment_record(**overrides):
    record = {
        "mode": "modal",
        "image": IMAGE_REF,
        "gpu_class": "H100",
        "tensor_parallelism": 1,
        "launch_args": list(LAUNCH_ARGS),
        "environment": {"SGLANG_FLASHINFER_WORKSPACE_SIZE": "2147483648"},
        "served_model_name": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "endpoint_url": "https://operator--app.modal.run",
        "deployed_at": "2026-06-01T00:00:00+00:00",
    }
    record.update(overrides)
    return record


def _round_payload(**overrides):
    payload = {
        "id": 123,
        "round_number": 456,
        "status": "INPUT_FROZEN",
        "input_package_cid": "QmInput",
        "input_package_hash": PACKAGE_HASH,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": None,
    }
    payload.update(overrides)
    return payload


class FakeClient:
    def __init__(self, *, payload=None):
        self.payload = payload or _round_payload()

    def fetch_round(self, round_id):
        return dict(self.payload)

    def fetch_rounds(self, *, limit, offset=0):
        return [dict(self.payload)]

    def close(self):
        pass


class FakeBackend:
    backend_mode = BACKEND_MODE_MODAL

    def __init__(self, *, content=RAW_RESPONSE, error=None):
        self.content = content
        self.error = error
        self.run_count = 0
        self.closed = False

    def run(self, model_request):
        self.run_count += 1
        if self.error is not None:
            raise self.error
        return InferenceResult(
            content=self.content,
            response_payload={"choices": [{"message": {"content": self.content}}]},
        )

    def close(self):
        self.closed = True


class BlockingBackend(FakeBackend):
    def __init__(self, entered: threading.Event, release: threading.Event):
        super().__init__()
        self.entered = entered
        self.release = release

    def run(self, model_request):
        self.run_count += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("blocking backend was not released")
        return InferenceResult(
            content=self.content,
            response_payload={"choices": [{"message": {"content": self.content}}]},
        )


def _make_package_fetcher(manifest, previous_unl=None):
    def fetcher(metadata, config, client, *, source, force):
        local_path = config.data_dir / "packages" / metadata.input_package_hash
        (local_path / "runtime").mkdir(parents=True, exist_ok=True)
        (local_path / "inputs").mkdir(parents=True, exist_ok=True)
        (local_path / "runtime" / "execution_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (local_path / "inputs" / "model_request.json").write_text(
            json.dumps(MODEL_REQUEST), encoding="utf-8"
        )
        (local_path / "inputs" / "validator_map.json").write_text(
            json.dumps(VALIDATOR_MAP), encoding="utf-8"
        )
        if previous_unl is not None:
            (local_path / "inputs" / "previous_unl.json").write_text(
                json.dumps({"previous_unl": previous_unl}), encoding="utf-8"
            )
        return FetchedInputPackage(
            round_id=metadata.round_id,
            round_number=metadata.round_number,
            network=config.network,
            input_package_cid=metadata.input_package_cid,
            input_package_hash=metadata.input_package_hash,
            input_frozen_at=metadata.input_frozen_at,
            source="https",
            cached=False,
            local_path=local_path,
            verified_file_count=3,
        )

    return fetcher


def _setup(tmp_path, deployment_record=None):
    config = load_config(
        base_url="https://scoring.example.org",
        data_dir=tmp_path,
        network="testnet",
        environ={},
    )
    record = deployment_record if deployment_record is not None else _deployment_record()
    record_path = tmp_path / "runtime" / "deployment_record.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return config


def _no_backend(record):
    raise AssertionError("backend must not be built on this path")


def _no_fetch(*args, **kwargs):
    raise AssertionError("package must not be re-fetched on this path")


def test_full_score_all_levels_match(tmp_path):
    config = _setup(tmp_path)
    backend = FakeBackend()

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda client, metadata, cfg: dict(EXPECTED_HASHES),
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_SCORED
    assert result.sidecar_state == STATE_SCORED
    assert result.compared is True
    assert result.matched_levels == ["RAW_MATCH", "PARSED_MATCH"]
    assert backend.closed is True

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)
    assert record.sidecar_state == STATE_SCORED
    assert record.model_response_hash == EXPECTED_HASHES[HASH_MODEL_RESPONSE]
    assert record.comparison_levels_matched == "RAW_MATCH,PARSED_MATCH"


def test_full_score_formula_round_selects_over_final_scores(tmp_path):
    # score 80 clears the cutoff but consensus 0 caps the formula final at 25,
    # below it: the foundation hashes are computed formula-mode, so all three
    # levels match only if score_round threads apply_score_formula from the
    # manifest into verification.
    config = _setup(tmp_path)
    raw = json.dumps(
        {
            "v1": {
                "score": 80,
                "consensus": 0,
                "reliability": 85,
                "software": 100,
                "diversity": 40,
                "identity": 80,
                "reasoning": "offline",
            },
            "network_summary": "healthy",
        }
    )
    selector_params = {"score_cutoff": 40, "max_size": 35, "min_score_gap": 5}
    manifest = _manifest()
    manifest["code"]["selector"]["parameters"] = dict(selector_params)
    manifest["code"]["score_formula"] = {
        "content_sha256": next(iter(SUPPORTED_SCORE_FORMULA_CONTENT_HASHES)),
    }
    foundation = compute_verification_hashes(
        raw,
        VALIDATOR_MAP,
        previous_unl=[],
        selector_parameters=selector_params,
        apply_score_formula=True,
    )

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: FakeBackend(content=raw),
        foundation_hash_fetcher=lambda *args: dict(foundation),
        package_fetcher=_make_package_fetcher(manifest, previous_unl=[]),
    )

    assert result.status == SCORE_STATUS_SCORED
    assert result.matched_levels == [
        "RAW_MATCH",
        "PARSED_MATCH",
        "SELECTED_UNL_MATCH",
    ]


def test_full_score_pending_when_foundation_unavailable(tmp_path):
    config = _setup(tmp_path)

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: FakeBackend(),
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_COMPARISON_PENDING
    assert result.sidecar_state == STATE_SCORED
    assert result.compared is False
    assert (
        tmp_path / "scored" / PACKAGE_HASH / "verification_hashes.json"
    ).is_file()


def test_full_score_divergence(tmp_path):
    config = _setup(tmp_path)
    foundation = dict(EXPECTED_HASHES)
    foundation[HASH_MODEL_RESPONSE] = "0" * 64

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: FakeBackend(),
        foundation_hash_fetcher=lambda *args: foundation,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_DIVERGENT
    assert result.error_category == FailureCategory.OUTPUT_DIVERGENCE.value
    assert result.matched_levels == ["PARSED_MATCH"]


def test_already_scored_divergent_round_returns_persisted_details(tmp_path):
    config = _setup(tmp_path)
    foundation = dict(EXPECTED_HASHES)
    foundation[HASH_MODEL_RESPONSE] = "0" * 64

    score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: FakeBackend(),
        foundation_hash_fetcher=lambda *args: foundation,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=_no_backend,
        foundation_hash_fetcher=lambda *args: foundation,
        package_fetcher=_no_fetch,
    )

    assert result.status == SCORE_STATUS_ALREADY_SCORED
    assert result.error_details == {
        "matched_levels": ["PARSED_MATCH"],
        "diverged_levels": ["RAW_MATCH"],
    }


def test_full_score_inference_failure_records_scoring_failed(tmp_path):
    config = _setup(tmp_path)
    backend = FakeBackend(error=InferenceError(FailureCategory.INFERENCE_TIMEOUT, "slow"))

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_SCORING_FAILED
    assert result.sidecar_state == STATE_SCORING_FAILED
    assert result.error_category == FailureCategory.INFERENCE_TIMEOUT.value
    assert result.error_details == {"message": "slow"}
    assert backend.closed is True


def test_full_score_endpoint_unreachable_preserves_diagnostics(tmp_path):
    config = _setup(tmp_path)
    message = (
        "could not reach inference endpoint at https://operator--app.modal.run"
        "/v1/chat/completions: Server disconnected without sending a response."
    )
    backend = FakeBackend(
        error=InferenceError(
            FailureCategory.RUNTIME_UNAVAILABLE,
            message,
            details={FAILURE_REASON_KEY: FAILURE_REASON_ENDPOINT_UNREACHABLE},
        )
    )

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_SCORING_FAILED
    assert result.error_category == FailureCategory.RUNTIME_UNAVAILABLE.value
    assert result.error_details == {
        "message": message,
        FAILURE_REASON_KEY: FAILURE_REASON_ENDPOINT_UNREACHABLE,
    }
    assert result.as_dict()["error_details"] == result.error_details

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)
    assert record.sidecar_state == STATE_SCORING_FAILED
    persisted = json.loads(record.error_details)
    assert persisted["message"] == message
    assert persisted[FAILURE_REASON_KEY] == FAILURE_REASON_ENDPOINT_UNREACHABLE


def test_full_score_releases_sidecar_lock_during_inference(tmp_path):
    config = _setup(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    backend = BlockingBackend(entered, release)

    env = dict(os.environ)
    src_path = os.path.abspath("src")
    env["PYTHONPATH"] = (
        src_path
        if not env.get("PYTHONPATH")
        else os.pathsep.join([src_path, env["PYTHONPATH"]])
    )
    lock_probe = """
import sys
from pathlib import Path
from validator_scoring_sidecar.sync import SidecarLock

with SidecarLock(Path(sys.argv[1])):
    print("acquired")
"""

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            score_round,
            config,
            FakeClient(),
            round_id=123,
            backend_factory=lambda record: backend,
            foundation_hash_fetcher=lambda client, metadata, cfg: dict(EXPECTED_HASHES),
            package_fetcher=_make_package_fetcher(_manifest()),
        )
        try:
            assert entered.wait(timeout=5)
            probe = subprocess.run(
                [sys.executable, "-c", lock_probe, str(tmp_path)],
                text=True,
                capture_output=True,
                timeout=5,
                env=env,
            )
            assert probe.returncode == 0, probe.stderr
            assert "acquired" in probe.stdout
        finally:
            release.set()

        result = future.result(timeout=5)

    assert result.status == SCORE_STATUS_SCORED
    assert backend.closed is True


def _fixed_now():
    return datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_effective_timeout_uses_configured_bound_without_deadline():
    # No commit deadline → the configured upper bound is used as-is.
    assert _effective_inference_timeout_seconds(None, _fixed_now, 600.0) == 600.0
    assert _effective_inference_timeout_seconds(None, _fixed_now, 180.0) == 180.0


def test_effective_timeout_deadline_cap_beats_large_configured_bound():
    # A far-off configured bound must not override a near commit deadline.
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(seconds=200)
    result = _effective_inference_timeout_seconds(deadline, lambda: now, 600.0)
    assert result == 200.0 - INFERENCE_DEADLINE_SAFETY_MARGIN_SECONDS


def test_effective_timeout_configured_bound_wins_when_smaller_than_remaining():
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(seconds=10_000)
    result = _effective_inference_timeout_seconds(deadline, lambda: now, 120.0)
    assert result == 120.0


def test_effective_timeout_skips_when_too_little_time_remains():
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(
        seconds=INFERENCE_DEADLINE_SAFETY_MARGIN_SECONDS
        + MIN_INFERENCE_READ_TIMEOUT_SECONDS
        - 1
    )
    assert _effective_inference_timeout_seconds(deadline, lambda: now, 600.0) is None


def test_full_score_abandons_when_round_deadline_budget_elapsed(tmp_path):
    deadline = (
        datetime.now(timezone.utc) + timedelta(seconds=5)
    ).isoformat()
    config = _setup(tmp_path)
    backend = FakeBackend()

    # The commit window is known only from the on-chain announcement windows
    # persisted in local state on an earlier pass.
    with SidecarState(tmp_path) as state:
        state.record_announcement_windows(
            "testnet",
            RoundMetadata(
                round_id=123,
                round_number=456,
                status="INPUT_FROZEN",
                input_package_cid="QmInput",
                input_package_hash=PACKAGE_HASH,
                input_frozen_at="2026-05-25T00:00:00+00:00",
                final_bundle_cid=None,
            ),
            commit_opens_at="2026-05-25T00:00:00+00:00",
            commit_closes_at=deadline,
            reveal_opens_at=deadline,
            reveal_closes_at=deadline,
        )

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
        now_fn=lambda: datetime.now(timezone.utc),
    )

    assert result.status == SCORE_STATUS_SCORING_FAILED
    assert result.error_category == FailureCategory.INFERENCE_TIMEOUT.value
    assert backend.run_count == 0
    assert backend.closed is False

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)
    assert record.sidecar_state == STATE_SCORING_FAILED
    assert "round_deadline_elapsed" in record.error_details


def test_override_round_is_skipped_without_inference(tmp_path):
    config = _setup(tmp_path)
    manifest = _manifest()
    manifest["round"] = {**manifest["round"], "kind": "override", "inference_performed": False}
    backend = FakeBackend()

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(manifest),
    )

    assert result.status == SCORE_STATUS_SKIPPED
    assert result.sidecar_state == STATE_SKIPPED
    assert result.error_category == FailureCategory.SKIPPED_OVERRIDE.value
    assert backend.run_count == 0


def test_manifest_incompatible_marks_scoring_failed(tmp_path):
    config = _setup(tmp_path, deployment_record=_deployment_record(gpu_class="A100"))
    backend = FakeBackend()

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_SCORING_FAILED
    assert result.error_category == FailureCategory.MANIFEST_INCOMPATIBLE.value
    assert backend.run_count == 0


def test_deferred_comparison_completes_without_reinference(tmp_path):
    config = _setup(tmp_path)
    backend = FakeBackend()

    first = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )
    assert first.status == SCORE_STATUS_COMPARISON_PENDING
    assert backend.run_count == 1

    second = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=_no_backend,
        foundation_hash_fetcher=lambda *args: dict(EXPECTED_HASHES),
        package_fetcher=_no_fetch,
    )

    assert second.status == SCORE_STATUS_SCORED
    assert second.matched_levels == ["RAW_MATCH", "PARSED_MATCH"]
    assert backend.run_count == 1


def test_deferred_comparison_preserves_committed_state_and_reveal_miss(tmp_path):
    config = _setup(tmp_path)

    first = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: FakeBackend(),
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )
    assert first.status == SCORE_STATUS_COMPARISON_PENDING

    # The round then commits and misses its reveal window before the foundation
    # publishes its hashes.
    with SidecarState(tmp_path) as state:
        existing = state.get_round("testnet", 123)
        metadata = RoundMetadata(
            round_id=existing.round_id,
            round_number=existing.round_number,
            status=existing.scoring_status,
            input_package_cid=existing.input_package_cid,
            input_package_hash=existing.input_package_hash,
            input_frozen_at=existing.input_frozen_at,
            final_bundle_cid=None,
        )
        state.record_commit(
            "testnet",
            metadata,
            CommitOutcome(
                validator_master_key="nHValidatorKey",
                salt="d" * 64,
                commit_tx_hash="TX1",
                commitment_hash="c" * 64,
                commit_opens_at="2026-05-25T00:00:00+00:00",
                commit_closes_at="2026-05-25T00:30:00+00:00",
                reveal_opens_at="2026-05-25T00:30:00+00:00",
                reveal_closes_at="2026-05-25T01:00:00+00:00",
            ),
        )
        state.record_reveal_miss(
            "testnet",
            metadata,
            error_category=FailureCategory.REVEAL_WINDOW_MISSED.value,
        )

    # The foundation hashes arrive; the deferred comparison must complete without
    # downgrading the lifecycle or erasing the reveal-stage miss.
    second = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=_no_backend,
        foundation_hash_fetcher=lambda *args: dict(EXPECTED_HASHES),
        package_fetcher=_no_fetch,
    )

    assert second.status == SCORE_STATUS_SCORED
    assert second.sidecar_state == STATE_COMMITTED

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)
    assert record.sidecar_state == STATE_COMMITTED
    assert record.comparison_levels_matched == "RAW_MATCH,PARSED_MATCH"
    assert record.reveal_error_category == FailureCategory.REVEAL_WINDOW_MISSED.value
    assert record.commit_tx_hash == "TX1"


def test_already_scored_round_is_a_noop(tmp_path):
    config = _setup(tmp_path)
    backend = FakeBackend()

    score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: dict(EXPECTED_HASHES),
        package_fetcher=_make_package_fetcher(_manifest()),
    )
    assert backend.run_count == 1

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=_no_backend,
        foundation_hash_fetcher=lambda *args: dict(EXPECTED_HASHES),
        package_fetcher=_no_fetch,
    )

    assert result.status == SCORE_STATUS_ALREADY_SCORED
    assert backend.run_count == 1


def test_default_factory_missing_credentials_records_scoring_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTFIAT_SIDECAR_MODAL_KEY", raising=False)
    monkeypatch.delenv("POSTFIAT_SIDECAR_MODAL_SECRET", raising=False)
    config = _setup(tmp_path)

    # No backend_factory injected — exercises the real default factory, which
    # builds a ModalBackend from the env and finds no credentials.
    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_SCORING_FAILED
    assert result.sidecar_state == STATE_SCORING_FAILED
    assert result.error_category == FailureCategory.RUNTIME_UNAVAILABLE.value
    assert result.error_details[FAILURE_REASON_KEY] == FAILURE_REASON_CONFIGURATION
    assert "POSTFIAT_SIDECAR_MODAL_KEY" in result.error_details["message"]

    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)
    persisted = json.loads(record.error_details)
    assert persisted[FAILURE_REASON_KEY] == FAILURE_REASON_CONFIGURATION
    assert "POSTFIAT_SIDECAR_MODAL_KEY" in persisted["message"]


def test_deferred_falls_back_to_rescore_when_persisted_hashes_missing(tmp_path):
    config = _setup(tmp_path)
    backend = FakeBackend()

    score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )
    assert backend.run_count == 1

    (tmp_path / "scored" / PACKAGE_HASH / "verification_hashes.json").unlink()

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda *args: dict(EXPECTED_HASHES),
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.status == SCORE_STATUS_SCORED
    assert backend.run_count == 2


def _config_without_record(tmp_path):
    return load_config(
        base_url="https://scoring.example.org",
        data_dir=tmp_path,
        network="testnet",
        environ={},
    )


def _never_provision(manifest):
    raise AssertionError("provisioning must not run on this path")


def test_missing_record_without_provisioner_raises(tmp_path):
    with pytest.raises(DeploymentError):
        score_round(
            _config_without_record(tmp_path),
            FakeClient(),
            round_id=123,
            backend_factory=_no_backend,
            foundation_hash_fetcher=lambda *args: None,
            package_fetcher=_make_package_fetcher(_manifest()),
        )


def test_missing_record_is_provisioned_for_modal(tmp_path):
    backend = FakeBackend()
    provisioned = []

    def provision(manifest):
        provisioned.append(manifest)
        return _deployment_record()

    result = score_round(
        _config_without_record(tmp_path),
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda client, metadata, cfg: dict(EXPECTED_HASHES),
        package_fetcher=_make_package_fetcher(_manifest()),
        runtime_provisioner=provision,
    )

    assert len(provisioned) == 1
    assert provisioned[0]["round"]["round_number"] == 456
    assert result.status == SCORE_STATUS_SCORED
    assert backend.run_count == 1


def test_stale_modal_record_is_reprovisioned(tmp_path):
    config = _setup(
        tmp_path, deployment_record=_deployment_record(model_revision="b" * 40)
    )
    backend = FakeBackend()
    provisioned = []

    def provision(manifest):
        provisioned.append(manifest)
        return _deployment_record()

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=lambda record: backend,
        foundation_hash_fetcher=lambda client, metadata, cfg: dict(EXPECTED_HASHES),
        package_fetcher=_make_package_fetcher(_manifest()),
        runtime_provisioner=provision,
    )

    assert len(provisioned) == 1
    assert result.status == SCORE_STATUS_SCORED
    assert backend.run_count == 1


def test_local_record_is_never_reprovisioned(tmp_path):
    config = _setup(
        tmp_path,
        deployment_record=_deployment_record(mode="local", model_revision="b" * 40),
    )

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=_no_backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
        runtime_provisioner=_never_provision,
    )

    assert result.status == SCORE_STATUS_SCORING_FAILED
    assert result.error_category == FailureCategory.MANIFEST_INCOMPATIBLE.value


def test_unfixable_failure_is_not_provisioned(tmp_path):
    # Vendored-code drift cannot be fixed by redeploying the runtime, so a
    # stale Modal record must not trigger a deploy loop.
    manifest = _manifest()
    manifest["code"]["parser"]["content_sha256"] = "f" * 64
    config = _setup(
        tmp_path, deployment_record=_deployment_record(model_revision="b" * 40)
    )

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        backend_factory=_no_backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(manifest),
        runtime_provisioner=_never_provision,
    )

    assert result.status == SCORE_STATUS_SCORING_FAILED
    assert result.error_category == FailureCategory.MANIFEST_INCOMPATIBLE.value


def test_dry_run_round_is_skipped_without_provisioning(tmp_path):
    manifest = _manifest()
    manifest["round"] = {**manifest["round"], "kind": "dry_run"}

    result = score_round(
        _config_without_record(tmp_path),
        FakeClient(),
        round_id=123,
        backend_factory=_no_backend,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(manifest),
        runtime_provisioner=_never_provision,
    )

    assert result.status == SCORE_STATUS_SKIPPED
    assert result.error_category == FailureCategory.SKIPPED_OPERATOR_OPT_OUT.value


def test_score_round_wires_persisted_call_id_into_default_factory(
    tmp_path, monkeypatch
):
    config = _setup(tmp_path)
    metadata = RoundMetadata(
        round_id=123,
        round_number=456,
        status="INPUT_FROZEN",
        input_package_cid="QmInput",
        input_package_hash=PACKAGE_HASH,
        input_frozen_at="2026-05-25T00:00:00+00:00",
        final_bundle_cid=None,
    )
    with SidecarState(tmp_path) as state:
        state.record_discovered("testnet", metadata)
        state.record_inference_call(
            "testnet", metadata, inference_call_id="fc-resume"
        )

    captured = {}

    def fake_default_factory(record, *, timeout_seconds, pending_call_id=None,
                             on_call_submitted=None):
        captured["pending_call_id"] = pending_call_id
        captured["on_call_submitted"] = on_call_submitted
        return FakeBackend()

    import validator_scoring_sidecar.score as score_module

    monkeypatch.setattr(
        score_module, "_default_backend_factory", fake_default_factory
    )

    result = score_round(
        config,
        FakeClient(),
        round_id=123,
        foundation_hash_fetcher=lambda *args: None,
        package_fetcher=_make_package_fetcher(_manifest()),
    )

    assert result.sidecar_state == STATE_SCORED
    assert captured["pending_call_id"] == "fc-resume"

    # The submit callback persists the new call id into the round state.
    captured["on_call_submitted"]("fc-fresh")
    with SidecarState(tmp_path) as state:
        record = state.get_round("testnet", 123)
    assert record.inference_call_id == "fc-fresh"
