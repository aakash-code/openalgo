# services/orderflow_universe_subscriber.py
"""
Order Flow Universe Subscriber — fully isolated background WS client. Opt-in
only (see ORDERFLOW_UNIVERSE_ENABLED gate in app.py). Same integration
pattern as services/tf_boost_snapshot_service.py: new file, no shared state
with any existing scheduler/service, single guarded registration line in
app.py.

Purpose
-------
websocket_proxy/server.py already tapes every tick it fans out into Redis
(tick_store.record_tick), for whichever symbols someone has subscribed —
today that's "whatever's open on a chart" in openalgo-chart. The order-flow
scanner (openalgo-chart's backend/) needs ticks for its whole scan universe
continuously, independent of any browser tab.

This module is that "someone": it's an ordinary WS client of the proxy
(same {action, symbol, exchange, mode} protocol any browser client uses).
Once subscribed, tick_store already captures everything; openalgo-chart's
backend/ reads those Redis streams directly.

Universe source (2026-07-29 update)
------------------------------------
Was a fixed symbol list. Now tracks TradeFinder's actual Boost/High Powered
lists dynamically — but deliberately conservatively, given this project's
own incident history: services/tf_boost_snapshot_service.py's "9 straight
days empty" investigation and, more seriously, a real ₹9k loss when a
240-symbol/60s REST scan tripped Upstox rate limits and froze the live feed
(see project memory `feed-stall-root-cause`). Guardrails here, specifically
because of that history:
  - Reuses tf_boost_snapshot_service's own 5-minute snapshots (already
    written to db/tf_boost_snapshots.duckdb) rather than hitting TradeFinder
    again — zero additional TF API load.
  - Refreshes on that same 5-minute cadence, never faster — there's no
    fresher data to react to in between anyway.
  - Hard-capped universe size (ORDERFLOW_MAX_UNIVERSE, default 30, never
    exceeds ORDERFLOW_MAX_UNIVERSE_HARD_LIMIT=50 regardless of config).
  - Diffs old vs. new target set and only subscribes additions /
    unsubscribes removals — not a full resubscribe every cycle.
  - Falls back to the static ORDERFLOW_UNIVERSE_SYMBOLS list (still
    supported) if the dynamic fetch fails or the snapshot DB is empty
    (e.g., market not open yet, or the snapshot recorder hasn't ticked once
    since startup) — always has *something* to track, never silently goes
    empty.
"""

from __future__ import annotations

import json
import os
import threading
import time

from utils.logging import get_logger

logger = get_logger(__name__)

WS_URL = os.getenv("ORDERFLOW_WS_URL", "ws://127.0.0.1:8765")
# "EXCHANGE:SYMBOL,EXCHANGE:SYMBOL,..." — fallback universe, used when the
# dynamic TF-Boost-derived universe isn't available yet.
STATIC_FALLBACK_UNIVERSE = os.getenv("ORDERFLOW_UNIVERSE_SYMBOLS", "")
# TF's own symbols are all NSE equities (confirmed: tf_boost_snapshots has
# no exchange column because there's never been anything but NSE in it).
TF_EXCHANGE = "NSE"
MAX_UNIVERSE = min(int(os.getenv("ORDERFLOW_MAX_UNIVERSE", "30")), 50)
DYNAMIC_REFRESH_INTERVAL_S = 300  # matches tf_boost_snapshot_service's own cadence
REDIS_UNIVERSE_KEY = "orderflow:universe"  # openalgo-chart's backend reads this
# mode 2 (Quote), not 3: confirmed live 2026-07-29 that on this broker
# (Upstox), mode 3 ("full tick") returns LTP + 5-level depth but not volume/
# last-trade-qty, contrary to the generic "3=full tick with trade direction"
# assumption in this project's docs — mode 2 is what actually carries
# volume/last_trade_quantity/total_buy_quantity/total_sell_quantity, which
# is what Phase 1's OHLCV candle engine needs (see backend/app/tick_engine).
# Tried subscribing both modes for the same symbol: both register
# successfully at the OpenAlgo layer, but empirically (300+ live samples)
# only mode 3 payloads ever actually arrive once both are subscribed for the
# same instrument — likely an artifact of upstox_adapter.py coalescing
# modes 2 and 3 into one upstream "full" subscription. Not chased further
# here (would need changes in broker/upstox/streaming/upstox_adapter.py,
# out of scope for this isolated module). Depth/DOM (mode 3) is left for a
# later phase to request per-symbol on demand rather than for the whole
# background universe.
MODES = (2,)

