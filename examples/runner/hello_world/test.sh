#!/usr/bin/env bash
# E2E: send a request through the Python SDK, assert the response from the
# hello_world container comes back through the orchestrator.

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"
NAME="${NAME:-livepeer}"
EXPECTED_MSG="hello, ${NAME}"

echo "Waiting for capability registration..."
# SDK self-registers inside the hello_world container; look for the log line
# emitted by livepeer_gateway.runner.registration.register().
# TODO: switch to `curl /status` once the SDK exposes a status endpoint
# (Phase 2 of auto-registration). Structured check beats log grep.
for _ in $(seq 30); do
    if docker logs hello_world 2>&1 | grep -q "registered capability=hello-world"; then
        echo "  registered."
        break
    fi
    sleep 1
done
if ! docker logs hello_world 2>&1 | grep -q "registered capability=hello-world"; then
    echo "FAIL: hello_world container hasn't logged registration success."
    echo "Make sure 'docker compose up -d --wait --build' completed first."
    exit 1
fi

echo "Sending request through SDK..."
RESPONSE=$(PYTHONPATH=../../../src python3 ../byoc_request.py \
    --token "${LIVEPEER_TOKEN}" \
    --capability hello-world \
    --route run \
    --body-json "{\"name\":\"${NAME}\"}" \
    --timeout-seconds 30)

echo "Response: ${RESPONSE}"

if echo "${RESPONSE}" | grep -qE "\"message\"[[:space:]]*:[[:space:]]*\"${EXPECTED_MSG}\""; then
    echo "PASS"
    exit 0
fi

echo "FAIL"
exit 1
