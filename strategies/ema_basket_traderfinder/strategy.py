"""
EMA-Crossover Basket on a TraderFinder Sector-Scope universe
============================================================

A *self-contained* dual-mode strategy for OpenAlgo:

    python strategy.py --mode backtest      # VectorBT simulation over the basket
    python strategy.py --mode live          # OpenAlgo live execution (sandbox/real per UI toggle)

Design
------
* Universe is NOT hardcoded. It is pulled from your TraderFinder "Sector Scope"
  filter via `fetch_traderfinder_universe()` (an adapter you wire to your real
  API/scrape - see the TODO block). A static fallback keeps it runnable today.
* Signal: fast EMA crosses slow EMA (long-only basket, one position per stock).
* Execution: end-of-candle (eoc) MARKET orders, evaluated on bar close.
* Risk: an **ATR trailing stop** recomputed every bar (volatility-adaptive),
  plus an initial ATR hard stop, applied per symbol via the live LTP feed.
* Capital: shared across the basket with a per-symbol cap and a max-concurrent
  positions cap so the whole basket can't over-deploy.

This file is single-file by design (no `core/` imports) so it is upload-ready
for OpenAlgo's `/python` self-hosted host. It reads env vars in the canonical
priority, traps SIGTERM, and logs to stdout only.
"""
import argparse
import logging
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import find_dotenv, load_dotenv

from openalgo import api, ta

# ============================================================================
# CONFIG - edit for your basket / timeframe / risk
# ============================================================================

STRATEGY_NAME = os.getenv("STRATEGY_NAME", "ema_basket_traderfinder")

EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
INTERVAL = os.getenv("INTERVAL", "15m")        # 1m,3m,5m,10m,15m,30m,1h,D
PRODUCT  = os.getenv("PRODUCT", "MIS")          # MIS intraday / CNC delivery
LOT_SIZE = 1                                    # 1 for equities

# --- Indicator params ---
FAST_EMA = int(os.getenv("FAST_EMA", "9"))
SLOW_EMA = int(os.getenv("SLOW_EMA", "21"))

# --- ATR trailing-stop params ---
ATR_PERIOD     = int(os.getenv("ATR_PERIOD", "14"))
ATR_SL_MULT    = float(os.getenv("ATR_SL_MULT", "2.0"))    # initial hard stop = entry - 2*ATR
ATR_TRAIL_MULT = float(os.getenv("ATR_TRAIL_MULT", "2.5")) # trail distance = 2.5*ATR from peak
TIME_EXIT_MIN  = int(os.getenv("TIME_EXIT_MIN", "0"))      # 0 = disabled

# --- Capital / risk caps (shared across the basket) ---
RISK_PER_TRADE        = float(os.getenv("RISK_PER_TRADE", "0.005"))   # 0.5% of cash risked per trade
PER_SYMBOL_CAP_PCT    = float(os.getenv("PER_SYMBOL_CAP_PCT", "0.18"))# <=18% of cash per stock
MAX_OPEN_POSITIONS    = int(os.getenv("MAX_OPEN_POSITIONS", "5"))     # max concurrent stocks

# --- Loop cadence ---
POLL_INTERVAL_SEC     = int(os.getenv("POLL_INTERVAL_SEC", "20"))     # how often to refetch bars
UNIVERSE_REFRESH_SEC  = int(os.getenv("UNIVERSE_REFRESH_SEC", "900")) # re-pull TraderFinder every 15m

# --- Backtest ---
INIT_CASH     = float(os.getenv("INIT_CASH", "1000000"))   # Rs 10 lakh per symbol leg
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "365"))
DATA_SOURCE   = os.getenv("DATA_SOURCE", "api")            # "api" | "db"