_SUBSCRIBE_PACE_PER_BATCH = 10
_SUBSCRIBE_PACE_DELAY_S = 0.08  # 10 frames / 80ms, matches the proxy's own pacing note
_RECONNECT_DELAY_S = 5

_thread: threading.Thread | None = None
_lock = threading.Lock()
_stop = threading.Event()


def _parse_static_universe() -> list[tuple[str, str]]:
    """Returns [(exchange, symbol), ...]. Skips malformed entries rather than
    failing the whole universe over one typo."""
    pairs = []
    for raw in STATIC_FALLBACK_UNIVERSE.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            logger.warning(f"orderflow_universe: skipping malformed entry (want EXCHANGE:SYMBOL): {raw}")
            continue
        exchange, symbol = raw.split(":", 1)
        pairs.append((exchange.strip(), symbol.strip()))
    return pairs


def _fetch_dynamic_universe(max_symbols: int) -> list[tuple[str, str]]:
    """Top symbols from TradeFinder's latest intraday_boost +
    high_powered_stocks snapshots, interleaved by rank so both lists are
    represented rather than one dominating, deduped, capped. Returns []
    (never raises) on any failure — caller falls back to the static list."""
    try:
        from database.tf_boost_db import get_connection

        symbols: list[str] = []
        seen: set[str] = set()
        per_list = []
        with get_connection() as conn:
            for list_type in ("intraday_boost", "high_powered_stocks"):
                rows = conn.execute(
                    """
                    SELECT symbol FROM tf_boost_snapshots
                    WHERE list_type = ?
                      AND snapshot_time = (
                          SELECT MAX(snapshot_time) FROM tf_boost_snapshots WHERE list_type = ?
                      )
                    ORDER BY score DESC
                    """,
                    [list_type, list_type],
                ).fetchall()
                per_list.append([r[0] for r in rows])

        # Interleave (one from each list per round) so a strong showing in
        # either list gets represented, not just whichever list is longer.
        for i in range(max(len(lst) for lst in per_list) if per_list else 0):
            for lst in per_list:
                if i < len(lst) and lst[i] not in seen:
                    seen.add(lst[i])
                    symbols.append(lst[i])
                    if len(symbols) >= max_symbols:
                        break
            if len(symbols) >= max_symbols:
                break

        return [(TF_EXCHANGE, sym) for sym in symbols]
    except Exception as e:
        logger.debug(f"orderflow_universe: dynamic universe fetch failed, will use fallback: {e}")
        return []


def _current_target_universe() -> list[tuple[str, str]]:
    dynamic = _fetch_dynamic_universe(MAX_UNIVERSE)
    if dynamic:
        return dynamic
    static = _parse_static_universe()
    if static:
        logger.debug("orderflow_universe: using static fallback universe (dynamic unavailable)")
    return static


def _publish_universe(universe: list[tuple[str, str]]) -> None:
    """Best-effort — openalgo-chart's backend falls back to its own static
    config if this key is missing, so a Redis hiccup here is never fatal."""
    try:
        import redis

        client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_connect_timeout=2, socket_timeout=2, decode_responses=True,
        )
        client.set(REDIS_UNIVERSE_KEY, ",".join(f"{ex}:{sym}" for ex, sym in universe))
    except Exception as e:
        logger.debug(f"orderflow_universe: failed to publish universe to Redis: {e}")


