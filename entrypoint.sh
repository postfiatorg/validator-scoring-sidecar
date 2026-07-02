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
case "$mode" in
    sync)
        command="sync"
        interval="${POSTFIAT_SIDECAR_SYNC_INTERVAL_SECONDS:-3600}"
        command_timeout="${POSTFIAT_SIDECAR_COMMAND_TIMEOUT_SECONDS:-}"
        ;;
    participate)
        command="participate"
        interval="${POSTFIAT_SIDECAR_CHAIN_POLL_INTERVAL_SECONDS:-60}"
        command_timeout="${POSTFIAT_SIDECAR_COMMAND_TIMEOUT_SECONDS:-360}"
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
