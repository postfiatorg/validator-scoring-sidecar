#!/bin/sh
set -e

# Pass-through mode lets operators run one-shot commands inside the container.
if [ "$#" -gt 0 ]; then
    exec validator-scoring-sidecar "$@"
fi

log() {
    printf '%s validator-scoring-sidecar: %s\n' "$(date -Iseconds)" "$1"
}

# The default loop verifies frozen input packages only. Set MODE=participate to
# run the full on-chain commit-reveal participation pass at the chain-poll cadence
# (requires the participation prerequisites; the command fails fast otherwise).
mode="${POSTFIAT_SIDECAR_MODE:-sync}"

# Watchdog budgets, in seconds. DEFAULT_INFERENCE_TIMEOUT must stay in sync with
# DEFAULT_INFERENCE_TIMEOUT_SECONDS in src/validator_scoring_sidecar/config.py —
# it is only a fallback for sizing the watchdog when the env var is unset.
DEFAULT_INFERENCE_TIMEOUT=180
WATCHDOG_HEADROOM=60           # slack the participate watchdog keeps over one inference
PARTICIPATE_PASS_BUDGET=180    # fetch/verify/submit work wrapped around the inference
PARTICIPATE_WATCHDOG_FLOOR=360 # never derive a participate watchdog below this
SYNC_WATCHDOG=900              # verify-only default; a first sync fetches a full package

# The inference read timeout (POSTFIAT_SIDECAR_INFERENCE_TIMEOUT_SECONDS) bounds
# one scoring call. The participation watchdog must stay large enough to contain
# a full inference plus the work around it, so it is derived from — and validated
# against — that bound. The Python config loader is the authority on the actual
# timeout; here we only need a safe integer to size the watchdog. Trim whitespace
# (a common .env artifact), then take the integer part. A set-but-unparseable
# value fails closed rather than silently shrinking the budget, which would let
# the watchdog fire mid-inference.
raw_inference_timeout="${POSTFIAT_SIDECAR_INFERENCE_TIMEOUT_SECONDS:-$DEFAULT_INFERENCE_TIMEOUT}"
inference_timeout_int="$(printf '%s' "$raw_inference_timeout" | tr -d ' \t\n\r')"
inference_timeout_int="${inference_timeout_int%%.*}"
case "$inference_timeout_int" in
    ''|*[!0-9]*)
        log "POSTFIAT_SIDECAR_INFERENCE_TIMEOUT_SECONDS must be a positive integer number of seconds, got '${raw_inference_timeout}'"
        exit 2
        ;;
esac
if [ "$inference_timeout_int" -le 0 ]; then
    log "POSTFIAT_SIDECAR_INFERENCE_TIMEOUT_SECONDS must be greater than zero, got '${raw_inference_timeout}'"
    exit 2
fi
watchdog_min=$((inference_timeout_int + WATCHDOG_HEADROOM))

case "$mode" in
    sync)
        command="sync"
        interval="${POSTFIAT_SIDECAR_SYNC_INTERVAL_SECONDS:-3600}"
        # Verify-only mode still needs a watchdog: a stuck package download would
        # otherwise hang the container indefinitely.
        command_timeout="${POSTFIAT_SIDECAR_COMMAND_TIMEOUT_SECONDS:-$SYNC_WATCHDOG}"
        ;;
    participate)
        command="participate"
        interval="${POSTFIAT_SIDECAR_CHAIN_POLL_INTERVAL_SECONDS:-60}"
        # Derive the default so it always contains a full inference plus the work
        # around it, and never drops below the floor.
        default_participate_timeout=$((inference_timeout_int + PARTICIPATE_PASS_BUDGET))
        if [ "$default_participate_timeout" -lt "$PARTICIPATE_WATCHDOG_FLOOR" ]; then
            default_participate_timeout="$PARTICIPATE_WATCHDOG_FLOOR"
        fi
        command_timeout="${POSTFIAT_SIDECAR_COMMAND_TIMEOUT_SECONDS:-$default_participate_timeout}"
        ;;
    *)
        log "POSTFIAT_SIDECAR_MODE must be 'sync' or 'participate', got '${mode}'"
        exit 2
        ;;
esac

case "$interval" in
    ''|*[!0-9]*)
        log "loop interval must be a positive integer, got '${interval}'"
        exit 2
        ;;
esac

case "$command_timeout" in
    ''|*[!0-9]*)
        if [ -n "$command_timeout" ]; then
            log "command timeout must be a positive integer, got '${command_timeout}'"
            exit 2
        fi
        ;;
esac
if [ -n "$command_timeout" ] && [ "$command_timeout" -le 0 ]; then
    log "command timeout must be greater than zero, got '${command_timeout}'"
    exit 2
fi

# The participation watchdog must never be able to fire mid-inference: reject a
# command timeout that leaves no room for a full inference plus head-room.
if [ "$mode" = "participate" ] && [ -n "$command_timeout" ] \
    && [ "$command_timeout" -le "$watchdog_min" ]; then
    log "command timeout ${command_timeout}s must exceed the inference timeout ${inference_timeout_int}s plus head-room (> ${watchdog_min}s)"
    exit 2
fi

# Installed before the warm-up so a shutdown signal during the (potentially long)
# first Modal build is handled gracefully rather than by default disposition.
trap 'log "received shutdown signal"; exit 0' TERM INT

# Pre-provision the manifest-pinned Modal endpoint before the first round so the
# one-time Modal build and cold start do not fall inside a round's commit window.
# Non-fatal: the participation loop still provisions on demand, and warm-runtime
# is a no-op for local SGLang and when Modal credentials are absent.
if [ "$mode" = "participate" ]; then
    log "provisioning inference runtime before the first round"
    if validator-scoring-sidecar warm-runtime; then
        log "runtime warm-up complete"
    else
        log "runtime warm-up did not complete; the loop will provision on demand"
    fi
fi

# `--version` prints "validator-scoring-sidecar X.Y.Z"; log() already prefixes
# the name, so keep only the version field to avoid repeating it.
version="$(validator-scoring-sidecar --version 2>/dev/null | awk '{print $NF}')"
log "version ${version:-unknown}"
log "starting ${mode} loop (interval=${interval}s)"

while true; do
    validator-scoring-sidecar "$command" &
    run_pid=$!
    timeout_marker="/tmp/validator-scoring-sidecar-timeout-${run_pid}"
    watchdog_pid=""
    if [ -n "$command_timeout" ]; then
        (
            sleep "$command_timeout"
            if kill -0 "$run_pid" 2>/dev/null; then
                log "${command} exceeded ${command_timeout}s watchdog; terminating"
                : > "$timeout_marker"
                kill -TERM "$run_pid" 2>/dev/null || true
                sleep 10
                kill -KILL "$run_pid" 2>/dev/null || true
            fi
        ) &
        watchdog_pid=$!
    fi
    if wait "$run_pid"; then
        if [ -f "$timeout_marker" ]; then
            rm -f "$timeout_marker"
            log "${command} watchdog fired; exiting for container restart"
            exit 124
        fi
        log "${command} completed; sleeping ${interval}s"
    else
        if [ -f "$timeout_marker" ]; then
            rm -f "$timeout_marker"
            log "${command} watchdog fired; exiting for container restart"
            exit 124
        fi
        log "${command} failed; sleeping ${interval}s before retry"
    fi
    if [ -n "$watchdog_pid" ]; then
        kill "$watchdog_pid" 2>/dev/null || true
        wait "$watchdog_pid" 2>/dev/null || true
    fi
    rm -f "$timeout_marker"
    sleep "$interval" &
    wait $!
done
