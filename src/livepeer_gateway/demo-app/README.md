# Livepeer Streaming Demo

A web-based demo that demonstrates live video-to-video streaming using the
Livepeer Python SDK with pymthouse.com billing.

## Prerequisites

- Python 3.10+
- The `livepeer-gateway` package installed (from this repository)
- A pymthouse.com account (OIDC device authorization flow will prompt you to
  log in on first run)

## Quick Start

```bash
# From the repository root, install the package
pip install -e .

# Run the demo server
python src/livepeer_gateway/demo-app/streaming_demo_server.py
```

Then open http://localhost:8080 in your browser.

## How It Works

1. **Start** -- Click the Start button. The server authenticates with
   pymthouse.com using OIDC Device Authorization Flow (follow the terminal
   prompt to complete login on first run).

2. **Input** -- Choose between Webcam (browser camera) or Synthetic (generated
   gradient frames). Frames are captured as JPEG and sent to the server via
   WebSocket.

3. **Processing** -- The server decodes each JPEG into a PyAV VideoFrame,
   publishes it through the Livepeer SDK's `MediaPublish`, and subscribes to
   the orchestrator output via `MediaOutput`.

4. **Output** -- Decoded output frames are encoded back to JPEG and streamed
   to the browser over the same WebSocket for display on the AI Output canvas.

## Server Options

```
python streaming_demo_server.py [OPTIONS]

Options:
  --port PORT     HTTP port (default: 8080)
  --host HOST     Bind address (default: 0.0.0.0)
  --model MODEL   Default model ID (default: noop)
  --fps FPS       Default frames per second (default: 24)
```

## Environment Variables

| Variable | Description |
|---|---|
| `LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `LIVEPEER_ALLOW_INSECURE_TLS` | Set to `1` to skip TLS verification (development only) |

## Configuration

The demo is pre-configured to use:
- **Billing URL:** `https://pymthouse.com`
- **Client ID:** `app_561127a9ac83c8758b25e4fe`
- **Auth Flow:** OIDC Device Authorization (headless/CLI mode)

These can be changed by editing the constants at the top of
`streaming_demo_server.py`.

## Troubleshooting

**"No orchestrators available" error**
- Ensure your pymthouse.com account has billing set up and an orchestrator is
  available for the selected model.

**Camera not working**
- Make sure your browser has permission to access the camera.
- Try the Synthetic source to verify the server connection works independently
  of camera access.

**WebSocket connection fails**
- Check that the server is running and accessible on the expected port.
- Look at the server terminal for error messages.
