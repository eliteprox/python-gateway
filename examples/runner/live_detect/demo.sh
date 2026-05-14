#!/usr/bin/env bash
# SDK live demo helper: start live-detect and print data-channel records.

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"

JOB_FILE=$(mktemp -t live_detect_demo.XXXXXX.job.json)
trap 'kill ${SUB_PID:-} 2>/dev/null || true; PYTHONPATH=../../../src python3 ../byoc_live.py stop --job-file "${JOB_FILE:-}" >/dev/null 2>&1 || true; rm -f "${JOB_FILE:-}"' EXIT

echo "Starting live-detect via SDK..."
PYTHONPATH=../../../src python3 ../byoc_live.py start \
    --token "${LIVEPEER_TOKEN}" \
    --capability live-detect \
    --enable-data-output \
    --job-file "${JOB_FILE}" \
    --timeout-seconds "${TIMEOUT_SECONDS:-600}" >/dev/null

python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, sort_keys=True))' "${JOB_FILE}"

echo
echo "Listening for detection/transcript records. Publish media to publish_url with an SDK media publisher."
PYTHONPATH=../../../src python3 ../byoc_live.py subscribe-data --job-file "${JOB_FILE}" \
    | grep --line-buffered '^data: {' \
    | sed -u 's/^data: //' \
    | python3 -u _format_records.py &
SUB_PID=$!

wait "${SUB_PID}"
