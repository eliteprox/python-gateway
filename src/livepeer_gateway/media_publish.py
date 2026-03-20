from __future__ import annotations

import asyncio
import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Set, Awaitable, Any, BinaryIO

import av
from av.video.frame import PictureType

from .errors import LivepeerGatewayError
from .trickle_publisher import (
    TricklePublisher,
    TricklePublisherStats,
    TricklePublisherTerminalError,
    TrickleSegmentWriteError,
)

_LOG = logging.getLogger(__name__)

_OUT_TIME_BASE = Fraction(1, 90_000)
_READ_CHUNK = 64 * 1024
_DRAIN_TIMEOUT_S = 5.0
_STOP = object()


def _fraction_from_time_base(time_base: object) -> Fraction:
    numerator = getattr(time_base, "numerator", None)
    denominator = getattr(time_base, "denominator", None)
    if numerator is not None and denominator is not None:
        return Fraction(int(numerator), int(denominator))
    return Fraction(time_base)


def _rescale_pts(pts: int, src_tb: Fraction, dst_tb: Fraction) -> int:
    if src_tb == dst_tb:
        return int(pts)
    return int(round(float((Fraction(pts) * src_tb) / dst_tb)))


def _normalize_fps(fps: Optional[float]) -> int:
    if fps is None or not math.isfinite(fps) or fps <= 0:
        fps = 30.0
    return max(1, int(round(fps)))


@dataclass(frozen=True)
class MediaPublishConfig:
    fps: Optional[float] = None
    mime_type: str = "video/mp2t"
    keyframe_interval_s: float = 2.0

@dataclass(frozen=True)
class MediaPublishStats:
    elapsed_s: float
    frames_in: int
    frames_dropped_overflow: int
    frames_dropped_debt: int
    frames_dropped_non_monotonic_pts: int
    time_debt_s: float
    segments_started: int
    segments_completed: int
    segments_failed: int
    bytes_streamed_to_trickle: int
    segment_writer_put_timeouts: int
    terminal_failures: int
    encoder_errors: int
    publisher: TricklePublisherStats

    def __str__(self) -> str:
        return (
            "MediaPublishStats("
            f"elapsed_s={self.elapsed_s:.1f}, "
            f"frames_in={self.frames_in}, "
            f"frames_dropped_overflow={self.frames_dropped_overflow}, "
            f"frames_dropped_debt={self.frames_dropped_debt}, "
            f"frames_dropped_non_monotonic_pts={self.frames_dropped_non_monotonic_pts}, "
            f"time_debt_s={self.time_debt_s:.4f}, "
            f"segments_started={self.segments_started}, "
            f"segments_completed={self.segments_completed}, "
            f"segments_failed={self.segments_failed}, "
            f"bytes_streamed_to_trickle={self.bytes_streamed_to_trickle}, "
            f"segment_writer_put_timeouts={self.segment_writer_put_timeouts}, "
            f"terminal_failures={self.terminal_failures}, "
            f"encoder_errors={self.encoder_errors}"
            ")"
        )


