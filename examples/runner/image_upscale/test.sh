#!/usr/bin/env bash
# E2E: send a request through the Python SDK, assert the upscaled image
# (2x of the 32x32 fixture = 64x64) comes back through the orchestrator.

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"
TEST_IMAGE="${TEST_IMAGE:-test_image.png}"
INPUT_WIDTH="${INPUT_WIDTH:-64}"
INPUT_HEIGHT="${INPUT_HEIGHT:-64}"

echo "Waiting for capability registration..."
# SDK self-registers inside the pipeline container; look for the log line
# emitted by livepeer_gateway.runner.registration.register().
# TODO: switch to `curl /status` once the SDK exposes a status endpoint
# (Phase 2 of auto-registration). Structured check beats log grep.
for _ in $(seq 30); do
    if docker logs image_upscale 2>&1 | grep -q "registered capability=image-upscale"; then
        echo "  registered."
        break
    fi
    sleep 1
done
if ! docker logs image_upscale 2>&1 | grep -q "registered capability=image-upscale"; then
    echo "FAIL: image_upscale container hasn't logged registration success." >&2
    echo "Make sure 'docker compose up -d --wait --build' completed first." >&2
    exit 1
fi

INPUT_B64=$(base64 -w0 < "${TEST_IMAGE}")

echo "Sending request through SDK..."
BODY_FILE=$(mktemp -t image_upscale.XXXXXX.json)
trap 'rm -f "${BODY_FILE}"' EXIT
INPUT_B64="${INPUT_B64}" python3 -c 'import json, os, sys; json.dump({"image": os.environ["INPUT_B64"]}, open(sys.argv[1], "w"))' "${BODY_FILE}"
RESPONSE=$(PYTHONPATH=../../../src python3 ../byoc_request.py \
    --token "${LIVEPEER_TOKEN}" \
    --capability image-upscale \
    --route run \
    --body-file "${BODY_FILE}" \
    --timeout-seconds 60)

# Trim the base64 image from the echoed response — keeps stdout readable.
echo "Response (image truncated): $(echo "${RESPONSE}" | sed 's/\("image":"\)[^"]*/\1<base64>/')"

WIDTH=$(echo "${RESPONSE}" | grep -oE '"width"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$')
HEIGHT=$(echo "${RESPONSE}" | grep -oE '"height"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$')

# The Swin2SR processor pads inputs to its window size before upscaling, so
# output is at least 2x input but may be slightly larger.
if [ "${WIDTH}" -ge $((INPUT_WIDTH * 2)) ] && [ "${HEIGHT}" -ge $((INPUT_HEIGHT * 2)) ]; then
    echo "PASS (${WIDTH}x${HEIGHT}, >=2x of ${INPUT_WIDTH}x${INPUT_HEIGHT})"
    exit 0
fi

echo "FAIL: expected >=${INPUT_WIDTH}x${INPUT_HEIGHT} doubled, got ${WIDTH}x${HEIGHT}"
exit 1