def _send_subscribe_batch(ws, frames: list[tuple[str, str, int, str]]) -> None:
    """frames: [(exchange, symbol, mode, action), ...]. Paced regardless of
    subscribe vs. unsubscribe — churn safety applies to both directions."""
    for i, (exchange, symbol, mode, action) in enumerate(frames):
        if _stop.is_set():
            break
        ws.send(json.dumps({"action": action, "symbol": symbol, "exchange": exchange, "mode": mode}))
        if (i + 1) % _SUBSCRIBE_PACE_PER_BATCH == 0:
            time.sleep(_SUBSCRIBE_PACE_DELAY_S)


def _run_forever():
    import websocket  # websocket-client, already a project dependency

    while not _stop.is_set():
        try:
            # Resolved fresh on every (re)connect, same reasoning as
            # tradefinder_service._get_tf_jwt(): a broker re-login takes
            # effect on the very next reconnect, no cache/restart needed.
            from database.auth_db import get_first_available_api_key

            api_key = get_first_available_api_key()
            if not api_key:
                logger.warning("orderflow_universe: no active broker session with an API key yet, retrying")
                time.sleep(_RECONNECT_DELAY_S)
                continue

            ws = websocket.create_connection(WS_URL, timeout=10)
            ws.send(json.dumps({"action": "authenticate", "api_key": api_key}))
            auth_reply = json.loads(ws.recv())
            if auth_reply.get("type") == "error" or auth_reply.get("status") == "error":
                logger.warning(f"orderflow_universe: authentication failed: {auth_reply}")
                ws.close()
                time.sleep(_RECONNECT_DELAY_S)
                continue

            current = set(_current_target_universe())
            if not current:
                logger.warning("orderflow_universe: no universe available (dynamic and static both empty)")
                ws.close()
                time.sleep(_RECONNECT_DELAY_S)
                continue

            logger.info(f"orderflow_universe: authenticated, subscribing {len(current)} symbols")
            _send_subscribe_batch(
                ws, [(ex, sym, mode, "subscribe") for ex, sym in current for mode in MODES],
            )
            _publish_universe(sorted(current))
            logger.info(f"orderflow_universe: subscribed {len(current)} symbols, tick_store now taping them")

            ws.settimeout(30)
            last_refresh = time.time()
            while not _stop.is_set():
                try:
                    ws.recv()
                except websocket.WebSocketTimeoutException:
                    pass

                if time.time() - last_refresh >= DYNAMIC_REFRESH_INTERVAL_S:
                    last_refresh = time.time()
                    target = set(_current_target_universe())
                    if not target:
                        continue  # keep the existing subscriptions rather than dropping everything
                    added = target - current
                    removed = current - target
                    if added or removed:
                        logger.info(
                            f"orderflow_universe: refresh — +{len(added)} -{len(removed)} "
                            f"(now {len(target)} symbols)"
                        )
                        _send_subscribe_batch(
                            ws, [(ex, sym, mode, "unsubscribe") for ex, sym in removed for mode in MODES],
                        )
                        _send_subscribe_batch(
                            ws, [(ex, sym, mode, "subscribe") for ex, sym in added for mode in MODES],
                        )
                        current = target
                        _publish_universe(sorted(current))
        except Exception as e:
            logger.warning(f"orderflow_universe: connection error, retrying in {_RECONNECT_DELAY_S}s: {e}")
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if not _stop.is_set():
            time.sleep(_RECONNECT_DELAY_S)


def init_orderflow_universe_subscriber():
    """Idempotent init: starts one daemon thread. Safe to call once at app
    startup; opt-in only."""
    global _thread
    with _lock:
        if _thread is not None:
            logger.debug("orderflow_universe: already initialized, skipping")
            return
        _stop.clear()
        _thread = threading.Thread(target=_run_forever, name="orderflow-universe-subscriber", daemon=True)
        _thread.start()
        logger.info("Order Flow universe subscriber started (isolated daemon thread)")
