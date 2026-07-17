"""Optional Redis-backed tick tape recorder.

Purpose
-------
The WebSocket proxy fans out live ticks to clients but keeps no history: after a
feed stall the browser cannot reconstruct the ticks it missed (footprint / tape
data), because the client-side buffer is memory-only and lost on reconnect.

This module records recent ticks into per-symbol capped Redis streams so a
client can replay the gap after recovery. It is an *add-on tape recorder*, never
on the hot path for live fan-out:

* Writes are non-blocking. ``record()`` drops the tick onto a bounded in-memory
  queue and returns immediately; a background daemon thread drains the queue and
  performs the actual ``XADD``. The async ZMQ listener therefore never blocks on
  Redis, and if Redis stalls the queue simply overflows and drops ticks — the
  live feed is completely unaffected.
* It degrades to a no-op if Redis is unreachable or the ``redis`` package is not
  installed. Live fan-out (ZMQ) is untouched in every failure mode.

Enable/disable and connection are controlled by env:
  REDIS_TICK_STORE   "true"/"false"  (default "true")
  REDIS_URL          redis://host:port/db  (default redis://127.0.0.1:6379/0)
  REDIS_TICK_MAXLEN  approx ticks kept per symbol stream (default 5000)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time

from utils.logging import get_logger

logger = get_logger(__name__)

_MAXLEN = int(os.getenv("REDIS_TICK_MAXLEN", "5000"))
_QUEUE_MAXSIZE = 20000  # bounded — overflow drops ticks rather than blocking
_KEY_PREFIX = "ticks:"


def _enabled() -> bool:
    return os.getenv("REDIS_TICK_STORE", "true").strip().lower() in ("1", "true", "yes", "on")


def _stream_key(exchange: str, symbol: str) -> str:
    return f"{_KEY_PREFIX}{exchange}:{symbol}"


class _TickStore:
    def __init__(self) -> None:
        self._client = None
        self._queue: "queue.Queue[tuple[str, float, str]]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._writer: threading.Thread | None = None
        self._connect_failed_logged = False
        self._overflow_logged = False
        self._lock = threading.Lock()
        self._started = False
        # Throttle reconnect attempts so a down Redis doesn't trigger a 2s-timeout
        # ping on every dequeued tick (which would spin the writer thread).
        self._next_connect_attempt = 0.0
        self._reconnect_backoff = 30.0

    # -- connection -------------------------------------------------------
    def _get_client(self):
        """Lazily create a Redis client. Returns None (and logs once) if Redis
        is unavailable so callers degrade to a no-op. Reconnect attempts are
        throttled to at most once per _reconnect_backoff seconds."""
        if self._client is not None:
            return self._client
        if time.time() < self._next_connect_attempt:
            return None
        self._next_connect_attempt = time.time() + self._reconnect_backoff
        try:
            import redis  # imported lazily so a missing package is non-fatal

            url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
            # Short timeouts: a Redis hiccup must never wedge the writer thread.
            client = redis.Redis.from_url(
                url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            client.ping()
            self._client = client
            logger.info(f"Tick store connected to Redis at {url}")
            return client
        except Exception as e:
            if not self._connect_failed_logged:
                self._connect_failed_logged = True
                logger.warning(
                    f"Tick store: Redis unavailable ({e}) — tick replay disabled, "
                    f"live feed unaffected."
                )
            return None

    def _ensure_writer(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._writer = threading.Thread(
                target=self._drain_loop, name="tick-store-writer", daemon=True
            )
            self._writer.start()
            self._started = True

    # -- write path -------------------------------------------------------
    def record(self, exchange: str, symbol: str, mode: int, data: dict) -> None:
        """Non-blocking: enqueue a tick for background persistence. Never raises,
        never blocks the caller (the async fan-out loop)."""
        if not _enabled():
            return
        self._ensure_writer()
        try:
            ts_ms = time.time() * 1000.0
            payload = json.dumps({"mode": mode, "ts": ts_ms, "data": data})
            self._queue.put_nowait((_stream_key(exchange, symbol), ts_ms, payload))
        except queue.Full:
            if not self._overflow_logged:
                self._overflow_logged = True
                logger.warning("Tick store queue full — dropping ticks (Redis slow/down?)")
        except Exception:
            pass  # recording must never disturb the live feed

    def _drain_loop(self) -> None:
        while True:
            try:
                key, ts_ms, payload = self._queue.get()
            except Exception:
                continue
            client = self._get_client()
            if client is None:
                # Redis down — drain and discard so the queue never wedges.
                continue
            try:
                # Approximate MAXLEN trim (~) is far cheaper than exact.
                client.xadd(
                    key,
                    {"p": payload},
                    maxlen=_MAXLEN,
                    approximate=True,
                )
                self._overflow_logged = False
            except Exception as e:
                # Drop this tick; surface once per failure episode.
                if not self._connect_failed_logged:
                    self._connect_failed_logged = True
                    logger.warning(f"Tick store XADD failed ({e}) — dropping tick")
                self._client = None  # force reconnect on next tick

    # -- read path (used by the Flask replay endpoint) --------------------
    def replay(self, exchange: str, symbol: str, since_ms: float, limit: int = 5000) -> list[dict]:
        """Return ticks recorded for symbol at or after ``since_ms`` (epoch ms).
        Synchronous — intended to be called from the Flask request thread, not
        the async fan-out loop. Returns [] if Redis is unavailable."""
        client = self._get_client()
        if client is None:
            return []
        try:
            # Redis stream IDs are "<ms>-<seq>"; start exclusive of since_ms.
            start_id = f"({int(since_ms)}-0" if since_ms and since_ms > 0 else "-"
            entries = client.xrange(_stream_key(exchange, symbol), min=start_id, count=limit)
            out: list[dict] = []
            for _entry_id, fields in entries:
                raw = fields.get("p")
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except (TypeError, ValueError):
                    continue
            return out
        except Exception as e:
            logger.warning(f"Tick store replay failed for {exchange}:{symbol}: {e}")
            return []


# Module-level singleton shared within a process.
_store = _TickStore()


def record_tick(exchange: str, symbol: str, mode: int, data: dict) -> None:
    _store.record(exchange, symbol, mode, data)


def replay_ticks(exchange: str, symbol: str, since_ms: float, limit: int = 5000) -> list[dict]:
    return _store.replay(exchange, symbol, since_ms, limit)
