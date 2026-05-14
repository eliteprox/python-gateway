#!/usr/bin/env bash
# SDK live demo helper: start a live-tint BYOC job, print URLs, cycle params,
# then stop on Ctrl-C.

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"

JOB_FILE=$(mktemp -t live_tint_demo.XXXXXX.job.json)
trap 'PYTHONPATH=../../../src python3 ../byoc_live.py stop --job-file "${JOB_FILE:-}" >/dev/null 2>&1 || true; rm -f "${JOB_FILE:-}"' EXIT

echo "Starting live-tint via SDK..."
PYTHONPATH=../../../src python3 ../byoc_live.py start \
    --token "${LIVEPEER_TOKEN}" \
    --capability live-tint \
    --job-file "${JOB_FILE}" \
    --timeout-seconds "${TIMEOUT_SECONDS:-600}" >/dev/null

python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, sort_keys=True))' "${JOB_FILE}"

echo
echo "Cycling tint params. Publish media to publish_url with an SDK media publisher."
while true; do
    for params in '{"u": 80, "v": 180}' '{"u": 180, "v": 80}' '{"u": 128, "v": 128}'; do
        echo "update ${params}"
        PYTHONPATH=../../../src python3 ../byoc_live.py update \
            --job-file "${JOB_FILE}" \
            --params-json "${params}" >/dev/null
        sleep "${TINT_INTERVAL:-5}"
    done
done
