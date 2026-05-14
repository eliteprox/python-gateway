#!/usr/bin/env bash
# E2E test for live_transcribe — pushes a known English speech clip (JFK's
# "Ask not..." inaugural, ~11 s) through the full BYOC stack, subscribes to
# the SDK data SSE subscriber, and asserts JSON transcript records arrive
# containing recognisable words from the speech.
#
# Path: SDK start → orch → runner → emit_data → SDK SSE subscriber.

set -euo pipefail
cd "$(dirname "$0")"

: "${LIVEPEER_TOKEN:?Set LIVEPEER_TOKEN to a BYOC token with signer/discovery credentials.}"

echo "Waiting for capability registration..."
# SDK self-registers inside the pipeline container; look for the log line
# emitted by livepeer_gateway.runner.registration.register().
# TODO: switch to `curl /status` once the SDK exposes a status endpoint
# (Phase 2 of auto-registration). Structured check beats log grep.
for _ in $(seq 30); do
    if docker logs live_transcribe 2>&1 | grep -q "registered capability=live-transcribe"; then
        echo "  registered."
        break
    fi
    sleep 1
done
if ! docker logs live_transcribe 2>&1 | grep -q "registered capability=live-transcribe"; then
    echo "FAIL: live_transcribe container hasn't logged registration success." >&2
    echo "Make sure 'docker compose up -d --wait --build' completed first." >&2
    exit 1
fi

# Best-effort session cleanup; registered early to catch Ctrl-C.
JOB_FILE=$(mktemp -t live_transcribe.XXXXXX.job.json)
trap 'kill ${SUB_PID:-} 2>/dev/null || true; PYTHONPATH=../../../src python3 ../byoc_live.py stop --job-file "${JOB_FILE:-}" >/dev/null 2>&1 || true; rm -f "${JOB_FILE:-}"' EXIT

# JFK "Ask not..." (~11s, 44.1kHz stereo) — whisper's own test fixture, so
# tiny.en transcribes it reliably. Cached under assets/ on first run.
SAMPLE_URL="${SAMPLE_URL:-https://github.com/openai/whisper/raw/main/tests/jfk.flac}"
SAMPLE_FILE="${SAMPLE_FILE:-assets/jfk.flac}"
if [ ! -f "${SAMPLE_FILE}" ]; then
    echo "Downloading sample audio..."
    mkdir -p "$(dirname "${SAMPLE_FILE}")"
    curl -fsSL -o "${SAMPLE_FILE}" "${SAMPLE_URL}"
fi

echo "Starting stream session..."
PYTHONPATH=../../../src python3 ../byoc_live.py start \
    --token "${LIVEPEER_TOKEN}" \
    --capability live-transcribe \
    --enable-data-output \
    --job-file "${JOB_FILE}" \
    --timeout-seconds 120 >/dev/null

STREAM_ID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['job_id'])" "${JOB_FILE}")
RTMP_IN=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['publish_url'])" "${JOB_FILE}")
DATA_URL=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['data_url'])" "${JOB_FILE}")
echo "  stream_id=${STREAM_ID}"
echo "  rtmp_in =${RTMP_IN}"
echo "  data_url=${DATA_URL}"

# Subscribe to the data channel SSE in the background BEFORE we push input,
# so we don't miss early transcripts. --max-time bounds it as a safety net
# in case the stream never closes cleanly. Output: raw SSE (lines starting
# with `data: ` carry the JSON payload).
SSE_OUT=$(mktemp -t live_transcribe.XXXXXX.sse)
echo "Subscribing to data channel SSE..."
PYTHONPATH=../../../src python3 ../byoc_live.py subscribe-data --job-file "${JOB_FILE}" --timeout-seconds 30 > "${SSE_OUT}" 2>/dev/null &
SUB_PID=$!
sleep 1  # let curl establish the connection before we start pushing

# 20s push: clip loops past its ~11s length so the demo gets multiple
# windowed transcribes plus the final on_stream_stop flush. Loop
# wraparound makes transcripts appear "out of order" relative to a
# single read of the speech — that's expected, the audio itself loops.
# Black video satisfies the live-transcribe capability's video ingress;
# JFK audio is what the pipeline actually consumes.
echo "Pushing JFK clip (20s, looped)..."
ffmpeg -loglevel error -re \
       -f lavfi -i "color=size=320x240:rate=15" \
       -stream_loop -1 -i "${SAMPLE_FILE}" \
       -map 0:v -map 1:a \
       -c:v libx264 -preset ultrafast -tune zerolatency -g 15 \
       -c:a aac -ar 48000 \
       -t 20 -f flv "${RTMP_IN}" </dev/null 2>/dev/null

# Let the final in-stream window + whisper inference (~3.5s) complete
# before /stream/stop fires. The gateway closes its SSE proxy on stop
# without draining, so we wait here, not after.
sleep 4
PYTHONPATH=../../../src python3 ../byoc_live.py stop --job-file "${JOB_FILE}" >/dev/null 2>&1 || true
# Buffer for the SSE subscriber to flush before we cancel the curl reader.
sleep 5
# Wait for the SSE subscriber to terminate (gateway sends `event: end`
# when the data channel closes, or curl --max-time fires).
wait "${SUB_PID}" 2>/dev/null || true
SUB_PID=""

# SSE format: `data: <payload>\n\n` per event. Strip the prefix to get raw
# JSON records, one per line.
TRANSCRIPTS=$(grep '^data: {' "${SSE_OUT}" | sed 's/^data: //' || true)

if [ -z "${TRANSCRIPTS}" ]; then
    echo "FAIL: no transcript records arrived on the data channel." >&2
    echo "Raw SSE output (head):" >&2
    head -20 "${SSE_OUT}" | sed 's/^/  /' >&2 || true
    echo "Recent runner activity:" >&2
    docker logs --tail 20 live_transcribe 2>&1 | sed 's/^/  /' >&2
    exit 1
fi

# Sanity: the transcripts should mention "country" (the JFK speech says it
# three times). Catches a no-op pipeline that emits transcripts of silence
# / hallucinations instead of the actual audio.
if ! echo "${TRANSCRIPTS}" | grep -qi "country"; then
    echo "FAIL: transcripts arrived but don't mention 'country' — got:" >&2
    echo "${TRANSCRIPTS}" | sed 's/^/  /' >&2
    exit 1
fi

COUNT=$(echo "${TRANSCRIPTS}" | wc -l)
echo "PASS (${COUNT} transcript record(s) on the data channel):"
echo "${TRANSCRIPTS}" | python3 -c "
import json, sys
for line in sys.stdin:
    rec = json.loads(line)
    if rec.get('type') == 'transcript':
        print(f\"  transcript[{rec['index']}]: {rec['text']}\")
"
