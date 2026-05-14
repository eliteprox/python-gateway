#!/usr/bin/env bash
# E2E: send a request through the Python SDK, assert the SentimentAnalyzer
# response (label + score) comes back through the orchestrator.

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"
TEXT="${TEXT:-Livepeer makes decentralized inference effortless}"
EXPECTED_LABEL="${EXPECTED_LABEL:-POSITIVE}"

echo "Waiting for capability registration..."
# SDK self-registers inside the pipeline container; look for the log line
# emitted by livepeer_gateway.runner.registration.register().
# TODO: switch to `curl /status` once the SDK exposes a status endpoint
# (Phase 2 of auto-registration). Structured check beats log grep.
for _ in $(seq 30); do
    if docker logs sentiment 2>&1 | grep -q "registered capability=sentiment"; then
        echo "  registered."
        break
    fi
    sleep 1
done
if ! docker logs sentiment 2>&1 | grep -q "registered capability=sentiment"; then
    echo "FAIL: sentiment container hasn't logged registration success." >&2
    echo "Make sure 'docker compose up -d --wait --build' completed first." >&2
    exit 1
fi

echo "Sending request through SDK..."
BODY=$(TEXT="${TEXT}" python3 -c 'import json, os; print(json.dumps({"text": os.environ["TEXT"]}))')
RESPONSE=$(PYTHONPATH=../../../src python3 ../byoc_request.py \
    --token "${LIVEPEER_TOKEN}" \
    --capability sentiment \
    --route run \
    --body-json "${BODY}" \
    --timeout-seconds 30)

echo "Response: ${RESPONSE}"

if echo "${RESPONSE}" | grep -qE "\"label\"[[:space:]]*:[[:space:]]*\"${EXPECTED_LABEL}\""; then
    echo "PASS"
    exit 0
fi

echo "FAIL"
exit 1
