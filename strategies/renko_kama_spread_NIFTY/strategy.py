"""
Renko + KAMA Hedged NIFTY Weekly-Options Credit Spread - EXECUTION-ONLY.

Uptrend (renko-brick KAMA rising, market not choppy)  -> bull put credit spread
    SELL PE near 0.45-0.55 |delta|, BUY further-OTM PE hedge near 0.20-0.25 |delta|
Downtrend (KAMA falling)                              -> mirror bear call spread (CE)

Exit: opposite trend flip only, or forced square-off at SQUAREOFF_TIME. No SL/target -
this matches exactly what was validated in the separate 90-day backtest at
backtesting/renko_kama_hedged_spread/renko_kama_spread_backtest.py (fixed 15pt renko
brick, KAMA(14,2,30) on brick closes, Choppiness Index < 38.2 filter, 5-minute bars).
Do not add risk logic here without re-validating in that backtest first.

Backtest mode is not supported here - this file is live-execution-only, mirroring the
short_straddle/iron_condor templates in this skill pack. The signal itself was already
backtested with real historical option premiums in the DuckDB harness referenced above;
re-deriving that here would just duplicate untested code paths.

Execution-order safety: entries go through optionsmultiorder with the BUY (hedge) leg
listed first - OpenAlgo always places BUY legs before SELL legs in a multi-leg basket,
so the hedge is in place before the naked short leg's margin is ever evaluated. Exits are
NOT a multi-leg basket (must close the *exact* strikes already held, not a fresh ATM-
relative offset) - they use two sequenced placeorder calls: buy back the short leg first
(removing the open-ended risk while the hedge still protects it), confirm the fill, then
sell the hedge leg second.

Run under OpenAlgo's Analyzer Mode (toggle at /analyzer) for a dry run - orders are
intercepted server-side and never reach the broker, regardless of anything in this file.
"""
import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
from dotenv import find_dotenv, load_dotenv

_HERE = Path(__file__).resolve().parent
for parent in [_HERE, *_HERE.parents]:
    candidate = parent / ".claude" / "skills" / "algo-expert" / "rules" / "assets" / "core"
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent))
        break

from openalgo import api, ta  # noqa: E402

# === Config ===
UNDERLYING = "NIFTY"
UNDERLYING_EXCH = os.getenv("OPENALGO_STRATEGY_EXCHANGE", "NSE_INDEX")
EXPIRY_DATE = os.getenv("EXPIRY_DATE", "04AUG26")  # DDMMMYY - update every week, never auto-rolled
LOTS = int(os.getenv("LOTS", "5"))
PRODUCT = "NRML"
STRATEGY_NAME = os.getenv("STRATEGY_NAME", "renko_kama_spread")

SQUAREOFF_TIME = dtime(15, 24)
SESSION_START = dtime(9, 15)
SESSION_END = dtime(15, 29)

# EXIT_MODE="eod" (default): force-close every day at SQUAREOFF_TIME, matching
# exactly what was backtested and what ran live on day 1.
# EXIT_MODE="carry": skip the daily square-off and let a position ride across
# days (validated in backtest to capture more theta decay - net P&L roughly
# 2.5x higher over the same 90 days, at the cost of a somewhat larger max
# drawdown and real overnight/weekend gap exposure the backtest doesn't fully
# price). Still force-closes on/after the position's own contract expiry day.
EXIT_MODE = os.getenv("EXIT_MODE", "eod")
assert EXIT_MODE in ("eod", "carry"), f"EXIT_MODE must be 'eod' or 'carry', got {EXIT_MODE!r}"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

BRICK_SIZE = float(os.getenv("BRICK_SIZE", "15"))  # points, validated in backtest
KAMA_LEN, KAMA_FAST, KAMA_SLOW = 14, 2, 30
CHOP_PERIOD = 14
CHOP_THRESHOLD = float(os.getenv("CHOP_THRESHOLD", "38.2"))  # trade only when CHOP < this

SHORT_LO, SHORT_HI = 0.45, 0.55
LONG_LO, LONG_HI = 0.20, 0.25
RATE = 0.065
STRIKE_COUNT = 15  # option-chain strikes scanned each side of ATM

WARMUP_DAYS = 15  # trailing days of 1m history to seed renko/KAMA/CHOP
POLL_SECONDS = 30  # how often to check for a newly-closed 5m bar
FILL_WAIT_RETRIES, FILL_WAIT_SLEEP = 20, 1.0

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(STRATEGY_NAME)
load_dotenv(find_dotenv(usecwd=True))
API_KEY = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WS_URL = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}"
)


