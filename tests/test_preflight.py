from types import SimpleNamespace

from validator_scoring_sidecar.preflight import (
    CHECK_RELAY_FUNDING,
    CHECK_RELAY_WALLET,
    CHECK_REPRODUCTION,
    CHECK_RPC,
    CHECK_VALIDATOR_KEY,
    MIN_RELAY_BALANCE_DROPS,
    CheckResult,
    run_preflight,
)

# Canonical public BIP39 24-word test mnemonic (not anyone's wallet).
GOOD_SEED = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon abandon abandon art"
)
GOOD_ADDRESS = "rKxpJQ6hLWYbo7p1oo7WHjrcrRFv1TUQeC"


class FakeRpc:
    def __init__(self, *, balance=MIN_RELAY_BALANCE_DROPS, reachable=True):
        self._balance = balance
        self._reachable = reachable
        self.balance_calls: list[str] = []

    def latest_validated_ledger_close_time(self):
        if not self._reachable:
            raise RuntimeError("rpc down")
        return "close-time"

    def account_balance_drops(self, address):
        self.balance_calls.append(address)
        if isinstance(self._balance, Exception):
            raise self._balance
        return self._balance


def _config(*, seed=GOOD_SEED, keys_path, rpc_url="https://rpc.example"):
    return SimpleNamespace(
        validator_wallet_seed=seed,
        validator_keys_path=keys_path,
        pftl_rpc_url=rpc_url,
    )


def _key_file(tmp_path):
    path = tmp_path / "validator-keys.json"
    path.write_text("{}")
    return str(path)


def _ok_publisher():
    return "rFoundationPublisher"


def _repro_ok():
    return CheckResult(CHECK_REPRODUCTION, True, "round 288 reproduced; matched RAW, PARSED, SELECTED_UNL")


def _find(report, name):
    return next(c for c in report.checks if c.name == name)


def test_all_checks_pass_is_ready(tmp_path):
    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(balance=9_000_000),
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_ok,
    )
    assert report.ready is True
    assert report.relay_address == GOOD_ADDRESS
    assert all(c.ok for c in report.checks)


def test_unfunded_relay_is_not_ready(tmp_path):
    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(balance=None),
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_ok,
    )
    assert report.ready is False
    assert _find(report, CHECK_RELAY_FUNDING).ok is False


def test_low_balance_is_not_ready(tmp_path):
    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(balance=MIN_RELAY_BALANCE_DROPS - 1),
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_ok,
    )
    assert report.ready is False
    assert _find(report, CHECK_RELAY_FUNDING).ok is False


def test_missing_key_file_is_not_ready(tmp_path):
    report = run_preflight(
        _config(keys_path=str(tmp_path / "nope.json")),
        rpc_client=FakeRpc(),
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_ok,
    )
    assert report.ready is False
    assert _find(report, CHECK_VALIDATOR_KEY).ok is False


def test_unreachable_rpc_is_not_ready(tmp_path):
    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(reachable=False),
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_ok,
    )
    assert report.ready is False
    assert _find(report, CHECK_RPC).ok is False
    # Funding is skipped rather than crashing when the RPC is down.
    assert _find(report, CHECK_RELAY_FUNDING).ok is False


def test_undiscoverable_publisher_is_not_ready(tmp_path):
    def _bad_publisher():
        raise RuntimeError("config endpoint unreachable")

    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(),
        resolve_publisher=_bad_publisher,
        run_reproduction=_repro_ok,
    )
    assert report.ready is False


def test_invalid_seed_is_not_ready(tmp_path):
    report = run_preflight(
        _config(seed="not a real seed", keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(),
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_ok,
    )
    assert report.ready is False
    assert report.relay_address is None
    assert _find(report, CHECK_RELAY_WALLET).ok is False
    # An invalid secret must never be echoed back in the check detail.
    assert "not a real seed" not in report.render()


def test_quick_skips_reproduction(tmp_path):
    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(),
        resolve_publisher=_ok_publisher,
        run_reproduction=None,
    )
    assert report.ready is True
    repro = _find(report, CHECK_REPRODUCTION)
    assert repro.ok is True
    assert "skipped" in repro.detail.lower()


def test_reproduction_not_run_when_fast_checks_fail(tmp_path):
    called = []

    def _repro_spy():
        called.append(True)
        return _repro_ok()

    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(balance=None),  # unfunded -> a fast check fails
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_spy,
    )
    assert report.ready is False
    assert called == []  # the GPU step is never spent on a broken config
    assert _find(report, CHECK_REPRODUCTION).ok is False


def test_secret_never_appears_in_output(tmp_path):
    report = run_preflight(
        _config(keys_path=_key_file(tmp_path)),
        rpc_client=FakeRpc(),
        resolve_publisher=_ok_publisher,
        run_reproduction=_repro_ok,
    )
    assert GOOD_SEED not in report.render()
    assert GOOD_SEED not in str(report.as_dict())


# --- reproduction status -> readiness mapping (cli._reproduction_result_to_check) ---


def _score_result(status, matched_levels=()):
    from validator_scoring_sidecar.score import ScoreResult

    return ScoreResult(
        status=status,
        network="testnet",
        round_id=1,
        round_number=288,
        sidecar_state="SCORED",
        backend_mode="modal",
        compared=bool(matched_levels),
        matched_levels=list(matched_levels),
        error_category=None,
    )


def test_reproduction_scored_and_matched_is_pass():
    from validator_scoring_sidecar.cli import _reproduction_result_to_check
    from validator_scoring_sidecar.score import SCORE_STATUS_SCORED

    check = _reproduction_result_to_check(
        _score_result(SCORE_STATUS_SCORED, ["RAW", "PARSED", "SELECTED_UNL"])
    )
    assert check.ok is True


def test_reproduction_divergent_is_fail():
    from validator_scoring_sidecar.cli import _reproduction_result_to_check
    from validator_scoring_sidecar.score import SCORE_STATUS_DIVERGENT

    assert _reproduction_result_to_check(_score_result(SCORE_STATUS_DIVERGENT)).ok is False


def test_reproduction_comparison_pending_is_pass():
    # The key regression: a round still in its window has no foundation bundle to
    # compare against, but the operator successfully reproduced it — that is READY.
    from validator_scoring_sidecar.cli import _reproduction_result_to_check
    from validator_scoring_sidecar.score import SCORE_STATUS_COMPARISON_PENDING

    assert (
        _reproduction_result_to_check(_score_result(SCORE_STATUS_COMPARISON_PENDING)).ok
        is True
    )


def test_reproduction_skipped_round_is_pass():
    from validator_scoring_sidecar.cli import _reproduction_result_to_check
    from validator_scoring_sidecar.score import SCORE_STATUS_SKIPPED

    assert _reproduction_result_to_check(_score_result(SCORE_STATUS_SKIPPED)).ok is True


def test_reproduction_scoring_failed_is_fail():
    from validator_scoring_sidecar.cli import _reproduction_result_to_check
    from validator_scoring_sidecar.score import SCORE_STATUS_SCORING_FAILED

    assert (
        _reproduction_result_to_check(_score_result(SCORE_STATUS_SCORING_FAILED)).ok
        is False
    )