class MediaPublish:
    def __init__(
        self,
        publish_url: str,
        *,
        config: MediaPublishConfig = MediaPublishConfig(),
    ) -> None:
        self.publish_url = publish_url
        self._publisher = TricklePublisher(
            publish_url,
            config.mime_type,
        )
        self._keyframe_interval_s = float(config.keyframe_interval_s)
        self._fps_hint = config.fps

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._segment_tasks: Set[asyncio.Task[None]] = set()
        self._start_lock = threading.Lock()

        self._closed = False
        self._error: Optional[BaseException] = None
        self._started_at = time.time()
        self._stats: dict[str, int] = {
            "frames_in": 0,
            "frames_dropped_overflow": 0,
            "frames_dropped_debt": 0,
            "frames_dropped_non_monotonic_pts": 0,
            "segments_started": 0,
            "segments_completed": 0,
            "segments_failed": 0,
            "terminal_failures": 0,
            "bytes_streamed_to_trickle": 0,
            "segment_writer_put_timeouts": 0,
            "encoder_errors": 0,
        }
        self._frame_queue = _FrameQueue(maxsize=8, stats=self._stats)

        # Encoder state (owned by the encoder thread).
        self._container: Optional[av.container.OutputContainer] = None
        self._video_stream: Optional[av.video.stream.VideoStream] = None
        self._wallclock_start: Optional[float] = None
        self._last_keyframe_time: Optional[float] = None
        self._last_out_pts: Optional[int] = None

    async def write_frame(self, frame: av.VideoFrame) -> None:
        if self._closed:
            raise LivepeerGatewayError("MediaPublish is closed")
        if not isinstance(frame, av.VideoFrame):
            raise TypeError(f"write_frame expects av.VideoFrame, got {type(frame).__name__}")
        if self._error:
            raise LivepeerGatewayError(f"MediaPublish failed: {self._error}") from self._error

        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        self._ensure_thread()
        self._stats["frames_in"] += 1
        self._frame_queue.put(frame)

    async def _suppress_close_step(self, step_name: str, awaitable: Awaitable[Any]) -> None:
        try:
            await awaitable
        except Exception:
            _LOG.warning("MediaPublish close suppressed %s failure", step_name, exc_info=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Close is best-effort; capture any errors, log them and move on.
        # Intentionally step-wise: each shutdown action has its own
        # suppression so one failure does not prevent later cleanup.
        if self._thread is not None:
            await self._suppress_close_step("sentinel enqueue", asyncio.to_thread(self._frame_queue.put, _STOP))
            await self._suppress_close_step("encoder join", asyncio.to_thread(self._thread.join, 2.0))
            if self._thread.is_alive():
                _LOG.warning("MediaPublish encoder thread still alive after join timeout")

        # Segment tasks may be blocked writing into trickle when the network
        # path is unhealthy; cancel them first so the close stays bounded.
        for task in list(self._segment_tasks):
            task.cancel()
        if self._segment_tasks:
            await self._suppress_close_step(
                "segment task gather",
                asyncio.gather(*list(self._segment_tasks), return_exceptions=True),
            )

        await self._suppress_close_step("publisher close", self._publisher.close())

        if self._error:
            _LOG.warning(
                "MediaPublish close suppressed prior publish failure: %s",
                self._error,
                exc_info=(type(self._error), self._error, self._error.__traceback__),
            )

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run_encoder,
                name="MediaPublishEncoder",
                daemon=True,
            )
            self._thread.start()

    def _run_encoder(self) -> None:
        try:
            while True:
                item = self._frame_queue.get()
                if item is _STOP or self._error is not None:
                    break
                chosen = item
                if self._container is None:
                    self._open_container(chosen)

                encode_started = time.monotonic()
                encoded, encoded_media_time_s = self._encode_frame(chosen)
                encode_duration_s = max(0.0, time.monotonic() - encode_started)
                if encoded:
                    self._frame_queue.update_after_encode(
                        encoded_media_time_s=encoded_media_time_s,
                        encode_duration_s=encode_duration_s,
                    )

            self._flush_encoder()
        except Exception as e:
            self._error = e
            self._stats["encoder_errors"] += 1
            _LOG.error("MediaPublish encoder error", exc_info=True)
        finally:
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    _LOG.exception("MediaPublish failed to close container")
            self._container = None
            self._video_stream = None

    def _open_container(self, first_frame: av.VideoFrame) -> None:
        if self._loop is None:
            raise RuntimeError("MediaPublish loop is not set")

        def custom_io_open(url: str, flags: int, options: dict) -> object:
            read_fd, write_fd = os.pipe()
            read_file = os.fdopen(read_fd, "rb", buffering=0)
            write_file = os.fdopen(write_fd, "wb", buffering=0)
            self._schedule_pipe_reader(read_file)
            return write_file

        segment_options = {
            "segment_time": str(self._keyframe_interval_s),
            "segment_format": "mpegts",
        }

        self._container = av.open(
            "%d.ts",
            format="segment",
            mode="w",
            io_open=custom_io_open,
            options=segment_options,
        )

        video_opts = {
            "bf": "0",
            "preset": "superfast",
            "tune": "zerolatency",
            "forced-idr": "1",
        }
        video_kwargs = {
            "time_base": _OUT_TIME_BASE,
            "width": first_frame.width,
            "height": first_frame.height,
            "pix_fmt": "yuv420p",
        }

        rounded_fps = _normalize_fps(self._fps_hint)
        self._video_stream = self._container.add_stream("libx264", rate=rounded_fps, options=video_opts, **video_kwargs)

    def _encode_frame(self, frame: av.VideoFrame) -> tuple[bool, float]:
        if self._video_stream is None or self._container is None:
            raise RuntimeError("MediaPublish encoder is not initialized")

        source_pts = frame.pts
        source_tb = frame.time_base

        if frame.format.name != "yuv420p":
            frame = frame.reformat(format="yuv420p")

        current_time_s, out_pts = self._compute_pts(source_pts, source_tb)
        if self._last_out_pts is not None and out_pts <= self._last_out_pts:
            # timestamp would overlap with previous frame, so drop
            # happens if frames come in faster than the encode rate
            self._stats["frames_dropped_non_monotonic_pts"] += 1
            return False, current_time_s
        self._last_out_pts = out_pts
        frame.pts = out_pts
        frame.time_base = _OUT_TIME_BASE

        if (
            self._last_keyframe_time is None
            or current_time_s - self._last_keyframe_time >= self._keyframe_interval_s
        ):
            frame.pict_type = PictureType.I
            self._last_keyframe_time = current_time_s
        else:
            frame.pict_type = PictureType.NONE

        packets = self._video_stream.encode(frame)
        for packet in packets:
            self._container.mux(packet)
        return True, current_time_s

    def _flush_encoder(self) -> None:
        if self._video_stream is None or self._container is None:
            return
        packets = self._video_stream.encode(None)
        for packet in packets:
            self._container.mux(packet)

    def _compute_pts(self, pts: Optional[int], time_base: Optional[Fraction]) -> tuple[float, int]:
        if pts is not None and time_base is not None:
            tb = _fraction_from_time_base(time_base)
            current_time_s = float(Fraction(pts) * tb)
            out_pts = _rescale_pts(pts, tb, _OUT_TIME_BASE)
            return current_time_s, out_pts

        now = time.time()
        if self._wallclock_start is None:
            self._wallclock_start = now
        current_time_s = now - self._wallclock_start
        return current_time_s, int(current_time_s * _OUT_TIME_BASE.denominator)

    def _schedule_pipe_reader(self, read_file: BinaryIO) -> None:
        def _start() -> None:
            task = self._loop.create_task(self._stream_pipe_to_trickle(read_file))
            self._segment_tasks.add(task)
            task.add_done_callback(self._segment_tasks.discard)

        self._loop.call_soon_threadsafe(_start)

    async def _stream_pipe_to_trickle(self, read_file: BinaryIO) -> None:
        segment_seq: Optional[int] = None
        try:
            segment = await self._publisher.next()
            segment_seq = segment.seq()
            self._stats["segments_started"] += 1
            async with segment:
                while True:
                    chunk = await asyncio.to_thread(read_file.read, _READ_CHUNK)
                    if not chunk:
                        break
                    self._stats["bytes_streamed_to_trickle"] += len(chunk)
                    # NB: the segment writer has its own chunk timeout, so
                    # lean on that instead of doing that here
                    await segment.write(chunk)
            self._stats["segments_completed"] += 1
        except TricklePublisherTerminalError as e:
            # At this point, publisher.next() has exhausted its retries and the
            # publisher cannot be used for future segments.
            if self._error is None:
                self._error = e
            self._stats["segments_failed"] += 1
            self._stats["terminal_failures"] += 1
            _LOG.error("MediaPublish terminal failure while streaming", exc_info=True)
        except TrickleSegmentWriteError:
            self._stats["segments_failed"] += 1
            _LOG.warning("MediaPublish dropped segment seq=%s", segment_seq, exc_info=True)
        except Exception:
            self._stats["segments_failed"] += 1
            _LOG.exception(
                "MediaPublish unexpected failure while streaming segment seq=%s",
                segment_seq,
            )
        finally:
            # This block is critical for clean-up; always drain and close file
            # Because not all exceptions will be caught (eg, CancelledError)
            try:
                await self._drain_pipe(read_file)
                read_file.close()
            except Exception:
                pass

    async def _drain_pipe(self, read_file: BinaryIO) -> None:
        async def _drain() -> None:
            while True:
                chunk = await asyncio.to_thread(read_file.read, _READ_CHUNK)
                if not chunk:
                    break

        # Best-effort cleanup: absorb TimeoutError and CancelledError
        # so drain never blocks shutdown
        try:
            await asyncio.wait_for(_drain(), timeout=_DRAIN_TIMEOUT_S)
        except BaseException:
            pass

    def get_stats(self) -> MediaPublishStats:
        publisher_stats = self._publisher.get_stats()
        return MediaPublishStats(
            elapsed_s=max(0.0, time.time() - self._started_at),
            frames_in=self._stats["frames_in"],
            frames_dropped_overflow=self._stats["frames_dropped_overflow"],
            frames_dropped_debt=self._stats["frames_dropped_debt"],
            frames_dropped_non_monotonic_pts=self._stats["frames_dropped_non_monotonic_pts"],
            time_debt_s=self._frame_queue.time_debt_s,
            segments_started=self._stats["segments_started"],
            segments_completed=self._stats["segments_completed"],
            segments_failed=self._stats["segments_failed"],
            bytes_streamed_to_trickle=self._stats["bytes_streamed_to_trickle"],
            segment_writer_put_timeouts=max(
                self._stats["segment_writer_put_timeouts"],
                publisher_stats.segment_writer_put_timeouts,
            ),
            terminal_failures=max(
                self._stats["terminal_failures"],
                publisher_stats.terminal_failures,
            ),
            encoder_errors=self._stats["encoder_errors"],
            publisher=publisher_stats,
        )


