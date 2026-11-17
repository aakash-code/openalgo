"""Tick-level capture for NIFTY/BANKNIFTY and their active option contracts.

Opt-in (``TICKS_RECORDER_ENABLED``). Streams every raw tick in Depth mode for
the ATM +/- N strike window of the nearest expiry per underlying (plus the index
spots) and persists it to the Parquet lake in ``database/ticks_store``, for
later tick-level strategy development and backtesting.

Threading (see CLAUDE.md "Nothing may block or be blocked across the eventlet
boundary"):

  * **Producer** -- :meth:`_on_tick`, a ``market_data`` callback. It may run on
    a real OS thread, so it does exactly two things: a lockless ``key in
    frozenset`` membership test and ``put_nowait`` on a REAL
    ``utils.real_threading.Queue``. No lock, no I/O, no logging.
  * **Writer** -- :meth:`_run_writer_loop`, a REAL ``utils.real_threading.Thread``.
    It drains the real queue, builds rows and writes Parquet. Parquet writes are
    blocking file I/O that would stall the eventlet hub on a green thread, so
    this MUST be a real thread, and it must touch no green primitive (it does
    not: the queue is real, the dicts are read atomically, ``ticks_store`` is
    pure file I/O).
  * **Selection** -- :meth:`_refresh_symbols`, an APScheduler job. Under eventlet
    APScheduler workers are GREEN, so it may do the REST ``get_option_chain``
    call and the websocket subscribe, exactly as ``oi_flow_recorder_service``
    does. It never raises (a raising scheduler job stops the beat for the day).

The active symbol set is recomputed every minute, so it follows spot and rolls
at expiry for free -- the same selection path as the OI-flow recorder.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from datetime import date, datetime

from utils import real_threading as _rt
from utils import stream_registry
from utils.logging import get_logger

logger = get_logger(__name__)

# Reuse the OI-flow recorder's proven selection + session helpers verbatim.
from services.oi_flow_recorder_service import (  # noqa: E402
    IST,
    nearest_expiry,
    options_exchange,
    within_session,
)

SUBSCRIBE_MODE_DEFAULT = "Depth"


# ---------------------------------------------------------------------------
# Config accessors (read at call time; clamped)
# ---------------------------------------------------------------------------


def recorder_enabled() -> bool:
    return os.getenv("TICKS_RECORDER_ENABLED", "false").strip().lower() == "true"


def configured_underlyings() -> list[tuple[str, str]]:
    """``TICKS_UNDERLYINGS`` as ``[(underlying, quote_exchange), ...]``."""
    raw = os.getenv("TICKS_UNDERLYINGS", "NIFTY:NSE_INDEX,BANKNIFTY:NSE_INDEX").strip()
    out: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        under, _, exch = part.partition(":")
        under = under.strip().upper()
        exch = exch.strip().upper() or "NSE_INDEX"
        if under:
            out.append((under, exch))
    return out or [("NIFTY", "NSE_INDEX"), ("BANKNIFTY", "NSE_INDEX")]


def _clamped_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def strikes_around_atm() -> int:
    return _clamped_int("TICKS_STRIKES_AROUND_ATM", 15, 1, 50)


def subscribe_mode() -> str:
    return os.getenv("TICKS_MODE", SUBSCRIBE_MODE_DEFAULT).strip() or SUBSCRIBE_MODE_DEFAULT


def flush_seconds() -> float:
    try:
        v = float(os.getenv("TICKS_FLUSH_SECONDS", "") or 5)
    except (TypeError, ValueError):
        v = 5.0
    return v if v > 0 else 5.0


def flush_rows() -> int:
    return _clamped_int("TICKS_FLUSH_ROWS", 50000, 1000, 2_000_000)


def queue_max() -> int:
    return _clamped_int("TICKS_QUEUE_MAX", 200000, 10000, 5_000_000)


def compact_hours() -> list[int]:
    raw = os.getenv("TICKS_COMPACT_HOURS", "16").strip()
    hours = []
    for part in raw.split(","):
        try:
            h = int(part.strip())
        except (TypeError, ValueError):
            continue
        if 0 <= h <= 23:
            hours.append(h)
    return hours or [16]


# How many ticks the writer applies before yielding, so a fast feed cannot
# starve the writer thread on one pass.
_WRITER_BATCH_MAX = 5000
_WRITER_IDLE_SEC = 0.05


class TickCapture:
    """Streams and persists ticks for the configured underlyings. One per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Read locklessly by the producer; a frozenset so swaps are atomic.
        self._active: frozenset[str] = frozenset()
        # key -> underlying, read by the writer thread (atomic dict reads).
        self._underlying_of: dict[str, str] = {}

        self._queue = _rt.Queue(maxsize=queue_max())
        self._dropped = 0
        self._written = 0

        self._ws = None
        self._ws_lock = threading.Lock()
        self._api_key: str | None = None

        self._running = False
        self._writer_thread = None
        self._scheduler = None
        self._lifecycle_lock = threading.Lock()
        self._has_writer_lock = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Claim the writer lock, start the writer thread and the scheduler."""
        from database import ticks_store

        with self._lifecycle_lock:
            if self._running:
                return True
            if not ticks_store.claim_writer():
                logger.warning("tick_capture: another process holds the writer lock; not starting")
                return False
            self._has_writer_lock = True
            self._running = True

            self._writer_thread = _rt.Thread(
                target=self._run_writer_loop, name="tick-capture-writer", daemon=True
            )
            self._writer_thread.start()
            self._start_scheduler()
            logger.info(
                "tick_capture: started (underlyings=%s, ATM+/-%d, mode=%s)",
                ",".join(u for u, _ in configured_underlyings()),
                strikes_around_atm(),
                subscribe_mode(),
            )
        atexit.register(self.stop)
        return True

    def _start_scheduler(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        self._scheduler = BackgroundScheduler(timezone=IST)
        # Recompute the active set every minute during market hours. second=15
        # keeps it off the oi_flow recorder's second=5 beat.
        self._scheduler.add_job(
            self._refresh_symbols,
            trigger=CronTrigger(hour="9-23", minute="*", second=15, timezone=IST),
            id="tick_capture_refresh",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        # Compact the day's part-files after the close.
        self._scheduler.add_job(
            self._compact_today,
            trigger=CronTrigger(
                hour=",".join(str(h) for h in compact_hours()), minute=5, timezone=IST
            ),
            id="tick_capture_compact",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self._scheduler.start()
        # Prime the set at startup so capture begins without waiting a full minute.
        threading.Thread(
            target=self._refresh_symbols, name="tick-capture-prime", daemon=True
        ).start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._running:
                return
            self._running = False
            scheduler, self._scheduler = self._scheduler, None
            writer, self._writer_thread = self._writer_thread, None

        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
        self._unsubscribe_all()
        self._release_ws()
        if writer is not None:
            try:
                _rt.join(writer, timeout=10)
            except Exception:
                pass
        if self._has_writer_lock:
            from database import ticks_store

            ticks_store.release_writer()
            self._has_writer_lock = False
        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

    # -------------------------------------------------------------- producer

    def _on_tick(self, payload: dict) -> None:
        """One websocket tick. May run on a real OS thread -- enqueue only."""
        if not isinstance(payload, dict):
            return
        symbol = payload.get("symbol")
        exchange = payload.get("exchange")
        if not symbol or not exchange:
            return
        key = f"{exchange}:{symbol}"
        if key not in self._active:
            return
        try:
            self._queue.put_nowait((key, symbol, exchange, payload.get("data")))
        except Exception:
            # Full or tearing down. Dropping is correct for market data: the next
            # tick supersedes the one shed. Counter is a lockless diagnostic.
            self._dropped += 1

    # ---------------------------------------------------------------- writer

    def _run_writer_loop(self) -> None:
        """Drain ticks and write Parquet. REAL thread, off the eventlet hub."""
        from database import ticks_store

        buf: list[dict] = []
        last_flush = time.monotonic()
        flush_s = flush_seconds()
        flush_n = flush_rows()

        while self._running:
            drained = 0
            underlying_of = self._underlying_of  # atomic reference read
            today = datetime.now(IST).date()
            while drained < _WRITER_BATCH_MAX:
                try:
                    key, symbol, exchange, data = self._queue.get_nowait()
                except _rt.Empty:
                    break
                except Exception:
                    break
                drained += 1
                if not isinstance(data, dict):
                    continue
                underlying = underlying_of.get(key)
                if not underlying:
                    continue
                try:
                    buf.append(ticks_store.row_from_tick(symbol, exchange, underlying, data, today))
                except Exception:
                    logger.exception("tick_capture: failed to build a row for %s", key)

            now = time.monotonic()
            if buf and (len(buf) >= flush_n or now - last_flush >= flush_s):
                self._written += ticks_store.write_batch(buf)
                buf = []
                last_flush = now

            if stream_registry.should_stop():
                break
            if drained == 0:
                time.sleep(_WRITER_IDLE_SEC)

        # Final flush on shutdown so the last partial batch is not lost.
        if buf:
            try:
                from database import ticks_store as _ts

                _ts.write_batch(buf)
            except Exception:
                logger.exception("tick_capture: final flush failed")

    # ------------------------------------------------------------- selection

    def _refresh_symbols(self) -> None:
        """Recompute the active ATM+/-N set and reconcile subscriptions.

        Runs on a (green, under eventlet) scheduler thread, so REST + subscribe
        are allowed here. Never raises.
        """
        try:
            now = datetime.now(IST)
            new_active: set[str] = set()
            new_under: dict[str, str] = {}

            api_key = self._resolve_api_key()
            if api_key:
                for underlying, quote_exchange in configured_underlyings():
                    if not within_session(now, quote_exchange):
                        continue
                    self._collect_symbols(
                        underlying, quote_exchange, api_key, new_active, new_under
                    )

            with self._lock:
                old_active = self._active
            to_add = [k for k in new_active if k not in old_active]
            to_remove = [k for k in old_active if k not in new_active]

            # Publish the new set BEFORE unsubscribing, so the producer keeps
            # accepting ticks for symbols that remain, and AFTER adding to the
            # map so a tick for a new symbol can be tagged.
            with self._lock:
                self._active = frozenset(new_active)
                self._underlying_of = new_under

            if to_add:
                self._subscribe([self._split(k) for k in to_add])
            if to_remove:
                self._unsubscribe([self._split(k) for k in to_remove])
            if to_add or to_remove:
                logger.info(
                    "tick_capture: active set %d symbols (+%d/-%d), dropped=%d written=%d",
                    len(new_active),
                    len(to_add),
                    len(to_remove),
                    self._dropped,
                    self._written,
                )
        except Exception:
            logger.exception("tick_capture: symbol refresh failed")

    def _collect_symbols(self, underlying, quote_exchange, api_key, out_active, out_under) -> None:
        from services.option_chain_service import get_option_chain

        today = datetime.now(IST).date()
        expiry = nearest_expiry(underlying, quote_exchange, api_key, today)
        if not expiry:
            logger.warning("tick_capture: no expiry for %s this minute", underlying)
            return
        ok, chain, _ = get_option_chain(
            underlying, quote_exchange, expiry, strikes_around_atm(), api_key
        )
        if not ok or not isinstance(chain, dict):
            logger.warning("tick_capture: option chain for %s unavailable this minute", underlying)
            return
        opt_exchange = options_exchange(quote_exchange)
        for row in chain.get("chain") or []:
            for side in ("ce", "pe"):
                leg = row.get(side) or {}
                sym = leg.get("symbol")
                if sym:
                    key = f"{opt_exchange}:{sym}"
                    out_active.add(key)
                    out_under[key] = underlying
        # The index spot itself.
        spot_key = f"{quote_exchange}:{underlying}"
        out_active.add(spot_key)
        out_under[spot_key] = underlying

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        exchange, _, symbol = key.partition(":")
        return symbol, exchange

    # ------------------------------------------------------------ ws plumbing

    def _subscribe(self, pairs: list[tuple[str, str]]) -> None:
        symbols = [{"symbol": s, "exchange": e} for s, e in pairs]
        with self._ws_lock:
            client = self._ensure_ws()
            if client is None:
                logger.warning(
                    "tick_capture: feed unavailable; %d symbols not subscribed", len(symbols)
                )
                return
            try:
                client.subscribe(symbols, mode=subscribe_mode())
            except Exception:
                logger.exception("tick_capture: subscribe raised for %d symbols", len(symbols))

    def _unsubscribe(self, pairs: list[tuple[str, str]]) -> None:
        symbols = [{"symbol": s, "exchange": e} for s, e in pairs]
        with self._ws_lock:
            client = self._ws
            if client is None:
                return
            try:
                client.unsubscribe(symbols, mode=subscribe_mode())
            except Exception:
                logger.debug("tick_capture: unsubscribe failed for %d symbols", len(symbols))

    def _unsubscribe_all(self) -> None:
        with self._lock:
            pairs = [self._split(k) for k in self._active]
            self._active = frozenset()
            self._underlying_of = {}
        if pairs:
            self._unsubscribe(pairs)

    def _ensure_ws(self):
        """The shared websocket client, or None. Caller holds ``_ws_lock``."""
        if self._ws is not None:
            alive = getattr(self._ws, "alive", None)
            if alive is None:
                alive = getattr(self._ws, "connected", True)
            if alive:
                return self._ws
        api_key = self._resolve_api_key()
        if not api_key:
            return None
        try:
            from services.websocket_client import get_websocket_client

            client = get_websocket_client(api_key)
        except Exception as exc:
            logger.debug("tick_capture: websocket not available yet: %s", exc)
            return None
        if client is None:
            return None
        self._ws = client
        callbacks = getattr(client, "callbacks", None) or {}
        if self._on_tick not in callbacks.get("market_data", ()):
            client.register_callback("market_data", self._on_tick)
        if self._on_auth not in callbacks.get("auth", ()):
            client.register_callback("auth", self._on_auth)
        return self._ws

    def _on_auth(self, data: dict) -> None:
        """Re-subscribe the active set after a reconnect."""
        if not isinstance(data, dict) or data.get("status") != "success":
            return
        with self._lock:
            pairs = [self._split(k) for k in self._active]
        if pairs:
            self._subscribe(pairs)

    def _release_ws(self) -> None:
        with self._ws_lock:
            ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            ws.unregister_callback("market_data", self._on_tick)
            ws.unregister_callback("auth", self._on_auth)
        except Exception:
            logger.debug("tick_capture: could not unregister callbacks")

    def _resolve_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key
        try:
            from database.auth_db import get_first_available_api_key

            self._api_key = get_first_available_api_key()
        except Exception:
            logger.exception("tick_capture: could not resolve an API key")
            self._api_key = None
        return self._api_key

    # ------------------------------------------------------------ compaction

    def _compact_today(self) -> None:
        """Scheduler entry: run the heavy DuckDB compaction on a REAL thread.

        Compaction is blocking DuckDB/Parquet work. The scheduler thread is
        green under the default eventlet worker, so running it inline would stall
        the hub for the compaction's duration; a real thread keeps it off the
        hub. One short-lived thread per day, so it does not accumulate.
        """
        _rt.Thread(target=self._run_compaction, name="tick-capture-compact", daemon=True).start()

    def _run_compaction(self) -> None:
        try:
            from database import ticks_store

            today = datetime.now(IST).date().isoformat()
            n = ticks_store.compact_day(today)
            if n:
                logger.info("tick_capture: compacted %d underlying folder(s) for %s", n, today)
        except Exception:
            logger.exception("tick_capture: compaction failed")

    def health(self) -> dict:
        return {
            "running": self._running,
            "active_symbols": len(self._active),
            "queue_depth": self._queue.qsize(),
            "dropped": self._dropped,
            "written": self._written,
        }


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_capture: TickCapture | None = None
_capture_lock = threading.Lock()


def get_tick_capture() -> TickCapture:
    global _capture
    with _capture_lock:
        if _capture is None:
            _capture = TickCapture()
        return _capture


def init_tick_capture() -> None:
    """Start tick capture if ``TICKS_RECORDER_ENABLED`` is set. Idempotent."""
    if not recorder_enabled():
        logger.debug("tick_capture: disabled (TICKS_RECORDER_ENABLED not true)")
        return
    try:
        get_tick_capture().start()
    except Exception:
        logger.exception("tick_capture: failed to start")
