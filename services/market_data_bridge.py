"""
MarketDataService ZMQ bridge — tick feed for the Flask process.

Since the websocket proxy runs as its own process (GIL isolation from
pandas-heavy request threads), its zmq_listener feeds the MarketDataService
instance in the PROXY process only. Consumers inside the Flask process —
the sandbox/analyzer execution engine (paper trading), position MTM — would
otherwise silently fall back to slow REST polling.

This bridge gives the Flask process its own tick stream: it binds a SUB on
ZMQ_MDS_PORT (default 5557) and every broker-adapter publisher additionally
connects its PUB there (see websocket_proxy/base_adapter.py:
get_mds_bridge_endpoint). Topic parsing mirrors
websocket_proxy/server.py:zmq_listener so both processes interpret the bus
identically.

Started from websocket_proxy/app_integration.py only when the proxy runs as
a subprocess — in legacy in-process mode the proxy's own zmq_listener already
feeds this process's MDS, and running both would double-deliver every tick
(duplicate sandbox executions).
"""
import json
import os
import threading

import zmq

from utils.logging import get_logger
from websocket_proxy.base_adapter import get_mds_bridge_endpoint
from websocket_proxy.mode_utils import normalize_mode_or_none

logger = get_logger(__name__)

_bridge_thread: threading.Thread | None = None
_started = False
_lock = threading.Lock()

# Two-segment exchange prefixes — keep in sync with server.py zmq_listener.
_MULTI_SEGMENT_EXCHANGE_PREFIXES = (
    ("NSE", "INDEX"),
    ("BSE", "INDEX"),
    ("MCX", "INDEX"),
    ("GLOBAL", "INDEX"),
)


def _run_bridge(endpoint: str) -> None:
    from services.market_data_service import get_market_data_service

    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVHWM, 10000)
    try:
        sub.bind(endpoint)
    except zmq.ZMQError as e:
        logger.error(f"MDS bridge could not bind {endpoint}: {e} — sandbox will not get ticks")
        return

    logger.info(f"MDS bridge listening on {endpoint}")
    mds = get_market_data_service()

    while True:
        try:
            topic, payload = sub.recv_multipart()
            topic_str = topic.decode("utf-8")

            if topic_str.startswith("CACHE_INVALIDATE") or topic_str.endswith(
                ("_orders", "_positions", "_margins")
            ):
                continue

            parts = topic_str.split("_")
            if len(parts) < 3:
                continue
            mode_norm = normalize_mode_or_none(parts[-1])
            if mode_norm is None:
                continue
            mode, _ = mode_norm

            remaining = parts[:-1]
            if len(remaining) >= 2 and (remaining[0], remaining[1]) in _MULTI_SEGMENT_EXCHANGE_PREFIXES:
                exchange = f"{remaining[0]}_{remaining[1]}"
                symbol = "_".join(remaining[2:])
            else:
                exchange = remaining[0]
                symbol = "_".join(remaining[1:])
            if not symbol:
                continue

            mds.process_market_data(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "mode": mode,
                    "data": json.loads(payload.decode("utf-8")),
                }
            )
        except Exception as e:
            # Never let one bad message kill the sandbox tick feed.
            logger.debug(f"MDS bridge message error: {e}")


def start_market_data_bridge() -> bool:
    """Start the bridge thread (idempotent). Returns True if running."""
    global _bridge_thread, _started
    endpoint = get_mds_bridge_endpoint()
    if not endpoint:
        logger.info("MDS bridge disabled via ZMQ_MDS_PORT")
        return False

    with _lock:
        if _started:
            return True
        _bridge_thread = threading.Thread(
            target=_run_bridge, args=(endpoint,), daemon=True, name="mds-zmq-bridge"
        )
        _bridge_thread.start()
        _started = True
    return True
