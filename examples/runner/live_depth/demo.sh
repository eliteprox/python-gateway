#!/usr/bin/env bash
# SDK live demo helper: start a live-depth BYOC job, print media URLs, and stop
# when the user exits.

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"

JOB_FILE=$(mktemp -t live_depth_demo.XXXXXX.job.json)
trap 'PYTHONPATH=../../../src python3 ../byoc_live.py stop --job-file "${JOB_FILE:-}" >/dev/null 2>&1 || true; rm -f "${JOB_FILE:-}"' EXIT

echo "Starting live-depth via SDK..."
PYTHONPATH=../../../src python3 ../byoc_live.py start \
    --token "${LIVEPEER_TOKEN}" \
    --capability live-depth \
    --job-file "${JOB_FILE}" \
    --timeout-seconds "${TIMEOUT_SECONDS:-600}" >/dev/null

python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, sort_keys=True))' "${JOB_FILE}"

echo
echo "Publish media to publish_url with an SDK media publisher. Press Ctrl-C to stop."
sleep "${DURATION:-600}"