# ============================================================================
# TradeFinder Sector-Scope universe
# ============================================================================
# Reuses the project's existing, authenticated fetcher:
#     strategies/scan_today_signals.py :: fetch_sector_scope()
# which handles JWT + TOTP auth against tradefinder.in/api_be and returns
#     {SECTOR: {symbol: {"ltp", "change_pct", "r_factor"}}}
#
# Optional filters (default = take every symbol, matching the base scanner):
#   TF_MIN_R_FACTOR  keep only stocks with r_factor >= this (e.g. "0" longs only)
#   TF_SECTORS       comma list to whitelist sectors (empty = all sectors)
#   TF_TOP_N         keep only the top N by r_factor (0 = no cap)
#
# Falls back to TF_SYMBOLS static list if the fetcher is unavailable
# (missing JWT, no network, or running where the package isn't importable) so
# backtests and live trading are never hard-blocked by a screener outage.
# ----------------------------------------------------------------------------

TF_MIN_R_FACTOR = float(os.getenv("TF_MIN_R_FACTOR", "0"))      # 0 = strong/long-biased only; set very low to disable
TF_SECTORS      = {s.strip().upper() for s in os.getenv("TF_SECTORS", "").split(",") if s.strip()}
TF_TOP_N        = int(os.getenv("TF_TOP_N", "50"))             # 50 strongest by r_factor; 0 = no cap

TRADERFINDER_FALLBACK = [
    s.strip().upper()
    for s in os.getenv("TF_SYMBOLS", "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK").split(",")
    if s.strip()
]


