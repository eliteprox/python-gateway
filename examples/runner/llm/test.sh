#!/usr/bin/env bash
# E2E: send a chat request through the Python SDK, assert the LLM streams
# tokens back via SSE and terminates with [DONE].

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"
PROMPT="${PROMPT:-Say hello in three words}"

echo "Waiting for capability registration..."
# SDK self-registers inside the pipeline container; look for the log line
# emitted by livepeer_gateway.runner.registration.register().
# TODO: switch to `curl /status` once the SDK exposes a status endpoint
# (Phase 2 of auto-registration). Structured check beats log grep.
for _ in $(seq 30); do
    if docker logs llm 2>&1 | grep -q "registered capability=llm"; then
        echo "  registered."
        break
    fi
    sleep 1
done
if ! docker logs llm 2>&1 | grep -q "registered capability=llm"; then
    echo "FAIL: llm container hasn't logged registration success." >&2
    echo "Make sure 'docker compose up -d --wait --build' completed first." >&2
    exit 1
fi

echo "Sending chat request through SDK (streaming)..."
BODY=$(PROMPT="${PROMPT}" python3 -c 'import json, os; print(json.dumps({"prompt": os.environ["PROMPT"]}))')
RESPONSE=$(PYTHONPATH=../../../src python3 ../byoc_request.py \
    --token "${LIVEPEER_TOKEN}" \
    --capability llm \
    --route run \
    --stream \
    --body-json "${BODY}" \
    --timeout-seconds 120)

echo "Response (first events):"
echo "${RESPONSE}" | head -10

# Verify SSE format: at least one token event + the [DONE] terminator.
if echo "${RESPONSE}" | grep -q '^data: {"token":' \
   && echo "${RESPONSE}" | grep -q '^data: \[DONE\]'; then
    echo "PASS"
    exit 0
fi

echo "FAIL: expected token events and [DONE] terminator"
exit 1
