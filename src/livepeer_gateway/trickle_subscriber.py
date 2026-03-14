from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .segment_reader import SegmentReader


_LOG = logging.getLogger(__name__)


class TrickleSubscriber:
    """
    Trickle subscriber that streams bytes from a sequence of HTTP GET endpoints:
      - Read segment: GET {base_url}/{seq}

    Note: the runtime (lock/session) is created lazily to allow construction
    in sync code without opening sockets.

    max_bytes (if set) limits the total bytes read per segment, not the entire subscription.
    """

    def __init__(
        self,
        url: str,
        *,
        start_seq: int = -2,
        max_retries: int = 5,
        connection_close: bool = False,
        max_bytes: Optional[int] = None,
    ):
        if max_bytes is not None and max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self.base_url = url.rstrip("/")
        self._seq = start_seq
        self._max_retries = max_retries
        self._connection_close = connection_close
        self._max_bytes = max_bytes

        self._pending_get: Optional[aiohttp.ClientResponse] = None
        self._lock: Optional[asyncio.Lock] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._errored = False

    async def __aenter__(self) -> "TrickleSubscriber":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    async def _ensure_runtime(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)

    def _segment_url(self, seq: int) -> str:
        return f"{self.base_url}/{seq}"

    async def _preconnect(self) -> Optional[aiohttp.ClientResponse]:
        """
        Preconnect to the server by making a GET request to fetch the next segment.

        For non-200 responses, retries up to max_retries unless a 404 is encountered.
        """
        await self._ensure_runtime()
        assert self._session is not None

        seq = self._seq
        url = self._segment_url(seq)
        headers = {"Connection": "close"} if self._connection_close else None

        for attempt in range(0, self._max_retries):
            _LOG.debug("Trickle sub preconnect attempt=%s url=%s", attempt, url)
            try:
                resp = await self._session.get(url, headers=headers)

                if resp.status == 200:
                    # Return the response for later processing
                    return resp

                if resp.status == 404:
                    _LOG.debug("Trickle sub got 404, terminating %s", url)
                    resp.release()
                    self._errored = True
                    return None

                if resp.status == 470:
                    # Channel exists but no data at this index, so reset.
                    latest = resp.headers.get("Lp-Trickle-Latest") or "-1"
                    try:
                        seq = int(latest)
                    except ValueError:
                        seq = -1
                    self._seq = seq
                    url = self._segment_url(seq)
                    _LOG.debug("Trickle sub resetting index to leading edge %s", url)
                    resp.release()
                    continue

                body = await resp.text()
                resp.release()
                _LOG.error("Trickle sub failed GET %s status=%s msg=%s", url, resp.status, body)

            except Exception:
                _LOG.exception("Trickle sub failed to complete GET %s", url)

            if attempt < self._max_retries - 1:
                await asyncio.sleep(0.5)

        _LOG.error("Trickle sub hit max retries, exiting %s", url)
        self._errored = True
        return None

    async def next(self) -> Optional["SegmentReader"]:
        """Retrieve data from the current segment and set up the next segment concurrently."""
        await self._ensure_runtime()
        assert self._lock is not None

        async with self._lock:
            # We intentionally serialize preconnect/next under one lock to avoid
            # overlapping fetches that could race and stomp segment ordering.
            if self._errored:
                _LOG.debug("Trickle subscription closed or errored for %s", self.base_url)
                return None

            # If we don't have a pending GET request, preconnect
            if self._pending_get is None:
                _LOG.debug("Trickle sub no pending connection, preconnecting...")
                self._pending_get = await self._preconnect()

            # Extract the current connection to use for reading
            resp = self._pending_get
            self._pending_get = None

            # Preconnect has failed, notify caller
            if resp is None:
                return None

            # Extract and set the next index from the response headers
            segment = SegmentReader(resp, max_bytes=self._max_bytes)

            if segment.eos():
                await segment.close()
                return None

            seq = segment.seq()
            if seq >= 0:
                self._seq = seq + 1

            # Set up the next connection in the background
            asyncio.create_task(self._preconnect_next_segment())

        return segment

    async def _preconnect_next_segment(self) -> None:
        """Preconnect to the next segment in the background."""
        await self._ensure_runtime()
        assert self._lock is not None

        async with self._lock:
            if self._pending_get is not None:
                return
            next_conn = await self._preconnect()
            if next_conn:
                self._pending_get = next_conn

    async def close(self) -> None:
        """Close the session when done."""
        if self._session is None and self._lock is None and self._pending_get is None:
            return

        await self._ensure_runtime()
        assert self._lock is not None

        _LOG.debug("Trickle sub closing %s", self.base_url)
        async with self._lock:
            self._errored = True
            if self._pending_get:
                self._pending_get.close()
                self._pending_get = None
            if self._session:
                try:
                    await self._session.close()
                except Exception:
                    _LOG.error("Error closing trickle subscriber", exc_info=True)
                finally:
                    self._session = None


