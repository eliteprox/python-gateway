"""
Streaming demo web server.

Bridges browser WebSocket connections to the Livepeer Python SDK using
aiohttp.  Serves a single-page HTML frontend and exposes a WebSocket
endpoint that accepts JPEG frames from the browser, feeds them through
a live-video-to-video job on Livepeer, and streams the AI output back
as JPEG frames.

Usage::

    python streaming_demo_server.py                    # defaults
    python streaming_demo_server.py --port 9000        # custom port
    python streaming_demo_server.py --model noop       # override model
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

import av
from aiohttp import web

# Ensure the livepeer_gateway package is importable when running the
# script directly from the demo-app directory.
_REPO_SRC = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.lv2v import LiveVideoToVideo, StartJobRequest, start_lv2v
from livepeer_gateway.media_publish import MediaPublishConfig

_LOG = logging.getLogger("streaming_demo")

BILLING_URL = "https://pymthouse.com"
CLIENT_ID = "app_561127a9ac83c8758b25e4fe"
DEFAULT_MODEL = "noop"
DEFAULT_FPS = 24.0
DEFAULT_PORT = 8080

_TIME_BASE = 90_000


def _configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(message)s",
        force=True,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Livepeer streaming demo web server")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT})")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Default model ID (default: {DEFAULT_MODEL})")
    p.add_argument("--fps", type=float, default=DEFAULT_FPS, help=f"Frames per second (default: {DEFAULT_FPS})")
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# JPEG <-> av.VideoFrame helpers
# ---------------------------------------------------------------------------

def _jpeg_to_video_frame(jpeg_bytes: bytes) -> av.VideoFrame:
    """Decode a JPEG blob into an av.VideoFrame (rgb24)."""
    buf = io.BytesIO(jpeg_bytes)
    container = av.open(buf, format="image2pipe", mode="r")
    frame = next(container.decode(video=0))
    container.close()
    return frame


def _video_frame_to_jpeg(frame: av.VideoFrame, quality: int = 80) -> bytes:
    """Encode an av.VideoFrame to JPEG bytes."""
    rgb = frame.reformat(format="rgb24")
    buf = io.BytesIO()
    container = av.open(buf, format="mjpeg", mode="w")
    stream = container.add_stream("mjpeg")
    stream.width = rgb.width
    stream.height = rgb.height
    stream.pix_fmt = "yuvj420p"
    stream.options = {"q:v": str(quality)}
    rgb.pts = 0
    rgb.time_base = Fraction(1, 25)
    for packet in stream.encode(rgb):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Session: one per WebSocket connection
# ---------------------------------------------------------------------------

@dataclass
class StreamSession:
    session_id: str
    model_id: str
    fps: float
    job: Optional[LiveVideoToVideo] = None
    output_task: Optional[asyncio.Task] = None
    _pts: int = field(default=0, repr=False)
    _last_time: Optional[float] = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def next_pts(self) -> tuple[int, Fraction]:
        now = time.time()
        if self._last_time is not None:
            self._pts += int((now - self._last_time) * _TIME_BASE)
        else:
            self._pts = 0
        self._last_time = now
        return self._pts, Fraction(1, _TIME_BASE)


_sessions: dict[str, StreamSession] = {}


async def _subscribe_and_forward(
    session: StreamSession,
    ws: web.WebSocketResponse,
) -> None:
    """Read decoded video frames from the orchestrator output and send
    them back to the browser as JPEG over the WebSocket."""
    if session.job is None:
        _LOG.warning("[%s] Output subscriber: no job", session.session_id)
        return
    recv_count = 0
    try:
        _LOG.info("[%s] Output subscriber starting (subscribe_url=%s)", session.session_id, session.job.subscribe_url)
        output = session.job.media_output()
        async for decoded in output.frames():
            if session._closed:
                break
            if decoded.kind != "video":
                continue
            recv_count += 1
            if recv_count <= 3 or recv_count % 50 == 0:
                _LOG.info(
                    "[%s] Output frame #%d  %dx%d  pts_time=%.3f",
                    session.session_id, recv_count,
                    decoded.width, decoded.height,
                    decoded.pts_time or 0,
                )
            try:
                jpeg = await asyncio.to_thread(_video_frame_to_jpeg, decoded.frame)
            except Exception as enc_err:
                _LOG.warning("[%s] JPEG encode error on frame #%d: %s", session.session_id, recv_count, enc_err)
                continue
            if not session._closed:
                await ws.send_bytes(jpeg)
        _LOG.info("[%s] Output subscriber ended normally after %d frames", session.session_id, recv_count)
    except asyncio.CancelledError:
        _LOG.info("[%s] Output subscriber cancelled after %d frames", session.session_id, recv_count)
        raise
    except Exception as exc:
        _LOG.warning("[%s] Output subscriber error after %d frames: %s", session.session_id, recv_count, exc, exc_info=True)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def ws_stream(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
    await ws.prepare(request)

    session_id = str(uuid.uuid4())[:8]
    model_id = request.query.get("model_id", request.app["default_model"])
    fps = float(request.query.get("fps", request.app["default_fps"]))

    session = StreamSession(
        session_id=session_id,
        model_id=model_id,
        fps=fps,
    )
    _sessions[session_id] = session
    _LOG.info("[%s] WebSocket connected  model=%s fps=%s", session_id, model_id, fps)

    try:
        _LOG.info("[%s] Starting LV2V job...", session_id)
        await ws.send_str(json.dumps({"type": "status", "status": "starting_job"}))

        job = await asyncio.to_thread(
            start_lv2v,
            None,
            StartJobRequest(model_id=model_id),
            billing_url=BILLING_URL,
            client_id=CLIENT_ID,
            headless=True,
        )
        session.job = job
        _LOG.info("[%s] Job started  publish=%s  subscribe=%s", session_id, job.publish_url, job.subscribe_url)

        # start_lv2v ran in a thread pool so payment_sender couldn't
        # start (no running event loop).  Kick it off now.
        job.start_payment_sender()

        media = job.start_media(MediaPublishConfig(fps=fps))

        session.output_task = asyncio.create_task(
            _subscribe_and_forward(session, ws)
        )

        await ws.send_str(json.dumps({
            "type": "status",
            "status": "connected",
            "job_id": job.manifest_id,
        }))

        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                try:
                    frame = await asyncio.to_thread(_jpeg_to_video_frame, msg.data)
                    pts, tb = session.next_pts()
                    frame.pts = pts
                    frame.time_base = tb
                    await media.write_frame(frame)
                except Exception as exc:
                    _LOG.debug("[%s] Frame processing error: %s", session_id, exc)
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                    if payload.get("type") == "ping":
                        await ws.send_str(json.dumps({"type": "pong", "ts": payload.get("ts")}))
                except Exception:
                    pass
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    except LivepeerGatewayError as exc:
        _LOG.error("[%s] SDK error: %s", session_id, exc)
        with suppress(Exception):
            await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
    except Exception as exc:
        _LOG.exception("[%s] Unexpected error", session_id)
        with suppress(Exception):
            await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
    finally:
        session._closed = True
        if session.output_task is not None:
            session.output_task.cancel()
            with suppress(asyncio.CancelledError):
                await session.output_task
        if session.job is not None:
            try:
                await session.job.close()
                _LOG.info("[%s] Job closed", session_id)
            except Exception as exc:
                _LOG.warning("[%s] Error closing job: %s", session_id, exc)
        _sessions.pop(session_id, None)
        _LOG.info("[%s] WebSocket disconnected", session_id)

    return ws


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    html_path = Path(__file__).parent / "streaming_demo.html"
    return web.FileResponse(html_path)


async def health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "sessions": len(_sessions),
    })


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(model: str = DEFAULT_MODEL, fps: float = DEFAULT_FPS) -> web.Application:
    app = web.Application()
    app["default_model"] = model
    app["default_fps"] = fps
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/ws/stream", ws_stream)
    return app


def main() -> None:
    _configure_logging()
    args = _parse_args()
    app = create_app(model=args.model, fps=args.fps)
    _LOG.info(
        "Starting streaming demo on http://%s:%d  (model=%s, fps=%s)",
        args.host, args.port, args.model, args.fps,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