class _FrameQueue:
    """Queue helper for overflow handling and debt-based frame selection.

    Frames can arrive in bursts even when their timestamps are evenly spaced.
    The queue absorbs those bursts, then keeps output on playback cadence by
    skipping intermediate frames when the encoder is behind, and picking the
    next frame that jumps far enough ahead in time to catch up. When not
    behind, it continues encoding frames in order.

    "Media-time debt" is the running gap between:
    - wall-clock time spent encoding frames, and
    - media-time progress achieved by the frames that were encoded.

    After each successful encode:
    - media_advance_s = encoded_media_time_s - previous_encoded_media_time_s
    - debt = max(0, debt + encode_duration_s - media_advance_s)
    - example: if encode_duration=0.080s and media_advance=0.033s,
      debt increases by 0.047s for that step.

    Intuition:
    - If encoding is slower than media progress, debt grows (we are behind).
    - If media progress catches up relative to encode cost, debt shrinks.
    """

    def __init__(self, *, maxsize: int, stats: dict[str, int]) -> None:
        self._queue: queue.Queue[object] = queue.Queue(maxsize=maxsize)
        self._stats = stats
        self._time_debt_s = 0.0
        self._last_encoded_media_time_s: Optional[float] = None
        self._stop_after_current = False
        self._stop_enqueued = False

    def put(self, item: object) -> None:
        if self._stop_enqueued:
            return
        max_retries = 10
        for _ in range(max_retries):
            try:
                self._queue.put_nowait(item)
                if item is _STOP:
                    self._stop_enqueued = True
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    continue
                self._stats["frames_dropped_overflow"] += 1
        _LOG.error("MediaPublish frame queue put exceeded retry limit (%d); dropping item", max_retries)
        if item is not _STOP:
            self._stats["frames_dropped_overflow"] += 1

    def get(self) -> object:
        if self._stop_after_current:
            self._stop_after_current = False
            return _STOP

        item = self._queue.get()
        if item is _STOP:
            return _STOP

        # Candidate selection is intentionally one-at-a-time:
        # - pop one frame candidate
        # - accept it if it advances enough media time to repay current debt
        # - otherwise skip it and inspect only the next immediately available frame
        # This avoids waiting for future frames and preserves low-latency behavior.
        candidate = item
        while True:
            if self._accept_candidate(candidate):
                return candidate
            try:
                next_item = self._queue.get_nowait()
            except queue.Empty:
                # No immediate replacement candidate; keep pipeline moving.
                return candidate
            if next_item is _STOP:
                # Treat STOP as a normal FIFO sentinel, but do not drop the
                # current candidate mid-selection. Request shutdown right after.
                self._stop_after_current = True
                return candidate
            self._stats["frames_dropped_debt"] += 1
            candidate = next_item

    def update_after_encode(self, *, encoded_media_time_s: float, encode_duration_s: float) -> None:
        if self._last_encoded_media_time_s is None:
            self._last_encoded_media_time_s = encoded_media_time_s
            self._time_debt_s = 0.0
            return

        # Debt tracks wall-clock encode cost relative to media-time progress.
        media_advance_s = max(0.0, encoded_media_time_s - self._last_encoded_media_time_s)
        self._time_debt_s = max(0.0, self._time_debt_s + encode_duration_s - media_advance_s)
        self._last_encoded_media_time_s = encoded_media_time_s

    @property
    def time_debt_s(self) -> float:
        # Metric for "how far behind are we", in seconds.
        return self._time_debt_s

    def _accept_candidate(self, candidate: av.VideoFrame) -> bool:
        candidate_media_time_s = self._frame_media_time_s(candidate)
        if candidate_media_time_s is None or self._last_encoded_media_time_s is None:
            return True
        media_advance_s = candidate_media_time_s - self._last_encoded_media_time_s
        # Candidate must advance enough media time to cover current debt.
        return media_advance_s >= self._time_debt_s

    @staticmethod
    def _frame_media_time_s(frame: av.VideoFrame) -> Optional[float]:
        if frame.pts is None or frame.time_base is None:
            return None
        tb = _fraction_from_time_base(frame.time_base)
        return float(Fraction(frame.pts) * tb)