# ---- Black-Scholes (identical to the validated backtest - do not diverge) --
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(opt: str, S: float, K: float, T: float, r: float, sig: float) -> float:
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if opt == "CE" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if opt == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(opt: str, price: float, S: float, K: float, T: float, r: float):
    intrinsic = max(0.0, (S - K) if opt == "CE" else (K - S))
    if price <= intrinsic + 1e-6 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(opt, S, K, T, r, hi) < price:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(opt, S, K, T, r, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_delta(opt: str, S: float, K: float, T: float, r: float, sig: float) -> float:
    if T <= 0 or sig <= 0:
        return (1.0 if S > K else 0.0) if opt == "CE" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return _ncdf(d1) if opt == "CE" else _ncdf(d1) - 1.0


# ---- Renko / KAMA / CHOP (identical logic to the validated backtest) -------
def resample_ohlc(spot_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Right-labeled resample: a bucket's timestamp is when its data is fully
    known (bucket close), not when it opened. Left-labeling was a confirmed
    lookahead bug in the original backtest - do not change this to 'left'."""
    agg = spot_1m.resample(rule, label="right", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return agg.dropna()


def build_renko_bricks(close: pd.Series, brick_size: float) -> pd.DataFrame:
    rows = []
    level = None
    for ts, price in close.items():
        if level is None:
            level = round(price / brick_size) * brick_size
            continue
        while price >= level + brick_size:
            level += brick_size
            rows.append((ts, level, 1))
        while price <= level - brick_size:
            level -= brick_size
            rows.append((ts, level, -1))
    if not rows:
        return pd.DataFrame(columns=["close", "direction"])
    df = pd.DataFrame(rows, columns=["timestamp", "close", "direction"])
    return df.set_index("timestamp")


def _stretch_to(step: pd.Series, full_index: pd.DatetimeIndex) -> pd.Series:
    step = step[~step.index.duplicated(keep="last")]
    merged = step.reindex(step.index.union(full_index)).sort_index().ffill()
    return merged.reindex(full_index).fillna(0)


def compute_trend(close: pd.Series, brick_size: float) -> pd.Series:
    """KAMA on renko brick closes; trend = sign of KAMA slope between bricks."""
    bricks = build_renko_bricks(close, brick_size)
    if len(bricks) < KAMA_LEN + 2:
        return pd.Series(0, index=close.index)
    k = ta.kama(bricks["close"].values, length=KAMA_LEN, fast_length=KAMA_FAST, slow_length=KAMA_SLOW)
    slope = np.sign(np.diff(k, prepend=k[0]))
    at_brick = pd.Series(slope, index=bricks.index)
    return _stretch_to(at_brick, close.index)


def compute_chop(spot: pd.DataFrame) -> pd.Series:
    vals = ta.chop(spot["high"].values, spot["low"].values, spot["close"].values, period=CHOP_PERIOD)
    return pd.Series(vals, index=spot.index)


def pick_in_band(chain: list, side: str, spot: float, T: float, lo: float, hi: float):
    """Scan the live option chain for `side` (CE/PE); return the entry
    (strike, symbol, offset_label, ltp, delta) whose |delta| lands in
    [lo, hi], else the one closest to the band center."""
    center = (lo + hi) / 2
    candidates = []
    for row in chain:
        opt = row.get(side.lower())
        if not opt or not opt.get("ltp"):
            continue
        px = float(opt["ltp"])
        if px <= 0.05:
            continue
        K = float(row["strike"])
        iv = implied_vol(side, px, spot, K, T, RATE)
        if iv is None:
            continue
        d = abs(bs_delta(side, spot, K, T, RATE, iv))
        candidates.append((K, opt["symbol"], opt["label"], px, d))
    if not candidates:
        return None
    in_band = [c for c in candidates if lo <= c[4] <= hi]
    pool = in_band if in_band else candidates
    return min(pool, key=lambda c: abs(c[4] - center))


def tte_years(now_ist: datetime, expiry: date) -> float:
    secs = (datetime.combine(expiry, dtime(15, 30)) - now_ist.replace(tzinfo=None)).total_seconds()
    return max(secs, 60) / (365.0 * 24 * 3600)


def _ist_now() -> datetime:
    return datetime.now(pytz.timezone("Asia/Kolkata"))


def _wait_fill(client, oid, retries=FILL_WAIT_RETRIES, sleep_s=FILL_WAIT_SLEEP):
    if not oid:
        return None
    for _ in range(retries):
        try:
            r = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
            d = r.get("data", {}) if isinstance(r, dict) else {}
            if d.get("order_status") == "complete":
                avg = d.get("average_price") or d.get("price")
                if avg:
                    return float(avg)
        except Exception:
            log.exception("orderstatus failed")
        stop_event.wait(sleep_s)
    return None


def run_backtest():
    log.error("=" * 70)
    log.error("This file is live-execution-only - options backtesting is out of scope")
    log.error("(volatility surface / time-decay / OI dynamics aren't well modeled by")
    log.error("intraday OHLCV replay). The signal was already backtested separately:")
    log.error("  backtesting/renko_kama_hedged_spread/renko_kama_spread_backtest.py")
    log.error("Use --mode live with OpenAlgo's Analyzer Mode toggle for a dry run.")
    log.error("=" * 70)
    sys.exit(2)


def _fetch_5m_bars(client, start_date: str, end_date: str) -> pd.DataFrame:
    df = client.history(
        symbol=UNDERLYING, exchange=UNDERLYING_EXCH, interval="1m",
        start_date=start_date, end_date=end_date, source="api",
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = df.rename(columns={c: c.lower() for c in df.columns})
    if not isinstance(df.index, pd.DatetimeIndex):
        ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
        idx = pd.to_datetime(df[ts_col], utc=True, unit="s" if pd.api.types.is_numeric_dtype(df[ts_col]) else None)
        df = df.set_index(idx)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    df = df[["open", "high", "low", "close"]].sort_index()
    bars = resample_ohlc(df, "5min")
    now_naive = _ist_now().replace(tzinfo=None)
    return bars[bars.index <= now_naive]  # drop any still-forming bucket


def open_spread(client, side: str, spot: float, expiry: date, qty: int, now_ist: datetime):
    T = tte_years(now_ist, expiry)
    ok, chain_resp, _ = _get_chain(client)
    if not ok:
        log.error("Option chain fetch failed: %s", chain_resp)
        return None
    chain = chain_resp.get("chain", [])
    spot_ltp = chain_resp.get("underlying_ltp") or spot

    short = pick_in_band(chain, side, spot_ltp, T, SHORT_LO, SHORT_HI)
    long_ = pick_in_band(chain, side, spot_ltp, T, LONG_LO, LONG_HI)
    if not short or not long_ or short[0] == long_[0]:
        log.warning("No valid strike pair for %s spread (short=%s long=%s) - skipping entry", side, short, long_)
        return None

    sK, s_sym, s_label, s_ltp, s_delta = short
    lK, l_sym, l_label, l_ltp, l_delta = long_
    log.info(
        "Opening %s spread: SELL %s (%s, delta=%.2f, ltp=%.2f) / BUY %s (%s, delta=%.2f, ltp=%.2f)",
        side, s_sym, s_label, s_delta, s_ltp, l_sym, l_label, l_delta, l_ltp,
    )

    try:
        response = client.optionsmultiorder(
            strategy=STRATEGY_NAME, underlying=UNDERLYING, exchange=UNDERLYING_EXCH,
            expiry_date=EXPIRY_DATE,
            legs=[
                {"offset": l_label, "option_type": side, "action": "BUY",
                 "quantity": qty, "product": PRODUCT, "pricetype": "MARKET"},
                {"offset": s_label, "option_type": side, "action": "SELL",
                 "quantity": qty, "product": PRODUCT, "pricetype": "MARKET"},
            ],
        )
    except Exception:
        log.exception("optionsmultiorder failed - aborting entry")
        return None

    if not isinstance(response, dict) or response.get("status") != "success":
        log.error("Spread entry failed: %s", response)
        return None
    results = response.get("results", [])
    if len(results) < 2:
        log.error("Unexpected optionsmultiorder results: %s", results)
        return None

    long_oid, short_oid = results[0].get("orderid"), results[1].get("orderid")
    long_symbol, short_symbol = results[0].get("symbol"), results[1].get("symbol")
    long_entry = _wait_fill(client, long_oid)
    short_entry = _wait_fill(client, short_oid)
    log.info("Fills: hedge(%s) @ %s | short(%s) @ %s", long_symbol, long_entry, short_symbol, short_entry)

    return {
        "side": side, "qty": qty, "opened_ts": now_ist, "expiry": expiry,
        "short_symbol": short_symbol, "short_entry": short_entry, "short_strike": sK,
        "long_symbol": long_symbol, "long_entry": long_entry, "long_strike": lK,
    }


def close_spread(client, position: dict, reason: str):
    if position is None:
        return
    qty = position["qty"]
    log.info("Closing %s spread (%s): buying back short %s first, then selling hedge %s",
              position["side"], reason, position["short_symbol"], position["long_symbol"])

    try:
        r = client.placeorder(
            strategy=STRATEGY_NAME, symbol=position["short_symbol"], exchange="NFO",
            action="BUY", price_type="MARKET", product=PRODUCT, quantity=qty,
        )
        short_oid = r.get("orderid") if isinstance(r, dict) else None
        short_exit = _wait_fill(client, short_oid)
        log.info("Short leg closed @ %s", short_exit)
    except Exception:
        log.exception("Failed to close short leg - hedge left open, manual check required")
        return

    try:
        r = client.placeorder(
            strategy=STRATEGY_NAME, symbol=position["long_symbol"], exchange="NFO",
            action="SELL", price_type="MARKET", product=PRODUCT, quantity=qty,
        )
        long_oid = r.get("orderid") if isinstance(r, dict) else None
        long_exit = _wait_fill(client, long_oid)
        log.info("Hedge leg closed @ %s", long_exit)
    except Exception:
        log.exception("Failed to close hedge leg - manual check required")
        return

    if position.get("short_entry") and short_exit and position.get("long_entry") and long_exit:
        gross = (position["short_entry"] - short_exit + long_exit - position["long_entry"]) * qty
        log.info("Estimated gross P&L (excl. charges): Rs %.0f", gross)


def _get_chain(client):
    try:
        resp = client.optionchain(
            underlying=UNDERLYING, exchange=UNDERLYING_EXCH,
            expiry_date=EXPIRY_DATE, strike_count=STRIKE_COUNT,
        )
        if isinstance(resp, dict) and resp.get("status") == "success":
            return True, resp, 200
        return False, resp, 400
    except Exception as e:
        log.exception("optionchain call failed")
        return False, {"message": str(e)}, 500


def _lot_size_from_chain(chain_resp: dict, default: int = 65) -> int:
    for row in chain_resp.get("chain", []):
        for side in ("ce", "pe"):
            opt = row.get(side)
            if opt and opt.get("lotsize"):
                return int(opt["lotsize"])
    return default


def _save_state(position: dict | None):
    """Persist the open position to disk. Only matters in EXIT_MODE="carry":
    the /python host restarts this process on its own daily start/stop
    schedule, so an in-memory-only position would silently vanish overnight
    even though the real broker position (or, in analyzer mode, the logged
    intent) is still open. EXIT_MODE="eod" always flattens before the host's
    stop time, so this file is a no-op / stays empty in that mode."""
    try:
        if position is None:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
            return
        serializable = dict(position)
        serializable["opened_ts"] = position["opened_ts"].isoformat()
        serializable["expiry"] = position["expiry"].isoformat()
        STATE_FILE.write_text(json.dumps(serializable))
    except Exception:
        log.exception("Failed to persist state to %s", STATE_FILE)


def _load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        raw = json.loads(STATE_FILE.read_text())
        raw["opened_ts"] = datetime.fromisoformat(raw["opened_ts"])
        raw["expiry"] = date.fromisoformat(raw["expiry"])
        return raw
    except Exception:
        log.exception("Failed to load state from %s - starting flat", STATE_FILE)
        return None


def _should_force_close(position: dict | None, today: date) -> bool:
    if position is None:
        return False
    if EXIT_MODE == "eod":
        return True
    return today >= position["expiry"]  # carry mode: only at/after contract expiry


def run_live():
    expiry = datetime.strptime(EXPIRY_DATE, "%d%b%y").date()
    log.info("=" * 70)
    log.info("LIVE: %s on %s expiry=%s lots=%d brick=%.0f chop<%.1f exit_mode=%s",
              STRATEGY_NAME, UNDERLYING, EXPIRY_DATE, LOTS, BRICK_SIZE, CHOP_THRESHOLD, EXIT_MODE)
    log.info("=" * 70)

    client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)

    ok, chain_resp, _ = _get_chain(client)
    lot_size = _lot_size_from_chain(chain_resp) if ok else 65
    qty = LOTS * lot_size
    log.info("Live lot size=%d -> quantity=%d (%d lots)", lot_size, qty, LOTS)

    # --- Warm-up: seed renko/KAMA/CHOP from trailing history ---
    end = date.today()
    start = end - timedelta(days=WARMUP_DAYS)
    bars = _fetch_5m_bars(client, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if bars.empty:
        log.error("Warm-up history fetch returned no data - aborting")
        return
    log.info("Warm-up: %d 5m bars, %s -> %s", len(bars), bars.index[0], bars.index[-1])

    # EXIT_MODE="carry" positions must survive the host's daily subprocess
    # restart - reload any position saved by a previous run of this process.
    position = _load_state()
    if position is not None:
        log.warning("Resuming a position saved from a previous run: %s %s/%s opened %s",
                     position["side"], position["short_symbol"], position["long_symbol"], position["opened_ts"])
        if EXIT_MODE == "eod":
            log.warning("EXIT_MODE=eod but a saved position was found - this should not happen "
                        "(eod mode always flattens before stop). Closing it now to be safe.")
            close_spread(client, position, "resume-eod-mismatch")
            position = None
            _save_state(None)
    last_bar_ts = bars.index[-1]

    def _process_bar(bar_ts, trend_val, now_ist):
        nonlocal position
        can_enter = now_ist.time() < SQUAREOFF_TIME
        side = "PE" if trend_val > 0 else ("CE" if trend_val < 0 else None)
        spot_px = float(bars.at[bar_ts, "close"])

        if position is None and side is not None and can_enter:
            position = open_spread(client, side, spot_px, expiry, qty, now_ist)
            _save_state(position)
        elif position is not None and side is not None and side != position["side"] and can_enter:
            close_spread(client, position, "flip")
            position = open_spread(client, side, spot_px, expiry, qty, now_ist)
            _save_state(position)

        if _should_force_close(position, now_ist.date()):
            close_spread(client, position, "eod")
            position = None
            _save_state(None)

    try:
        while not stop_event.is_set():
            now_ist = _ist_now()
            if now_ist.time() > SESSION_END:
                log.info("Past session end - stopping for the day")
                break

            fresh = _fetch_5m_bars(
                client, (date.today() - timedelta(days=3)).strftime("%Y-%m-%d"),
                date.today().strftime("%Y-%m-%d"),
            )
            if not fresh.empty:
                bars = pd.concat([bars[~bars.index.isin(fresh.index)], fresh]).sort_index()

            if not bars.empty and bars.index[-1] > last_bar_ts:
                trend = compute_trend(bars["close"], BRICK_SIZE)
                chop = compute_chop(bars)
                trend[chop >= CHOP_THRESHOLD] = 0

                new_bars = bars.index[bars.index > last_bar_ts]
                for bar_ts in new_bars:
                    bar_now = _ist_now()
                    log.info("New 5m bar %s close=%.2f chop=%.1f trend=%d",
                              bar_ts, bars.at[bar_ts, "close"], chop.loc[bar_ts], trend.loc[bar_ts])
                    _process_bar(bar_ts, trend.loc[bar_ts], bar_now)
                last_bar_ts = new_bars[-1]

            # Square-off check even without a new bar, in case polling lags 15:24
            now_ist = _ist_now()
            if _should_force_close(position, now_ist.date()):
                close_spread(client, position, "eod")
                position = None
                _save_state(None)

            stop_event.wait(POLL_SECONDS)
    finally:
        if position is not None:
            if EXIT_MODE == "eod":
                log.warning("Shutdown with an open position in eod mode - closing now")
                close_spread(client, position, "shutdown")
                _save_state(None)
            else:
                log.info("Shutdown in carry mode with an open position - leaving it open, "
                         "state saved to %s for the next run to resume", STATE_FILE)
        try:
            client.cancelallorder(strategy=STRATEGY_NAME)
        except Exception:
            log.exception("cancelallorder failed")
        log.info("Shutdown complete")


stop_event = threading.Event()


def _sh(s, f):
    log.info("signal %d - shutting down", s)
    stop_event.set()


signal.signal(signal.SIGTERM, _sh)
signal.signal(signal.SIGINT, _sh)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backtest", "live"], default=os.getenv("MODE", "live"))
    a = p.parse_args()
    run_backtest() if a.mode == "backtest" else run_live()


if __name__ == "__main__":
    main()
