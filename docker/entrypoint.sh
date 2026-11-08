#!/usr/bin/env bash
set -euo pipefail

checkrr_config="${CHECKRR_CONFIG:-/config/checkrr.yaml}"
mediacheck_dir="${MEDIACHECK_DIR:-/opt/mediacheck}"
poll_seconds="${MEDIACHECK_POLL_SECONDS:-5}"

if [[ ! -r "$checkrr_config" ]]; then
    echo "Checkrr config is not readable: $checkrr_config" >&2
    exit 1
fi
if [[ ! -r "$MEDIACHECK_CONFIG_DIR/mediacheck.yaml" ]]; then
    echo "Mediacheck config is not readable: $MEDIACHECK_CONFIG_DIR/mediacheck.yaml" >&2
    exit 1
fi
if [[ ! -r "$MEDIACHECK_CONFIG_DIR/checkrr.yaml" ]]; then
    echo "Mediacheck cannot read the shared Arr config: $MEDIACHECK_CONFIG_DIR/checkrr.yaml" >&2
    exit 1
fi
if ! [[ "$poll_seconds" =~ ^[0-9]+$ ]] || (( poll_seconds < 1 )); then
    echo "MEDIACHECK_POLL_SECONDS must be a positive integer" >&2
    exit 1
fi

mkdir -p "$MEDIACHECK_DATA_DIR"

/checkrr -c "$checkrr_config" &
checkrr_pid=$!
python3 "$mediacheck_dir/mediacheck" service --poll-seconds "$poll_seconds" &
mediacheck_pid=$!

shutdown() {
    trap - INT TERM
    kill -TERM "$checkrr_pid" "$mediacheck_pid" 2>/dev/null || true
    wait "$checkrr_pid" 2>/dev/null || true
    wait "$mediacheck_pid" 2>/dev/null || true
}

trap 'shutdown; exit 0' INT TERM
set +e
wait -n "$checkrr_pid" "$mediacheck_pid"
status=$?
set -e
shutdown
exit "$status"
