"""Participation readiness preflight.

Runs the checks that otherwise only surface at the first live scoring round —
relay wallet derivation and funding, validator-key readability, RPC
reachability, foundation publisher discovery, and (by default) a full
reproduction of the latest completed round — and returns one consolidated
READY / NOT READY verdict with a per-check reason. Cheap checks run first so a
misconfiguration is reported before any inference runtime is spent, and no
secret material (relay seed, inference credentials, validator key contents) is
placed in the result.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.wallet import relay_wallet_from_secret

# Funding floor for the relay account: the account reserve plus a runway of
# per-round commit+reveal fees. This assumes the devnet/testnet base reserve of
# 1 PFT (1,000,000 drops), so it leaves many rounds of headroom above reserve; a
# network with a higher reserve would need this revisited.
MIN_RELAY_BALANCE_DROPS = 5_000_000

CHECK_RELAY_WALLET = "relay_wallet"
CHECK_VALIDATOR_KEY = "validator_key"
CHECK_RPC = "rpc"
CHECK_RELAY_FUNDING = "relay_funding"
CHECK_PUBLISHER = "foundation_publisher"
CHECK_REPRODUCTION = "round_reproduction"


class BalanceRpcClient(Protocol):
    """The RPC surface preflight needs, injectable so tests avoid a live node."""

    def latest_validated_ledger_close_time(self) -> Any: ...

    def account_balance_drops(self, address: str) -> int | None: ...


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class PreflightReport:
    ready: bool
    relay_address: str | None
    checks: list[CheckResult]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "relay_address": self.relay_address,
            "checks": [c.as_dict() for c in self.checks],
        }

    def render(self) -> str:
        lines = [f"Preflight: {'READY' if self.ready else 'NOT READY'}"]
        if self.relay_address:
            lines.append(f"  relay address: {self.relay_address}")
        for check in self.checks:
            mark = "PASS" if check.ok else "FAIL"
            lines.append(f"  [{mark}] {check.name}: {check.detail}")
        return "\n".join(lines)


def run_preflight(
    config: SidecarConfig,
    *,
    rpc_client: BalanceRpcClient,
    resolve_publisher: Callable[[], str],
    run_reproduction: Callable[[], CheckResult] | None,
    min_balance_drops: int = MIN_RELAY_BALANCE_DROPS,
) -> PreflightReport:
    """Run the readiness checks and return the consolidated verdict.

    ``resolve_publisher`` returns the foundation publisher address or raises.
    ``run_reproduction`` reproduces the latest round and returns its own
    ``CheckResult``; pass ``None`` to skip that runtime-intensive step (the
    ``--quick`` config-only mode). The heavy reproduction is only attempted when
    every fast check passed, so a broken configuration never spends inference
    runtime.
    """
    checks: list[CheckResult] = []

    relay_address = _derive_relay_address(config, checks)
    checks.append(_check_validator_key(config.validator_keys_path))
    rpc_ok = _check_rpc(rpc_client, config.pftl_rpc_url, checks)
    checks.append(_check_funding(rpc_client, relay_address, rpc_ok, min_balance_drops))
    checks.append(_check_publisher(resolve_publisher))

    fast_checks_ok = all(check.ok for check in checks)
    checks.append(_reproduction_check(run_reproduction, fast_checks_ok))

    ready = all(check.ok for check in checks)
    return PreflightReport(ready=ready, relay_address=relay_address, checks=checks)


def _derive_relay_address(
    config: SidecarConfig, checks: list[CheckResult]
) -> str | None:
    seed = config.validator_wallet_seed
    if not seed:
        checks.append(
            CheckResult(
                CHECK_RELAY_WALLET,
                False,
                "no relay wallet secret set (POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED)",
            )
        )
        return None
    try:
        address = relay_wallet_from_secret(seed).classic_address
    except Exception:
        checks.append(
            CheckResult(
                CHECK_RELAY_WALLET,
                False,
                "relay wallet secret is not a valid recovery phrase or s... seed",
            )
        )
        return None
    checks.append(CheckResult(CHECK_RELAY_WALLET, True, f"resolves to {address}"))
    return address


def _check_validator_key(keys_path: str | None) -> CheckResult:
    if not keys_path:
        return CheckResult(
            CHECK_VALIDATOR_KEY,
            False,
            "no validator-keys file set (POSTFIAT_SIDECAR_VALIDATOR_KEYS_FILE)",
        )
    if not os.path.exists(keys_path):
        return CheckResult(
            CHECK_VALIDATOR_KEY, False, f"validator-keys file not found at {keys_path}"
        )
    if not os.access(keys_path, os.R_OK):
        return CheckResult(
            CHECK_VALIDATOR_KEY,
            False,
            f"validator-keys file at {keys_path} is not readable by this process "
            "(check file ownership/permissions)",
        )
    return CheckResult(
        CHECK_VALIDATOR_KEY, True, f"validator-keys file readable at {keys_path}"
    )


def _check_rpc(
    rpc_client: BalanceRpcClient, rpc_url: str, checks: list[CheckResult]
) -> bool:
    try:
        rpc_client.latest_validated_ledger_close_time()
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
        checks.append(
            CheckResult(CHECK_RPC, False, f"RPC at {rpc_url} is not reachable: {exc}")
        )
        return False
    checks.append(CheckResult(CHECK_RPC, True, f"RPC reachable at {rpc_url}"))
    return True


def _check_funding(
    rpc_client: BalanceRpcClient,
    relay_address: str | None,
    rpc_ok: bool,
    min_balance_drops: int,
) -> CheckResult:
    if relay_address is None:
        return CheckResult(CHECK_RELAY_FUNDING, False, "skipped: relay address unavailable")
    if not rpc_ok:
        return CheckResult(CHECK_RELAY_FUNDING, False, "skipped: RPC unreachable")
    try:
        balance = rpc_client.account_balance_drops(relay_address)
    except Exception as exc:  # noqa: BLE001 - report the RPC failure, don't crash
        return CheckResult(
            CHECK_RELAY_FUNDING, False, f"could not read relay balance: {exc}"
        )
    if balance is None:
        return CheckResult(
            CHECK_RELAY_FUNDING, False, "relay account not found on ledger (unfunded)"
        )
    if balance < min_balance_drops:
        return CheckResult(
            CHECK_RELAY_FUNDING,
            False,
            f"relay balance {balance} drops is below the {min_balance_drops}-drop "
            "floor; fund the relay account",
        )
    return CheckResult(CHECK_RELAY_FUNDING, True, f"relay balance {balance} drops")


def _check_publisher(resolve_publisher: Callable[[], str]) -> CheckResult:
    try:
        address = resolve_publisher()
    except Exception as exc:  # noqa: BLE001 - a missing publisher is a failed check
        return CheckResult(
            CHECK_PUBLISHER, False, f"foundation publisher not discoverable: {exc}"
        )
    return CheckResult(CHECK_PUBLISHER, True, f"foundation publisher {address}")


def _reproduction_check(
    run_reproduction: Callable[[], CheckResult] | None, fast_checks_ok: bool
) -> CheckResult:
    if run_reproduction is None:
        return CheckResult(
            CHECK_REPRODUCTION,
            True,
            "skipped (--quick); inference and reproduction were not verified",
        )
    if not fast_checks_ok:
        return CheckResult(
            CHECK_REPRODUCTION,
            False,
            "skipped: resolve the failing checks above before spending inference runtime",
        )
    return run_reproduction()
