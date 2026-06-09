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
        ;;
    participate)
        command="participate"
        interval="${POSTFIAT_SIDECAR_CHAIN_POLL_INTERVAL_SECONDS:-60}"
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

log "starting ${mode} loop (interval=${interval}s)"

trap 'log "received shutdown signal"; exit 0' TERM INT

while true; do
    validator-scoring-sidecar "$command" &
    run_pid=$!
    if wait "$run_pid"; then
        log "${command} completed; sleeping ${interval}s"
    else
        log "${command} failed; sleeping ${interval}s before retry"
    fi
    sleep "$interval" &
    wait $!
done