def _import_tf_base():
    """Import the existing scan_today_signals module by putting the repo root
    (the dir containing the `strategies/` package) on sys.path."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "strategies" / "scan_today_signals.py").exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            break
    import strategies.scan_today_signals as base  # noqa: E402
    return base


def fetch_traderfinder_universe():
    """Return a list of OpenAlgo equity symbols from TradeFinder Sector Scope."""
    try:
        base = _import_tf_base()
        sector_data = base.fetch_sector_scope()
    except Exception:
        log.exception("Sector Scope unavailable - using fallback universe")
        return list(TRADERFINDER_FALLBACK)

    best_rf = {}   # symbol -> max r_factor seen
    for sector, stocks in (sector_data or {}).items():
        if TF_SECTORS and str(sector).upper() not in TF_SECTORS:
            continue
        if not isinstance(stocks, dict):
            continue
        for sym, info in stocks.items():
            try:
                rf = float(info.get("r_factor", 0))
            except (TypeError, ValueError, AttributeError):
                rf = 0.0
            if rf < TF_MIN_R_FACTOR:
                continue
            s = str(sym).strip().upper()
            if s:
                best_rf[s] = max(best_rf.get(s, rf), rf)

    if not best_rf:
        log.warning("Sector Scope returned 0 symbols after filters - using fallback")
        return list(TRADERFINDER_FALLBACK)

    symbols = sorted(best_rf, key=lambda s: best_rf[s], reverse=True)
    if TF_TOP_N > 0:
        symbols = symbols[:TF_TOP_N]
    log.info("Sector Scope universe: %d symbols (min_r=%s sectors=%s top_n=%s)",
             len(symbols),
             "none" if TF_MIN_R_FACTOR <= -1e8 else TF_MIN_R_FACTOR,
             ",".join(sorted(TF_SECTORS)) or "ALL",
             TF_TOP_N or "ALL")
    return symbols


# ============================================================================
# Logging - stdout only (OpenAlgo /python host captures it)
# ============================================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(STRATEGY_NAME)

# ============================================================================
# Env resolution - HOST_SERVER wins over OPENALGO_HOST per OpenAlgo convention
# ============================================================================

load_dotenv(find_dotenv(usecwd=True))

API_KEY  = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL   = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}"
)

stop_event = threading.Event()


# ============================================================================
# SIGNALS + ATR - the only strategy logic. Shared by backtest and live.
# ============================================================================

def _normalize(df):
    if df is None or len(df) == 0:
        return df
    df = df.copy()
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    else:
        df.index = pd.to_datetime(df.index)
    df.columns = [c.lower() for c in df.columns]
    return df.sort_index()


def signals(df):
    """EMA crossover. Returns (entries, exits) as clean boolean Series."""
    close = df["close"]
    fast = pd.Series(ta.ema(close, FAST_EMA), index=close.index)
    slow = pd.Series(ta.ema(close, SLOW_EMA), index=close.index)

    buy_raw  = pd.Series(ta.crossover(fast, slow), index=close.index).fillna(False).astype(bool)
    sell_raw = pd.Series(ta.crossunder(fast, slow), index=close.index).fillna(False).astype(bool)

    entries = pd.Series(ta.exrem(buy_raw, sell_raw), index=close.index).fillna(False).astype(bool)
    exits   = pd.Series(ta.exrem(sell_raw, buy_raw), index=close.index).fillna(False).astype(bool)
    return entries, exits


def atr_series(df, period=ATR_PERIOD):
    return pd.Series(ta.atr(df["high"], df["low"], df["close"], period), index=df.index)


# ============================================================================
# BACKTEST (VectorBT) - per-symbol legs over the basket, aggregated report
# ============================================================================

def run_backtest():
    import vectorbt as vbt

    client = api(api_key=API_KEY, host=API_HOST)
    universe = fetch_traderfinder_universe()

    log.info("=" * 72)
    log.info("BACKTEST basket: %s | %d symbols | %s @ %s",
             STRATEGY_NAME, len(universe), EXCHANGE, INTERVAL)
    log.info("EMA %d/%d | ATR trail %.1fx (period %d) | per-symbol cap %.0f%%",
             FAST_EMA, SLOW_EMA, ATR_TRAIL_MULT, ATR_PERIOD, PER_SYMBOL_CAP_PCT * 100)
    log.info("=" * 72)

    end = datetime.now().date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    freq = _vbt_freq(INTERVAL)

    summary = []
    for sym in universe:
        try:
            raw = client.history(symbol=sym, exchange=EXCHANGE, interval=INTERVAL,
                                 start_date=start.strftime("%Y-%m-%d"),
                                 end_date=end.strftime("%Y-%m-%d"), source=DATA_SOURCE)
            df = _normalize(raw)
            if df is None or len(df) < max(SLOW_EMA, ATR_PERIOD) + 5:
                log.warning("%s: insufficient bars - skipping", sym)
                continue

            entries, exits = signals(df)
            close = df["close"]

            # ATR trailing distance as a fraction of price, per bar -> vbt trailing stop
            atr_frac = (ATR_TRAIL_MULT * atr_series(df) / close).clip(lower=0.001).fillna(0.02)

            pf = vbt.Portfolio.from_signals(
                close,
                entries=entries,
                exits=exits,
                price=df["open"].shift(-1),           # fill at next bar open
                init_cash=INIT_CASH,
                fees=0.0003,                          # ~3 bps brokerage+charges (equity intraday)
                slippage=0.0005,                      # 5 bps slippage assumption
                size=PER_SYMBOL_CAP_PCT,
                size_type="percent",
                sl_stop=atr_frac.values,              # ATR distance
                sl_trail=True,                        # ...as a TRAILING stop
                freq=freq,
                min_size=LOT_SIZE,
                size_granularity=LOT_SIZE,
            )
            s = pf.stats()
            summary.append({
                "symbol": sym,
                "return_pct": _num(s.get("Total Return [%]")),
                "sharpe": _num(s.get("Sharpe Ratio")),
                "maxdd_pct": _num(s.get("Max Drawdown [%]")),
                "win_pct": _num(s.get("Win Rate [%]")),
                "trades": _num(s.get("Total Trades")),
            })
            log.info("%-12s ret=%7.2f%%  sharpe=%5.2f  maxDD=%6.2f%%  win=%5.1f%%  trades=%s",
                     sym, summary[-1]["return_pct"], summary[-1]["sharpe"],
                     summary[-1]["maxdd_pct"], summary[-1]["win_pct"], summary[-1]["trades"])
        except Exception:
            log.exception("%s: backtest failed - skipping", sym)

    if not summary:
        log.error("No symbols produced a backtest.")
        return

    sdf = pd.DataFrame(summary)
    out_dir = Path("backtests") / STRATEGY_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    sdf.to_csv(out_dir / "basket_summary.csv", index=False)

    log.info("=" * 72)
    log.info("BASKET SUMMARY (equal-weight, independent legs)")
    log.info("  symbols traded : %d", len(sdf))
    log.info("  avg return     : %.2f%%", sdf["return_pct"].mean())
    log.info("  median return  : %.2f%%", sdf["return_pct"].median())
    log.info("  avg sharpe     : %.2f", sdf["sharpe"].mean())
    log.info("  avg max DD     : %.2f%%", sdf["maxdd_pct"].mean())
    log.info("  total trades   : %d", int(sdf["trades"].fillna(0).sum()))
    log.info("  saved -> %s", out_dir / "basket_summary.csv")
    log.info("=" * 72)


def _vbt_freq(interval):
    return {"1m": "1min", "3m": "3min", "5m": "5min", "10m": "10min",
            "15m": "15min", "30m": "30min", "1h": "1H", "D": "1D"}.get(interval, "15min")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


# ============================================================================
# LIVE - multi-symbol eoc loop + per-symbol ATR-trailing exits on the LTP feed
# ============================================================================

class Position:
    __slots__ = ("symbol", "qty", "entry_price", "entry_time", "watermark",
                 "sl_price", "trail_frac", "exiting")

    def __init__(self, symbol, qty, entry_price, entry_time, sl_price, trail_frac):
        self.symbol = symbol
        self.qty = qty
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.watermark = entry_price
        self.sl_price = sl_price        # initial ATR hard stop (absolute)
        self.trail_frac = trail_frac    # ATR trailing distance as fraction of price
        self.exiting = False


class BasketTrader:
    def __init__(self, client):
        self.client = client
        self.positions = {}            # symbol -> Position
        self.lock = threading.Lock()
        self.last_seen_ts = {}         # symbol -> last closed bar ts (off-by-one guard)
        self.universe = []
        self._universe_fetched_at = 0.0

    # --- universe -----------------------------------------------------------

    def refresh_universe(self, force=False):
        now = time.time()
        if not force and (now - self._universe_fetched_at) < UNIVERSE_REFRESH_SEC:
            return
        self.universe = fetch_traderfinder_universe()
        self._universe_fetched_at = now

    # --- LTP feed (one callback dispatches all symbols) ---------------------

    def on_tick(self, raw):
        d = raw.get("data", raw) if isinstance(raw, dict) else {}
        sym = d.get("symbol") or raw.get("symbol")
        try:
            ltp = float(d.get("ltp", 0) or 0)
        except (TypeError, ValueError):
            return
        if not sym or ltp <= 0:
            return

        with self.lock:
            pos = self.positions.get(sym)
            if pos is None or pos.exiting:
                return
            if ltp > pos.watermark:
                pos.watermark = ltp

            reason = None
            if ltp <= pos.sl_price:
                reason = f"ATR_SL ({ltp:.2f} <= {pos.sl_price:.2f})"
            else:
                trail_trigger = pos.watermark * (1.0 - pos.trail_frac)
                if pos.watermark > pos.entry_price and ltp <= trail_trigger:
                    reason = (f"ATR_TRAIL ({ltp:.2f} <= {trail_trigger:.2f}, "
                              f"peak={pos.watermark:.2f})")
            if reason is None and TIME_EXIT_MIN > 0:
                if (time.time() - pos.entry_time) / 60.0 >= TIME_EXIT_MIN:
                    reason = f"TIME_EXIT ({TIME_EXIT_MIN}m)"
            if reason is None:
                return
            pos.exiting = True

        threading.Thread(target=self._flatten, args=(sym, ltp, reason), daemon=True).start()

    def _flatten(self, sym, ltp, reason):
        log.info("EXIT %s reason=%s ltp=%.2f", sym, reason, ltp)
        try:
            self.client.placesmartorder(
                strategy=STRATEGY_NAME, symbol=sym, exchange=EXCHANGE,
                action="SELL", price_type="MARKET", product=PRODUCT,
                quantity=self.positions[sym].qty, position_size=0,
            )
        except Exception:
            log.exception("Exit order failed for %s - manual review", sym)
        finally:
            self._drop_position(sym)

    def _drop_position(self, sym):
        with self.lock:
            self.positions.pop(sym, None)
        try:
            self.client.unsubscribe_ltp([{"exchange": EXCHANGE, "symbol": sym}])
        except Exception:
            log.exception("unsubscribe_ltp failed for %s - continuing", sym)

    # --- sizing -------------------------------------------------------------

    def _size(self, ltp, sl_distance):
        try:
            cash = float(self.client.funds().get("data", {}).get("availablecash", 0) or 0)
        except Exception:
            log.exception("funds() failed - cannot size")
            return 0
        if cash <= 0 or ltp <= 0 or sl_distance <= 0:
            return 0
        qty_by_risk = math.floor((cash * RISK_PER_TRADE) / sl_distance)
        qty_by_cap  = math.floor((cash * PER_SYMBOL_CAP_PCT) / ltp)
        qty = min(qty_by_risk, qty_by_cap)
        return max(qty, 0)

    # --- per-symbol bar-close handling -------------------------------------

    def on_bar(self, sym, df):
        entries, exits = signals(df)
        if len(df) < 3:
            return
        last_atr = float(atr_series(df).iloc[-2])
        ltp = float(df["close"].iloc[-2])
        bar_ts = df.index[-2]

        with self.lock:
            pos = self.positions.get(sym)
            n_open = len(self.positions)

            # keep the trailing distance fresh on the open position
            if pos is not None and last_atr > 0:
                pos.trail_frac = (ATR_TRAIL_MULT * last_atr) / max(ltp, 1e-9)

        new_entry = bool(entries.iloc[-2])
        new_exit  = bool(exits.iloc[-2])

        # EXIT on opposite EMA signal
        if new_exit and pos is not None:
            log.info("SIGNAL EXIT %s @ %s ltp=%.2f", sym, bar_ts, ltp)
            try:
                self.client.placesmartorder(
                    strategy=STRATEGY_NAME, symbol=sym, exchange=EXCHANGE,
                    action="SELL", price_type="MARKET", product=PRODUCT,
                    quantity=pos.qty, position_size=0,
                )
            except Exception:
                log.exception("Signal-exit order failed for %s", sym)
            self._drop_position(sym)
            return

        # ENTRY
        if new_entry and pos is None and sym in self.universe:
            if n_open >= MAX_OPEN_POSITIONS:
                log.info("ENTRY %s skipped - %d/%d slots full", sym, n_open, MAX_OPEN_POSITIONS)
                return
            if last_atr <= 0:
                return
            sl_distance = ATR_SL_MULT * last_atr
            qty = self._size(ltp, sl_distance)
            if qty <= 0:
                log.info("ENTRY %s skipped - qty sized to 0", sym)
                return

            log.info("ENTRY %s @ %s ltp=%.2f atr=%.2f qty=%d sl=%.2f",
                     sym, bar_ts, ltp, last_atr, qty, ltp - sl_distance)
            try:
                resp = self.client.placeorder(
                    strategy=STRATEGY_NAME, symbol=sym, exchange=EXCHANGE,
                    action="BUY", price_type="MARKET", product=PRODUCT, quantity=qty,
                )
            except Exception:
                log.exception("Entry order failed for %s", sym)
                return
            fill = self._fill_price(resp, fallback=ltp)
            pos = Position(
                symbol=sym, qty=qty, entry_price=fill, entry_time=time.time(),
                sl_price=fill - sl_distance,
                trail_frac=(ATR_TRAIL_MULT * last_atr) / max(fill, 1e-9),
            )
            with self.lock:
                self.positions[sym] = pos
            try:
                self.client.subscribe_ltp([{"exchange": EXCHANGE, "symbol": sym}],
                                          on_data_received=self.on_tick)
            except Exception:
                log.exception("subscribe_ltp failed for %s - risk exits won't fire!", sym)

    def _fill_price(self, resp, fallback):
        order_id = resp.get("orderid") if isinstance(resp, dict) else None
        if not order_id:
            return fallback
        for _ in range(10):
            try:
                st = self.client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
                data = st.get("data", {}) if isinstance(st, dict) else {}
                if data.get("order_status") == "complete":
                    avg = data.get("average_price") or data.get("price")
                    if avg:
                        return float(avg)
            except Exception:
                log.exception("orderstatus poll failed")
            time.sleep(0.5)
        return fallback

    # --- main loop ----------------------------------------------------------

    def run(self):
        self.refresh_universe(force=True)
        self.client.connect()
        first_poll = True
        end = datetime.now().date()
        start = end - timedelta(days=10)

        while not stop_event.is_set():
            self.refresh_universe()
            # manage every symbol in the universe plus any with an open position
            with self.lock:
                watched = set(self.universe) | set(self.positions.keys())

            for sym in watched:
                if stop_event.is_set():
                    break
                try:
                    raw = self.client.history(
                        symbol=sym, exchange=EXCHANGE, interval=INTERVAL,
                        start_date=start.strftime("%Y-%m-%d"),
                        end_date=end.strftime("%Y-%m-%d"), source="api")
                    df = _normalize(raw)
                    if df is None or len(df) < 3:
                        continue
                    closed_ts = df.index[-2]
                    prev = self.last_seen_ts.get(sym)
                    self.last_seen_ts[sym] = closed_ts
                    if first_poll or prev is None:
                        continue           # seed only - never act on a stale bar at startup
                    if closed_ts > prev:
                        self.on_bar(sym, df)
                except Exception:
                    log.exception("poll failed for %s - continuing", sym)

            first_poll = False
            stop_event.wait(POLL_INTERVAL_SEC)


def run_live():
    log.info("=" * 72)
    log.info("LIVE basket: %s | %s @ %s | EMA %d/%d | ATR trail %.1fx",
             STRATEGY_NAME, EXCHANGE, INTERVAL, FAST_EMA, SLOW_EMA, ATR_TRAIL_MULT)
    log.info("Caps: per-symbol %.0f%%, max %d open, risk/trade %.2f%%",
             PER_SYMBOL_CAP_PCT * 100, MAX_OPEN_POSITIONS, RISK_PER_TRADE * 100)
    log.info("=" * 72)

    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)

    # Preflight: broker session must be alive before any order goes out
    try:
        funds = client.funds()
        if not isinstance(funds, dict) or funds.get("status") != "success":
            log.error("Preflight: funds() not successful (%s) - aborting", funds)
            return
        cash = float(funds["data"].get("availablecash", 0) or 0)
        log.info("Preflight OK - available cash Rs %.2f", cash)
    except Exception:
        log.exception("Preflight failed - broker not authenticated - aborting")
        return

    trader = BasketTrader(client)
    try:
        trader.run()
    finally:
        # flat-on-stop is intentionally NOT automatic; just clean up the feed
        try:
            client.disconnect()
        except Exception:
            pass
        log.info("Shutdown complete. Open positions left untouched: %s",
                 list(trader.positions.keys()))


# ============================================================================
# SIGTERM-safe shutdown (required for OpenAlgo /python self-hosted)
# ============================================================================

def _shutdown(signum, frame):
    log.info("Signal %d received - shutting down gracefully", signum)
    stop_event.set()

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


def main():
    parser = argparse.ArgumentParser(description=f"{STRATEGY_NAME} - dual-mode basket strategy")
    parser.add_argument("--mode", choices=["backtest", "live"],
                        default=os.getenv("MODE", "live"))
    args = parser.parse_args()
    if args.mode == "backtest":
        run_backtest()
    else:
        run_live()


if __name__ == "__main__":
    main()
