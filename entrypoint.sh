#!/bin/sh
set -e

# Pass-through mode lets operators run one-shot commands inside the container.
if [ "$#" -gt 0 ]; then
    exec validator-scoring-sidecar "$@"
fi

log() {
    printf '%s validator-scoring-sidecar: %s\n' "$(date -Iseconds)" "$1"
}

interval="${POSTFIAT_SIDECAR_SYNC_INTERVAL_SECONDS:-3600}"
case "$interval" in
    ''|*[!0-9]*)
        log "POSTFIAT_SIDECAR_SYNC_INTERVAL_SECONDS must be a positive integer, got '${interval}'"
        exit 2
        ;;
esac

log "starting sync loop (interval=${interval}s)"

trap 'log "received shutdown signal"; exit 0' TERM INT

while true; do
    validator-scoring-sidecar sync &
    sync_pid=$!
    if wait "$sync_pid"; then
        log "sync completed; sleeping ${interval}s"
    else
        log "sync failed; sleeping ${interval}s before retry"
    fi
    sleep "$interval" &
    wait $!
done
