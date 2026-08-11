#!/usr/bin/env python3
"""
================================================================================
  BREAKOUT INTRADAY STRATEGY  —  OpenAlgo + TradeFinder
================================================================================
  Single-file live trading strategy. No webhook, no TradingView.

  Pine Script port: "Intraday Signals & Risk Calculator [India v5.6]"
  ------------------------------------------------------------------------------
  Watchlist  : TradeFinder IntradayBoost  (refreshed every WATCHLIST_REFRESH_SEC)
  Signal     : single- and double-candle breakout (Pine buy1/buy2/sell1/sell2)
  Filters    : VWAP (buy>VWAP, sell<VWAP)  +  ADX >= threshold   [ON by default]
  Sizing     : risk-based  qty = (capital x risk%) / stop_distance, notional-capped
  Entry      : MARKET on the breakout bar's close
  Stop       : broker-side SL-M at breakout candle low/high  (survives a crash)
  Target     : single target at TARGET_RR x risk  (0 = disabled, hold till EXIT_TIME)
  Breakeven  : move SL to entry once price moves BREAKEVEN_PCT% in your favour (0=off)
  Trail      : move SL to prior candle low/high once the trade is in profit
  Re-entry   : after a position closes, take the next signal on the same stock (ALLOW_REENTRY)
  Square-off : flatten everything at EXIT_TIME HHMM (before broker MIS SOS)

  Self-hosted /python ready: reads env vars, traps SIGTERM, logs to stdout.

  Usage (standalone):
      python breakout_intraday_strategy.py
      python breakout_intraday_strategy.py --test     # one diagnostic pass, no orders

  Usage (OpenAlgo /python runner):
      Upload, set parameters (SYMBOLS source = TradeFinder), schedule 09:15-15:30.
      Env vars OPENALGO_API_KEY / HOST_SERVER / WEBSOCKET_URL are read automatically.
================================================================================
"""

import argparse
import base64
import hashlib
import hmac
import json as _json
import logging
import math
import os
import signal
import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except Exception:
    pass

from openalgo import api as openalgo_api

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG  (every value is env-overridable so the /python upload form can tune it)
# ══════════════════════════════════════════════════════════════════════════════

# ── OpenAlgo connection (canonical env priority: HOST_SERVER > OPENALGO_HOST) ──
OPENALGO_API_KEY = os.getenv("OPENALGO_API_KEY", "")
OPENALGO_HOST    = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
OPENALGO_WS_URL  = os.getenv("WEBSOCKET_URL") or (
    f"ws://{os.getenv('WEBSOCKET_HOST', '127.0.0.1')}:{os.getenv('WEBSOCKET_PORT', '8765')}")
EXCHANGE         = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))

STRATEGY_NAME    = os.getenv("STRATEGY_NAME", "BREAKOUT_INTRADAY")
PRODUCT          = os.getenv("PRODUCT", "MIS")          # intraday
INTERVAL         = os.getenv("INTERVAL", "5m")          # signal timeframe

# ── TradeFinder watchlist source ──────────────────────────────────────────────
# JWT is read DYNAMICALLY on every fetch — update without restarting the strategy.
# Priority: TF_JWT_FILE (file path) > TF_JWT_TOKEN (env var)
#
# Easiest morning workflow:
#   echo "<paste lt token>" > strategies/tf_jwt.txt
# Or set env: TF_JWT_TOKEN="eyJ..."
#
# To get the token: tradefinder.in → DevTools → Application → Local Storage → "lt"
TF_JWT_TOKEN          = os.getenv("TF_JWT_TOKEN", "")           # static fallback (read at startup)
TF_JWT_FILE           = os.getenv("TF_JWT_FILE",  os.path.join(os.path.dirname(__file__), "tf_jwt.txt"))
WATCHLIST_REFRESH_SEC = int(os.getenv("WATCHLIST_REFRESH_SEC", "60"))   # 1 minute
MAX_WATCHLIST         = int(os.getenv("MAX_WATCHLIST", "0"))             # 0 = all; else top-N by score
# TF_RANK_CAP: only take a signal if the stock is ranked <= this in the LIVE TF list at signal time.
# 0 = disabled (trade all stocks regardless of rank).
# Requires the rank poller to be running (starts automatically with the strategy).
TF_RANK_CAP           = int(os.getenv("TF_RANK_CAP", "0"))              # 0 = off; 10 = top-10 only
TF_RANK_POLL_SEC      = int(os.getenv("TF_RANK_POLL_SEC", "300"))        # snapshot every 5 min

# ── Sector Scope filter ───────────────────────────────────────────────────────
# When SECTOR_FILTER_ENABLED=true the strategy only trades stocks whose sector
# is in the top SECTOR_TOP_N_LONG gaining sectors (for LONG) or top
# SECTOR_TOP_N_SHORT losing sectors (for SHORT). No look-ahead bias — uses
# live change% at signal time. Off by default so existing behaviour is unchanged.
SECTOR_FILTER_ENABLED    = os.getenv("SECTOR_FILTER_ENABLED", "false").lower() == "true"
SECTOR_TOP_N_LONG        = int(os.getenv("SECTOR_TOP_N_LONG", "2"))
SECTOR_TOP_N_SHORT       = int(os.getenv("SECTOR_TOP_N_SHORT", "2"))
SECTOR_POLL_SEC          = int(os.getenv("SECTOR_POLL_SEC", "300"))       # poll interval seconds
SECTOR_EXCLUDE_RAW       = os.getenv("SECTOR_EXCLUDE", "OTHERS")          # comma-separated names to skip
SECTOR_EXCLUDE           = {s.strip().upper() for s in SECTOR_EXCLUDE_RAW.split(",") if s.strip()}
# "any" = trade if at least one sector qualifies  |  "all" = every sector must qualify
SECTOR_MULTI_SECTOR_RULE = os.getenv("SECTOR_MULTI_SECTOR_RULE", "any").lower()
# SECTOR_STOCK_ALIGN: skip stocks that move AGAINST their sector direction.
# LONG sector but stock change% < 0  → blocked (e.g. SBIN -0.21% in PSU BANK sector)
# SHORT sector but stock change% > 0 → blocked (e.g. ADANIENT +2.46% in METAL sector)
SECTOR_STOCK_ALIGN       = os.getenv("SECTOR_STOCK_ALIGN", "true").lower() == "true"
# SECTOR_MIN_BREADTH: minimum % of sector stocks that must move in the sector's direction.
# Filters out incoherent sectors (e.g. FIN SERVICE where 50% go up, 50% go down).
# METAL=82% coherent ✅  CEMENT=100% ✅  FIN SERVICE=50% ❌ (skipped at 65%)
# Set to 0 to disable. Default 65 skips mixed sectors.
SECTOR_MIN_BREADTH       = float(os.getenv("SECTOR_MIN_BREADTH", "65"))
# SECTOR_TOP_STOCKS_N: within each active sector, only trade the top N stocks by r_factor.
# 0 = trade all stocks in the sector. Default 4 limits exposure to the strongest movers.
# Backtest evidence: top 4 by r_factor in METAL captured all 4 targets on June 5.
SECTOR_TOP_STOCKS_N      = int(os.getenv("SECTOR_TOP_STOCKS_N", "4"))
# SECTOR_MIN_RFACTOR: skip stocks whose TF r_factor is below this threshold.
# r_factor=0 means TF sees no momentum (e.g. DLF=0.0, LODHA=0.0 in REALTY sector).
# Set to 0.0 to disable. Default 0.5 keeps only stocks with confirmed momentum.
SECTOR_MIN_RFACTOR       = float(os.getenv("SECTOR_MIN_RFACTOR", "0.5"))
# SECTOR_TRUST_AFTER: HHMM — do not apply sector gate before this time.
# First 15 min after open is noisy (gaps, low volume). Gate is permissive until settled.
SECTOR_TRUST_AFTER_HM    = os.getenv("SECTOR_TRUST_AFTER", "0930")
_sta                     = SECTOR_TRUST_AFTER_HM.zfill(4)
SECTOR_TRUST_AFTER_MIN   = int(_sta[:2]) * 60 + int(_sta[2:])
# SECTOR_REVERSAL_BREAKEVEN: when an open position's sector drops out of active range,
# move its SL to breakeven so a reversal costs nothing instead of the full SL distance.
# Uses two filters to avoid false triggers from rank oscillation noise:
#   SECTOR_REVERSAL_BUFFER : extra rank tolerance before triggering.
#       Default 2 means: if trading top-2, only trigger when sector drops to rank 5+
#       (rank 3 or 4 is noise, rank 5+ is a genuine reversal)
#   SECTOR_REVERSAL_POLLS  : consecutive polls sector must stay below threshold.
#       Default 2 = 10 minutes of sustained reversal before acting.
#       One bad poll is ignored — must be confirmed by the next poll.
SECTOR_REVERSAL_BREAKEVEN = os.getenv("SECTOR_REVERSAL_BREAKEVEN", "false").lower() == "true"
SECTOR_REVERSAL_BUFFER    = int(os.getenv("SECTOR_REVERSAL_BUFFER", "2"))
SECTOR_REVERSAL_POLLS     = int(os.getenv("SECTOR_REVERSAL_POLLS",  "2"))
# SECTOR_ONLY_MODE: replace TF IntradayBoost entirely with sector stocks as the watchlist.
# When True: no TF JWT needed, watchlist = all stocks from top SECTOR_TOP_N_LONG gaining
# sectors (LONG candidates) + top SECTOR_TOP_N_SHORT losing sectors (SHORT candidates).
# SECTOR_FILTER_ENABLED is automatically forced True in this mode.
SECTOR_ONLY_MODE         = os.getenv("SECTOR_ONLY_MODE", "false").lower() == "true"
# Force SECTOR_FILTER_ENABLED when SECTOR_ONLY_MODE is on
if SECTOR_ONLY_MODE:
    SECTOR_FILTER_ENABLED = True

# ── Narrow CPR filter ──────────────────────────────────────────────────────────
# When CPR_FILTER_ENABLED=true the strategy only trades stocks whose PRIOR DAY's
# Central Pivot Range (CP=(H+L+C)/3, BC=(H+L)/2, TC=(CP-BC)+CP) is "narrow" — a
# well-known breakout-day signal: a tight prior-day pivot range often precedes a
# bigger intraday move. Width = (TC-BC) as % of CP. Off by default so existing
# behaviour is unchanged.
CPR_FILTER_ENABLED = os.getenv("CPR_FILTER_ENABLED", "false").lower() == "true"
CPR_NARROW_PCT      = float(os.getenv("CPR_NARROW_PCT", "0.5"))   # e.g. 0.5 or 1.0

# CPR_POSITION_FILTER_ENABLED: directional confirmation, independent of the narrow-CPR
# filter above. LONG only allowed with price above CPR top (TC); SHORT only allowed
# with price below CPR bottom (BC). Price inside the CPR band (BC..TC) blocks both
# directions — no directional edge in the pivot zone itself.
CPR_POSITION_FILTER_ENABLED = os.getenv("CPR_POSITION_FILTER_ENABLED", "false").lower() == "true"


def _get_tf_jwt() -> str:
    """Read JWT dynamically: file first (hot-reloadable), then env var."""
    if TF_JWT_FILE and os.path.exists(TF_JWT_FILE):
        try:
            token = open(TF_JWT_FILE).read().strip()
            if token:
                return token
        except Exception:
            pass
    return TF_JWT_TOKEN

# ── Signal config (Pine defaults) ─────────────────────────────────────────────
ENABLE_BUY     = os.getenv("ENABLE_BUY",  "true").lower() == "true"
ENABLE_SELL    = os.getenv("ENABLE_SELL", "true").lower() == "true"
ENABLE_SINGLE  = os.getenv("ENABLE_SINGLE", "false").lower() == "true"  # single-candle breakouts (sweep: double-only wins)
ENABLE_DOUBLE  = os.getenv("ENABLE_DOUBLE", "true").lower() == "true"   # double-candle breakouts
USE_VWAP       = os.getenv("USE_VWAP", "true").lower() == "true"        # matches Pine in_24=true
USE_ADX        = os.getenv("USE_ADX",  "false").lower() == "true"       # Pine in_36=false (ADX filter OFF by default)
ADX_LEN        = int(os.getenv("ADX_LEN", "14"))
ADX_THRESHOLD  = float(os.getenv("ADX_THRESHOLD", "20"))               # Pine in_38=20 (only used when USE_ADX=true)

# ── ATR Stop Loss (matches Pine in_27=true, in_28=14, in_29=2.0) ──────────────
# When ON: SL = close - ATR(ATR_LEN)*ATR_MULT for LONG, close + ATR*MULT for SHORT.
# When OFF: SL = min/max of recent candle lows/highs (classic breakout SL).
USE_ATR_SL  = os.getenv("USE_ATR_SL",  "true").lower() == "true"   # Pine in_27=true
ATR_LEN     = int(os.getenv("ATR_LEN",  "14"))                      # Pine in_28=14
ATR_MULT    = float(os.getenv("ATR_MULT", "2.0"))                   # Pine in_29=2.0

# ── VWAP Chop Filter (Pine in_39=false, in_40=15, in_41=3) ───────────────────
# Suppresses signals when price crosses VWAP too many times (choppy market).
USE_VWAP_CHOP        = os.getenv("USE_VWAP_CHOP",        "false").lower() == "true"  # Pine in_39=false
VWAP_CHOP_LOOKBACK   = int(os.getenv("VWAP_CHOP_LOOKBACK",   "15"))                  # Pine in_40=15
VWAP_CHOP_MAX_TOUCHES= int(os.getenv("VWAP_CHOP_MAX_TOUCHES", "3"))                  # Pine in_41=3

# ── Risk / sizing (Pine "Risk Calculator" defaults) ───────────────────────────
BALANCE          = float(os.getenv("BALANCE", "311000"))      # trading capital
MAX_LOSS_PCT     = float(os.getenv("MAX_LOSS_PCT", "1.0"))    # % of capital risked per trade
CAPITAL_PER_TRADE  = float(os.getenv("CAPITAL_PER_TRADE", "50000"))  # notional cap per stock
MIN_SL_DIST_PCT    = float(os.getenv("MIN_SL_DIST_PCT", "0.3"))      # skip if SL < X% away (charges > profit)
TARGET_RR          = float(os.getenv("TARGET_RR", "2.0"))   # 0 = no fixed target (hold till EXIT_TIME)
# Trail mode: "none" | "after_1R" | "full"
# after_1R  — trail only after +1R profit (sweep winner, protects from noise)
# full      — trail every bar in profit (scratches winners on volatile days)
# none      — no trailing at all (just SL + target/time)
TRAIL_MODE       = os.getenv("TRAIL_MODE", "after_1R").lower()
# Breakeven: move SL to entry once price moves X% in your favour. 0 = disabled.
# Example: BREAKEVEN_PCT=1.0 → move SL to entry after 1% favourable move.
# Works with TRAIL_MODE — after breakeven activates, trail continues from entry.
BREAKEVEN_PCT    = float(os.getenv("BREAKEVEN_PCT", "0.0"))
# BREAKEVEN_COVER_CHARGES: when moving SL to breakeven, place it at entry ± charges-per-share
# instead of exactly at entry. Then a breakeven exit nets TRUE zero (charges covered) rather
# than a small loss (~Rs.53/trade). Applies to BREAKEVEN_PCT, sector-reversal BE, and the
# step-lock 0R level. Cost: SL sits ~10-20% of 1R tighter. Default ON.
BREAKEVEN_COVER_CHARGES = os.getenv("BREAKEVEN_COVER_CHARGES", "true").lower() == "true"
# BREAKEVEN_START_R: move SL to (cost-to-cost) breakeven once the trade reaches this R.
# R-based trigger scales with each stock's stop distance (cleaner than BREAKEVEN_PCT).
#
# IMPORTANT — when to use:
#   • With a FIXED target (TARGET_RR=2): keep this 0. Backtest June 5 showed any
#     breakeven below the target SCRATCHES winners that pull back then hit target
#     (e.g. NBCC: BE@1.5R → ₹0, but plain 2R → +₹733). Target IS your exit.
#   • Trend-riding mode (TARGET_RR=0, no fixed exit): set 1.5R. Then the original SL
#     rides until +1.5R, moves to cost-covered breakeven, and you ride further upside
#     with zero downside. 1.5R cushion beats 1.0R (fewer noise scratches).
# Default 0 = off (matches the recommended fixed-2R config).
BREAKEVEN_START_R       = float(os.getenv("BREAKEVEN_START_R", "0"))

# ── Step-Lock Trail ───────────────────────────────────────────────────────────
# Captures large moves (4R, 5R+) by locking in profit at each R milestone.
# Works best with TARGET_RR=0 (no fixed target — let the step locks handle exits).
#
# How it works:
#   At +1R move  → SL locked at entry   (0R locked — free trade)
#   At +2R move  → SL locked at +1R     (1R profit guaranteed)
#   At +3R move  → SL locked at +2R     (2R profit guaranteed)
#   At +NR move  → SL locked at +(N-1)R (always locks 1R below current milestone)
#
# TRAIL_STEP_R : R-multiple interval between lock steps. Default 1.0.
#   1.0 = lock every 1R (1→0, 2→1, 3→2 ...)
# WARNING: 0.5 is too tight for intraday — morning noise stops you at entry.
#          Backtest showed STEP=0.5 loses money (-₹318) vs STEP=1.0 (+₹4,906).
#          Stick with 1.0 unless the stock's 1R distance is very large (>2%).
#
# Step-lock combines WITH the candle trail — SL = max(step_lock_level, candle_trail).
# To use step-lock as the ONLY exit mechanism, set TRAIL_MODE=none TARGET_RR=0.
TRAIL_STEPS_ENABLED = os.getenv("TRAIL_STEPS_ENABLED", "false").lower() == "true"
TRAIL_STEP_R        = float(os.getenv("TRAIL_STEP_R", "1.0"))   # 1.0 = best for intraday
# TRAIL_LOCK_START_R: keep the ORIGINAL stop until price reaches this R, THEN start locking.
# Gives the trade room to breathe through the noisy 1R-2R pullback zone before the SL
# tightens. Backtest June 5: start=3R lost only -281 vs fixed-2R, while start=1R lost -639.
# Default 2.0 — original SL holds until +2R, then breakeven, then steps.
TRAIL_LOCK_START_R  = float(os.getenv("TRAIL_LOCK_START_R", "2.0"))

# ── Manual command file ───────────────────────────────────────────────────────
# Drop commands into this file while the strategy is running. Strategy reads it
# every loop (~20 sec) and acts safely — cancels broker SL order first.
#
# Supported commands (one per line):
#   EXIT HINDZINC          → cancel SL-M + market exit at best price
#   MOVESL HINDZINC 564    → move SL to 564 (e.g. to lock in more profit)
#   EXITALL                → square off every open position immediately
#
# Usage from terminal while strategy is running:
#   echo "EXIT HINDZINC"       >> strategies/manual_commands.txt
#   echo "MOVESL HINDZINC 564" >> strategies/manual_commands.txt
MANUAL_CMD_FILE = os.getenv("MANUAL_CMD_FILE",
                             os.path.join(os.path.dirname(__file__), "manual_commands.txt"))

# ── Session / risk guardrails ─────────────────────────────────────────────────
SESSION_START_HM = os.getenv("SESSION_START",  "0915")  # HHMM — session open
ENTRY_CUTOFF_HM  = os.getenv("ENTRY_CUTOFF",   "1500")  # HHMM — no NEW entries after this
EXIT_TIME_HM     = os.getenv("EXIT_TIME",       "1525")  # HHMM — voluntary square-off
HARD_EXIT_TIME_HM= os.getenv("HARD_EXIT_TIME",  "1520")  # HHMM — failsafe (< broker MIS SOS)
# Parse exit times into hours/minutes for backward compat
_et  = EXIT_TIME_HM.zfill(4);      EXIT_HOUR,      EXIT_MINUTE      = int(_et[:2]), int(_et[2:])
_het = HARD_EXIT_TIME_HM.zfill(4); HARD_EXIT_HOUR, HARD_EXIT_MINUTE = int(_het[:2]), int(_het[2:])

ALLOW_REENTRY    = os.getenv("ALLOW_REENTRY", "true").lower() == "true"  # re-enter after exit
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
MAX_DAILY_LOSS_RS  = float(os.getenv("MAX_DAILY_LOSS_RS", "25000"))
LEVERAGE           = float(os.getenv("LEVERAGE", "5"))   # MIS intraday leverage (margin = notional/LEVERAGE)
ORDER_DELAY_S      = float(os.getenv("ORDER_DELAY_S", "0.112"))   # ~9 orders/sec (< SEBI 10 OPS)
POLL_INTERVAL_SEC  = int(os.getenv("POLL_INTERVAL_SEC", "20"))    # main loop cadence

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"  # True = log only, no orders sent
# REQUIRE_ANALYZER: when running with real order placement (DRY_RUN=false) for a PAPER test,
# refuse to start unless OpenAlgo's analyzer/sandbox mode is ON. This is a hard safety guard
# so a toggled-off analyzer can never let the paper test fire REAL broker orders. Default ON.
REQUIRE_ANALYZER = os.getenv("REQUIRE_ANALYZER", "true").lower() == "true"

# ── Log directory ─────────────────────────────────────────────────────────────
# Uses BREAKOUT_LOG_DIR (not LOG_DIR) to avoid conflict with OpenAlgo's own LOG_DIR env var.
BREAKOUT_LOG_DIR = os.getenv("BREAKOUT_LOG_DIR", "logs/breakout")

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING  (stdout + daily rotating files)
# ══════════════════════════════════════════════════════════════════════════════
_RUN_DATE   = datetime.now().strftime("%Y-%m-%d")
_LOG_DIR    = BREAKOUT_LOG_DIR
os.makedirs(_LOG_DIR, exist_ok=True)

_FMT = "%(asctime)s  %(levelname)-7s  %(message)s"

# Root handler: stdout (captured by /python host)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=_FMT, stream=sys.stdout)
log = logging.getLogger(STRATEGY_NAME)
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# File handlers — skip when running under OpenAlgo /python host (STRATEGY_ID is injected by host)
if not os.getenv("STRATEGY_ID"):
    _fh_all = logging.FileHandler(os.path.join(_LOG_DIR, f"breakout_{_RUN_DATE}.log"), encoding="utf-8")
    _fh_all.setFormatter(logging.Formatter(_FMT))
    _fh_all.setLevel(logging.INFO)
    log.addHandler(_fh_all)

    _fh_err = logging.FileHandler(os.path.join(_LOG_DIR, f"breakout_errors_{_RUN_DATE}.log"), encoding="utf-8")
    _fh_err.setFormatter(logging.Formatter(_FMT))
    _fh_err.setLevel(logging.WARNING)
    log.addHandler(_fh_err)

# Paths
_LOG_JSON      = os.getenv("JSONL_PATH",  os.path.join(_LOG_DIR, f"breakout_events_{_RUN_DATE}.jsonl"))
_STATE_FILE    = os.getenv("STATE_PATH",  os.path.join(_LOG_DIR, f"breakout_state_{_RUN_DATE}.json"))
_TRADE_CSV     = os.path.join(_LOG_DIR, f"breakout_trades_{_RUN_DATE}.csv")
_TF_SNAPSHOT_FILE     = os.path.join(_LOG_DIR, f"tf_snapshots_{_RUN_DATE}.jsonl")
_SECTOR_SNAPSHOT_FILE = os.path.join(_LOG_DIR, f"sector_snapshots_{_RUN_DATE}.jsonl")

# ── TF rank cache (updated by background poller) ──────────────────────────────
_tf_rank_cache: dict[str, int] = {}   # symbol -> 1-based rank in current TF list
_tf_rank_lock  = threading.Lock()

# ── Sector scope cache (updated by sector poller) ─────────────────────────────
_sector_cache: dict = {
    "snapshot_time":   "",     # "HH:MM" of last successful poll
    "sector_scores":   {},     # sector_name -> avg_change_pct float
    "sector_ranks":    {},     # sector_name -> 1-based int rank (1 = top gainer)
    "stock_sectors":   {},     # symbol -> [sector_name, ...]
    "stock_change_pct":{},     # symbol -> individual change% (from sector data)
    "stock_rfactor":   {},     # symbol -> TF r_factor (momentum score)
    "stock_active_sectors": {},# symbol -> count of active (long/short) sectors it belongs to
    "sector_breadth":       {},  # sector_name -> % of stocks aligned with sector direction
    "sector_top_n_stocks":  {},  # sector_name -> set of top-N symbols by r_factor
    "long_sectors":    set(),  # top SECTOR_TOP_N_LONG sector names
    "short_sectors":   set(),  # bottom SECTOR_TOP_N_SHORT sector names
    "ever_active_today":  set(), # sectors that were EVER in top-N today (grows, never shrinks)
    "reversal_count":     {},   # sector_name -> consecutive polls outside active range
    "confirmed_reversed": set(),# sectors confirmed reversed (polls >= SECTOR_REVERSAL_POLLS)
    "trajectory":      {},     # sector_name -> [{time, rank, score, rank_delta}, ...]
}
_sector_lock = threading.Lock()

import csv as _csv

# ── Trade CSV writer ──────────────────────────────────────────────────────────
_CSV_HEADER = [
    "date", "signal_time", "entry_time", "exit_time",
    "symbol", "direction", "kind",
    "signal_adx", "signal_vwap",
    "entry_price", "initial_sl", "final_sl", "target",
    "exit_price", "qty", "notional",
    "gross", "charges", "net", "reason",
    "breakeven_activated", "reached_1r",
    "dry_run",
]

def _write_trade_csv(pos, signal_time: str = "", signal_adx: float = 0.0,
                     signal_vwap: float = 0.0, kind: str = ""):
    """Append one completed trade row to the daily CSV."""
    if pos.exit_price == 0.0:
        return
    now_str = _now_ist().strftime("%H:%M:%S") if "_now_ist" in globals() else ""
    gross   = (pos.exit_price - pos.entry_price) * pos.qty * (1 if pos.direction == "LONG" else -1)
    ch      = compute_charges(pos.entry_price, pos.exit_price, pos.qty)
    net     = gross - ch
    notional = pos.entry_price * pos.qty

    write_header = not os.path.exists(_TRADE_CSV)
    try:
        with open(_TRADE_CSV, "a", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=_CSV_HEADER)
            if write_header:
                w.writeheader()
            w.writerow({
                "date":                 _RUN_DATE,
                "signal_time":          signal_time or getattr(pos, "signal_time", ""),
                "entry_time":           getattr(pos, "entry_time", ""),
                "exit_time":            now_str,
                "symbol":               pos.symbol,
                "direction":            pos.direction,
                "kind":                 kind or getattr(pos, "kind", ""),
                "signal_adx":           round(signal_adx or getattr(pos, "signal_adx", 0.0), 2),
                "signal_vwap":          round(signal_vwap or getattr(pos, "signal_vwap", 0.0), 2),
                "entry_price":          pos.entry_price,
                "initial_sl":           pos.initial_sl,
                "final_sl":             pos.current_sl,
                "target":               pos.target,
                "exit_price":           pos.exit_price,
                "qty":                  pos.qty,
                "notional":             round(notional, 2),
                "gross":                round(gross, 2),
                "charges":              round(ch, 2),
                "net":                  round(net, 2),
                "reason":               pos.exit_reason,
                "breakeven_activated":  pos.reached_be,
                "reached_1r":           pos.reached_1r,
                "dry_run":              DRY_RUN,
            })
    except Exception as e:
        log.warning(f"_write_trade_csv failed: {e}")


def _jlog(event: str, **data):
    """Append one structured JSON line for offline replay of trade-critical events."""
    try:
        with open(_LOG_JSON, "a") as f:
            f.write(_json.dumps({"ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                                 "event": event, **data}) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# SIGTERM-safe shutdown  (required for /python host)
# ══════════════════════════════════════════════════════════════════════════════
stop_event = threading.Event()


def _shutdown(signum, frame):
    log.info("Signal %d received — shutting down gracefully", signum)
    stop_event.set()


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


# ══════════════════════════════════════════════════════════════════════════════
# TRADEFINDER CLIENT  (lifted verbatim from the ANN strategy)
# ══════════════════════════════════════════════════════════════════════════════
_TF_BASE   = "https://tradefinder.in/api_be"
_TF_SECRET = "5ACHPKZUZNTWYPSJXNP7IULMACAM6P6Q"   # TOTP secret — do not change

import urllib3  # noqa: E402
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_TF_SSL_VERIFY = False   # LibreSSL workaround (macOS system Python). Use Py3.12 to remove.


def _generate_totp(ts_ms: int) -> str:
    """RFC 6238 TOTP — HMAC-SHA1, 30s window, 6 digits."""
    raw = _TF_SECRET.upper()
    key = base64.b32decode(raw + "=" * ((8 - len(raw) % 8) % 8))
    counter = int(ts_ms / 1000 / 30)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def _tf_server_time_ms() -> int:
    try:
        r = requests.get(f"{_TF_BASE}/servertime", timeout=5, verify=_TF_SSL_VERIFY)
        return int(r.json()["payload"]["data"])
    except Exception:
        return int(time.time() * 1000)


def fetch_intraday_boost() -> list[dict]:
    """Fetch TradeFinder IntradayBoost stocks. Returns list sorted by score desc."""
    jwt = _get_tf_jwt()
    if not jwt:
        log.error("TF_JWT_TOKEN is empty. Quick fix: echo '<token>' > strategies/tf_jwt.txt")
        return []
    totp = _generate_totp(_tf_server_time_ms())
    headers = {"jwttoken": jwt, "accesstoken": totp}
    try:
        data = requests.get(f"{_TF_BASE}/data/market_pulse", headers=headers,
                            timeout=10, verify=_TF_SSL_VERIFY).json()
    except Exception as e:
        log.error(f"TradeFinder request failed: {e}")
        return []
    if data.get("status") != "SUCCESS":
        log.error(f"TradeFinder error: {data.get('code')} — {data.get('message')} "
                  f"(JWT expired? paste a fresh token)")
        return []
    raw = (data.get("payload") or {}).get("data") or {}
    items_raw = raw.get("intraday_boost") or raw.get("intradayBoost") or []
    items = [{"symbol": r["Symbol"], "ltp": float(r["param_0"]),
              "prev_close": float(r["param_1"]), "change_pct": float(r["param_2"]),
              "score": float(r["param_3"])} for r in items_raw]
    items.sort(key=lambda x: x["score"], reverse=True)
    return items


def fetch_sector_scope() -> dict[str, dict]:
    """
    Fetch TradeFinder Sector Scope data.

    Returns dict keyed by canonical sector name (uppercased, suffix stripped), e.g.:
      {
        "NIFTY REALTY": {
          "stocks": {"DLF": {"change_pct": 2.3, "r_factor": 1.8, "ltp": 890, ...}, ...},
          "avg_change_pct": 2.35,
          "avg_r_factor": 1.95,
          "stock_count": 7,
        }, ...
      }
    Returns {} on any error (never None).
    """
    jwt = _get_tf_jwt()
    if not jwt:
        log.warning("fetch_sector_scope: no JWT token available")
        return {}
    totp = _generate_totp(_tf_server_time_ms())
    headers = {"jwttoken": jwt, "accesstoken": totp}
    try:
        resp = requests.get(f"{_TF_BASE}/data/sector_scope", headers=headers,
                            timeout=10, verify=_TF_SSL_VERIFY)
        data = resp.json()
    except Exception as e:
        log.warning(f"fetch_sector_scope: request failed: {e}")
        return {}
    if data.get("status") != "SUCCESS":
        log.warning(f"fetch_sector_scope: API error {data.get('code')} — {data.get('message')}")
        return {}
    all_sector = ((data.get("payload") or {}).get("data") or {}).get("all_sector") or {}
    result: dict[str, dict] = {}
    for raw_key, stock_dict in all_sector.items():
        if not isinstance(stock_dict, dict):
            continue
        # Canonical name: strip "_r_factor" suffix, uppercase
        sector_name = raw_key.removesuffix("_r_factor").strip().upper()
        if sector_name in SECTOR_EXCLUDE:
            continue
        stocks: dict[str, dict] = {}
        for sym, entry in stock_dict.items():
            try:
                stocks[sym] = {
                    "symbol":     entry["Symbol"],
                    "ltp":        float(entry["param_0"]),
                    "prev_close": float(entry["param_1"]),
                    "change_pct": float(entry["param_2"]),
                    "r_factor":   float(entry["param_3"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        if not stocks:
            continue
        chg_vals = [s["change_pct"] for s in stocks.values()]
        rfac_vals = [s["r_factor"]   for s in stocks.values()]
        result[sector_name] = {
            "stocks":         stocks,
            "avg_change_pct": sum(chg_vals)  / len(chg_vals),
            "avg_r_factor":   sum(rfac_vals) / len(rfac_vals),
            "stock_count":    len(stocks),
        }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# TF RANK POLLER  — snapshots TF list every TF_RANK_POLL_SEC during market hours
# ══════════════════════════════════════════════════════════════════════════════

def _save_tf_snapshot(items: list[dict], ts: str) -> None:
    """Append one ranked snapshot to the daily JSONL file."""
    ranks = {item["symbol"]: idx + 1 for idx, item in enumerate(items)}
    line  = _json.dumps({"time": ts, "ranks": ranks})
    try:
        with open(_TF_SNAPSHOT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log.warning(f"TF snapshot write failed: {e}")


def _tf_rank_poller(stop_ev: threading.Event) -> None:
    """Background thread: poll TF every TF_RANK_POLL_SEC, update rank cache + save snapshot."""
    global _tf_rank_cache
    log.info(f"TF rank poller started (interval={TF_RANK_POLL_SEC}s, cap={TF_RANK_CAP})")
    while not stop_ev.is_set():
        cm = _cur_min()
        # Only poll during market hours (avoids wasting API calls overnight)
        if SESSION_START_MIN <= cm <= SESSION_END_MIN:
            items = fetch_intraday_boost()
            if items:
                ts = _now_ist().strftime("%H:%M")
                new_ranks = {item["symbol"]: idx + 1 for idx, item in enumerate(items)}
                with _tf_rank_lock:
                    _tf_rank_cache = new_ranks
                _save_tf_snapshot(items, ts)
                log.debug(f"TF rank poll {ts}: {len(items)} stocks, "
                          f"top3={[s['symbol'] for s in items[:3]]}")
        stop_ev.wait(TF_RANK_POLL_SEC)
    log.info("TF rank poller stopped")


def start_tf_rank_poller(stop_ev: threading.Event) -> threading.Thread:
    """Start background rank poller. Call once at strategy startup."""
    t = threading.Thread(target=_tf_rank_poller, args=(stop_ev,), daemon=True, name="tf-rank-poller")
    t.start()
    return t


def get_tf_rank(symbol: str) -> Optional[int]:
    """Return current 1-based rank of symbol in TF list, or None if not ranked."""
    with _tf_rank_lock:
        return _tf_rank_cache.get(symbol)


def load_tf_snapshots(date_str: str, log_dir: str = "logs/breakout") -> list[dict]:
    """Load all TF snapshots for a given date. Returns list of {time, ranks} dicts."""
    path = os.path.join(log_dir, f"tf_snapshots_{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    snapshots = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    snapshots.append(_json.loads(line))
                except Exception:
                    pass
    return snapshots


def get_rank_at_time(snapshots: list[dict], symbol: str, hhmm: str) -> Optional[int]:
    """
    Find the rank of symbol at the snapshot closest to (but not after) hhmm.
    hhmm format: "HH:MM"  e.g. "10:05"
    Returns 1-based rank or None if symbol not in list at that time.
    """
    if not snapshots:
        return None
    best = None
    for snap in snapshots:
        if snap["time"] <= hhmm:
            best = snap
    if best is None:
        best = snapshots[0]
    return best["ranks"].get(symbol)


# ══════════════════════════════════════════════════════════════════════════════
# SECTOR SCOPE POLLER  — tracks sector rankings every SECTOR_POLL_SEC
# ══════════════════════════════════════════════════════════════════════════════

def _save_sector_snapshot(snapshot: dict) -> None:
    """Append one sector snapshot line to the daily JSONL file."""
    try:
        with open(_SECTOR_SNAPSHOT_FILE, "a", encoding="utf-8") as f:
            f.write(_json.dumps(snapshot, default=list) + "\n")
    except Exception as e:
        log.warning(f"Sector snapshot write failed: {e}")


def build_sector_snapshot(sector_data: dict, ts: str | None = None,
                          prev_scores: dict | None = None) -> dict:
    """
    Pure function — turn raw fetch_sector_scope() output into the JSONL snapshot dict
    used by both the live poller and the standalone capture daemon. No side effects,
    no _sector_cache access. Produces the exact fields the replay engine reads.
    """
    if ts is None:
        ts = _now_ist().strftime("%H:%M")
    prev_scores = prev_scores or {}

    sorted_sectors = sorted(sector_data.items(),
                            key=lambda kv: (kv[1]["avg_change_pct"], kv[1]["avg_r_factor"]),
                            reverse=True)
    total = len(sorted_sectors)

    sector_scores: dict[str, float] = {}
    sector_ranks:  dict[str, int]   = {}
    stock_sectors: dict[str, list]  = {}
    for rank_idx, (sname, sinfo) in enumerate(sorted_sectors, 1):
        sector_scores[sname] = round(sinfo["avg_change_pct"], 4)
        sector_ranks[sname]  = rank_idx
        for sym in sinfo["stocks"]:
            stock_sectors.setdefault(sym, []).append(sname)

    long_sectors  = {s for s, r in sector_ranks.items() if r <= SECTOR_TOP_N_LONG}
    short_sectors = {s for s, r in sector_ranks.items() if r >= total - SECTOR_TOP_N_SHORT + 1}

    stock_chg: dict[str, float] = {}
    stock_rfac: dict[str, float] = {}
    sector_breadth: dict[str, float] = {}
    for sname, sinfo in sector_data.items():
        stocks = sinfo["stocks"]
        for sym, st in stocks.items():
            if sym not in stock_chg:
                stock_chg[sym]  = st["change_pct"]
                stock_rfac[sym] = st["r_factor"]
        if stocks and (sname in long_sectors or sname in short_sectors):
            is_long = sname in long_sectors
            aligned = sum(1 for st in stocks.values()
                          if (st["change_pct"] > 0 if is_long else st["change_pct"] < 0))
            sector_breadth[sname] = round(aligned / len(stocks) * 100, 1)

    top_n: dict[str, list] = {}
    if SECTOR_TOP_STOCKS_N > 0:
        for sname in long_sectors | short_sectors:
            if sname in sector_data:
                stk = sector_data[sname]["stocks"]
                ranked = sorted(stk.items(), key=lambda kv: kv[1]["r_factor"], reverse=True)
                top_n[sname] = [sym for sym, _ in ranked[:SECTOR_TOP_STOCKS_N]]

    trajectory: dict[str, dict] = {}
    for sname in sector_ranks:
        ps = prev_scores.get(sname)
        trajectory[sname] = {"score_delta": round(sector_scores[sname] - ps, 4)
                             if ps is not None else 0}

    return {
        "time":             ts,
        "sector_scores":    sector_scores,
        "sector_ranks":     sector_ranks,
        "long_sectors":     sorted(long_sectors),
        "short_sectors":    sorted(short_sectors),
        "stock_sectors":    stock_sectors,
        "stock_change_pct": stock_chg,
        "stock_rfactor":    stock_rfac,
        "sector_breadth":   sector_breadth,
        "sector_top_n":     top_n,
        "trajectory":       trajectory,
        "total_sectors":    total,
    }


def _sector_poller(stop_ev: threading.Event) -> None:
    """Background thread: poll sector scope every SECTOR_POLL_SEC, update cache + save snapshot."""
    log.info(f"Sector poller started (interval={SECTOR_POLL_SEC}s, "
             f"top_long={SECTOR_TOP_N_LONG}, top_short={SECTOR_TOP_N_SHORT})")
    prev_ranks: dict[str, int] = {}   # previous poll's sector ranks for trajectory delta

    while not stop_ev.is_set():
        cm = _cur_min()
        if SESSION_START_MIN <= cm <= SESSION_END_MIN:
            sector_data = fetch_sector_scope()
            if not sector_data:
                log.warning("Sector poller: fetch_sector_scope returned empty — keeping stale cache")
            else:
                ts = _now_ist().strftime("%H:%M")

                # Rank sectors by avg_change_pct descending (rank 1 = top gainer)
                sorted_sectors = sorted(sector_data.items(),
                                        key=lambda kv: (kv[1]["avg_change_pct"],
                                                         kv[1]["avg_r_factor"]),
                                        reverse=True)
                total = len(sorted_sectors)

                sector_scores: dict[str, float] = {}
                sector_ranks:  dict[str, int]   = {}
                stock_sectors: dict[str, list]  = {}
                traj_updates:  dict[str, dict]  = {}

                for rank_idx, (sname, sinfo) in enumerate(sorted_sectors, 1):
                    sector_scores[sname] = round(sinfo["avg_change_pct"], 4)
                    sector_ranks[sname]  = rank_idx
                    for sym in sinfo["stocks"]:
                        stock_sectors.setdefault(sym, []).append(sname)
                    # Trajectory delta
                    prev_r = prev_ranks.get(sname)
                    traj_updates[sname] = {
                        "rank_delta":  (rank_idx - prev_r) if prev_r is not None else 0,
                        "prev_rank":   prev_r,
                        "score_delta": round(sinfo["avg_change_pct"] - (
                            _sector_cache["sector_scores"].get(sname, sinfo["avg_change_pct"])), 4),
                    }

                long_sectors  = {s for s, r in sector_ranks.items() if r <= SECTOR_TOP_N_LONG}
                short_sectors = {s for s, r in sector_ranks.items()
                                 if r >= total - SECTOR_TOP_N_SHORT + 1}

                # Build per-stock change% and r_factor maps (first-seen value across sectors)
                stock_chg:  dict[str, float] = {}
                stock_rfac: dict[str, float] = {}
                sector_breadth: dict[str, float] = {}
                for sname, sinfo in sector_data.items():
                    for sym, st in sinfo["stocks"].items():
                        if sym not in stock_chg:
                            stock_chg[sym]  = st["change_pct"]
                            stock_rfac[sym] = st["r_factor"]
                    # Compute breadth: % of stocks moving with sector direction
                    stocks = sinfo["stocks"]
                    if stocks:
                        is_long_sector = sname in long_sectors
                        is_short_sector = sname in short_sectors
                        if is_long_sector or is_short_sector:
                            if is_long_sector:
                                aligned_n = sum(1 for st in stocks.values() if st["change_pct"] > 0)
                            else:
                                aligned_n = sum(1 for st in stocks.values() if st["change_pct"] < 0)
                            sector_breadth[sname] = round(aligned_n / len(stocks) * 100, 1)

                # Count how many ACTIVE sectors each stock belongs to (multi-sector priority)
                active_secs = long_sectors | short_sectors
                stock_active: dict[str, int] = {}
                for sym, secs in stock_sectors.items():
                    stock_active[sym] = sum(1 for s in secs if s in active_secs)

                # Reversal tracking — update consecutive-poll counter per sector
                # A sector that was active and is now beyond active_range+buffer increments
                # its counter. Coming back within range resets it to 0.
                long_threshold  = SECTOR_TOP_N_LONG  + SECTOR_REVERSAL_BUFFER
                short_threshold = total - SECTOR_TOP_N_SHORT - SECTOR_REVERSAL_BUFFER + 1
                with _sector_lock:
                    ever_active   = set(_sector_cache["ever_active_today"])
                    rev_count     = dict(_sector_cache["reversal_count"])
                    prev_short    = set(_sector_cache["short_sectors"])

                # Grow the ever-active set — sectors that have been in top-N at any point today
                ever_active |= long_sectors | short_sectors

                new_rev_count: dict[str, int] = {}
                new_confirmed: set[str]        = set()

                # Track every sector that was EVER active today — this ensures a sector
                # that temporarily dropped out of top-N (but had open positions) is still
                # tracked and can confirm a reversal across polls
                for sname in ever_active:
                    rank     = sector_ranks.get(sname, total)
                    still_ok = (sname in long_sectors) or (sname in short_sectors)

                    if still_ok:
                        new_rev_count[sname] = 0  # back in active range — reset
                    else:
                        was_long    = sname not in prev_short
                        rank_in_buf = (
                            (was_long  and rank <= long_threshold) or
                            (not was_long and rank >= short_threshold)
                        )
                        if rank_in_buf:
                            new_rev_count[sname] = 0  # minor drift — noise, ignore
                        else:
                            new_rev_count[sname] = rev_count.get(sname, 0) + 1

                        if new_rev_count[sname] >= SECTOR_REVERSAL_POLLS:
                            new_confirmed.add(sname)
                            log.info(f"Sector REVERSAL confirmed: {sname} "
                                     f"(rank={rank}, {new_rev_count[sname]} polls outside buffer)")

                with _sector_lock:
                    _sector_cache["snapshot_time"]        = ts
                    _sector_cache["sector_scores"]        = sector_scores
                    _sector_cache["sector_ranks"]         = sector_ranks
                    _sector_cache["stock_sectors"]        = stock_sectors
                    _sector_cache["stock_change_pct"]     = stock_chg
                    _sector_cache["stock_rfactor"]        = stock_rfac
                    # Build top-N stocks per active sector by r_factor
                    top_n: dict[str, set] = {}
                    if SECTOR_TOP_STOCKS_N > 0:
                        for sname in long_sectors | short_sectors:
                            if sname not in sector_data:
                                continue
                            stk = sector_data[sname]["stocks"]
                            ranked = sorted(stk.items(), key=lambda kv: kv[1]["r_factor"], reverse=True)
                            top_n[sname] = {sym for sym, _ in ranked[:SECTOR_TOP_STOCKS_N]}
                    _sector_cache["stock_active_sectors"]  = stock_active
                    _sector_cache["sector_breadth"]        = sector_breadth
                    _sector_cache["sector_top_n_stocks"]   = top_n
                    _sector_cache["long_sectors"]         = long_sectors
                    _sector_cache["short_sectors"]        = short_sectors
                    _sector_cache["ever_active_today"]    = ever_active
                    _sector_cache["reversal_count"]       = new_rev_count
                    _sector_cache["confirmed_reversed"]   = new_confirmed
                    for sname, entry in traj_updates.items():
                        lst = _sector_cache["trajectory"].setdefault(sname, [])
                        lst.append({"time": ts, "rank": sector_ranks[sname],
                                    "score": sector_scores[sname], **entry})
                        if len(lst) > 78:   # cap at one full session
                            lst.pop(0)

                prev_ranks = dict(sector_ranks)

                # Build snapshot for JSONL
                snapshot = {
                    "time":          ts,
                    "sector_scores": sector_scores,
                    "sector_ranks":  sector_ranks,
                    "long_sectors":  sorted(long_sectors),
                    "short_sectors": sorted(short_sectors),
                    "stock_sectors":    stock_sectors,
                    "stock_change_pct": stock_chg,
                    "stock_rfactor":    stock_rfac,
                    "sector_breadth":   sector_breadth,
                    "sector_top_n":     {k: sorted(v) for k, v in top_n.items()},
                    "trajectory":       {s: traj_updates[s] for s in traj_updates},
                    "total_sectors":    total,
                }
                _save_sector_snapshot(snapshot)

                top3  = [s for s, _ in sorted_sectors[:3]]
                bot3  = [s for s, _ in sorted_sectors[-3:]]
                skipped_breadth = [s for s in (long_sectors|short_sectors)
                                   if sector_breadth.get(s, 100) < SECTOR_MIN_BREADTH]
                log.debug(f"Sector poll {ts}: {total} sectors  "
                          f"long={sorted(long_sectors)}  short={sorted(short_sectors)}  "
                          f"breadth={sector_breadth}  "
                          f"low_breadth_skip={skipped_breadth}")

        stop_ev.wait(SECTOR_POLL_SEC)
    log.info("Sector poller stopped")


def start_sector_poller(stop_ev: threading.Event) -> threading.Thread:
    """Start background sector scope poller. Call once at strategy startup."""
    t = threading.Thread(target=_sector_poller, args=(stop_ev,), daemon=True, name="sector-poller")
    t.start()
    return t


def get_sector_gate(symbol: str, direction: str) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    allowed=True → sector filter permits this direction for this symbol.
    When SECTOR_FILTER_ENABLED=False always returns (True, "disabled").
    """
    if not SECTOR_FILTER_ENABLED:
        return True, "disabled"
    # Trust-after gate: sector data is unreliable before SECTOR_TRUST_AFTER_MIN
    if _cur_min() < SECTOR_TRUST_AFTER_MIN:
        return True, f"pre_trust_time(wait_till_{SECTOR_TRUST_AFTER_HM})"
    with _sector_lock:
        if not _sector_cache["snapshot_time"]:
            return True, "no_data_yet"
        memberships = _sector_cache["stock_sectors"].get(symbol, [])
        if not memberships:
            return False, "not_in_any_sector"
        long_secs      = _sector_cache["long_sectors"]
        short_secs     = _sector_cache["short_sectors"]
        stock_chg      = _sector_cache["stock_change_pct"].get(symbol)
        stock_rfac     = _sector_cache["stock_rfactor"].get(symbol)
        sector_breadth = _sector_cache["sector_breadth"]
        top_n_sets     = _sector_cache.get("sector_top_n_stocks", {})
    if direction == "LONG":
        qualifying = [s for s in memberships if s in long_secs]
    else:
        qualifying = [s for s in memberships if s in short_secs]
    if SECTOR_MULTI_SECTOR_RULE == "all" and len(qualifying) < len(memberships):
        return False, f"sector_rule_all_failed({','.join(memberships)})"
    if not qualifying:
        return False, f"sector_mismatch({','.join(memberships)})"
    # Breadth filter: skip incoherent sectors where stocks move in mixed directions
    if SECTOR_MIN_BREADTH > 0:
        breadths = [sector_breadth.get(s, 100.0) for s in qualifying]
        min_b = min(breadths)
        if min_b < SECTOR_MIN_BREADTH:
            return False, f"low_breadth({min_b:.0f}%<{SECTOR_MIN_BREADTH:.0f}%)"
    # Top-N filter: only trade the highest r_factor stocks within each sector
    if SECTOR_TOP_STOCKS_N > 0 and top_n_sets:
        in_top_n = any(symbol in top_n_sets.get(s, set()) for s in qualifying)
        if not in_top_n:
            return False, f"not_in_top{SECTOR_TOP_STOCKS_N}(rf={stock_rfac:.2f})"
    # Stock-level alignment: skip stocks moving against their sector direction
    if SECTOR_STOCK_ALIGN and stock_chg is not None:
        if direction == "LONG"  and stock_chg < 0:
            return False, f"stock_negative({stock_chg:+.2f}%)_in_long_sector"
        if direction == "SHORT" and stock_chg > 0:
            return False, f"stock_positive({stock_chg:+.2f}%)_in_short_sector"
    # r_factor filter: skip stocks with weak TF momentum score
    if SECTOR_MIN_RFACTOR > 0 and stock_rfac is not None and stock_rfac < SECTOR_MIN_RFACTOR:
        return False, f"rfactor_weak({stock_rfac:.3f}<{SECTOR_MIN_RFACTOR})"
    chg_str  = f"stock={stock_chg:+.2f}%" if stock_chg is not None else ""
    rfac_str = f"rf={stock_rfac:.2f}"     if stock_rfac is not None else ""
    return True, f"sector_ok({qualifying[0]} {chg_str} {rfac_str})".strip()


_CprInfo = tuple[bool, float, float, float]   # (is_narrow, width_pct, bottom, top)
_cpr_cache: dict[str, tuple[str, _CprInfo]] = {}   # symbol -> (date, info)


def _get_cpr(symbol: str, today: str) -> Optional[_CprInfo]:
    """
    Fetches (or returns cached) prior-day CPR for symbol: (is_narrow, width_pct,
    bottom, top). CPR is a static daily value, so it's cached per symbol per
    day — the daily bar is fetched at most once per symbol per trading day.
    Returns None if the daily bar can't be fetched/parsed.
    """
    cached = _cpr_cache.get(symbol)
    if cached and cached[0] == today:
        return cached[1]

    df = fetch_history(symbol, "1d", 5)
    df = _normalise(df) if df is not None else None
    if df is None or len(df) < 2:
        return None

    prev = df.iloc[-2] if df.iloc[-1]["_date"] == today else df.iloc[-1]
    prev_h, prev_l, prev_c = float(prev["high"]), float(prev["low"]), float(prev["close"])
    cp = (prev_h + prev_l + prev_c) / 3
    bc = (prev_h + prev_l) / 2
    tc = (cp - bc) + cp
    bottom, top = (bc, tc) if bc <= tc else (tc, bc)
    width_pct = (top - bottom) / cp * 100 if cp else 999.0
    narrow = width_pct <= CPR_NARROW_PCT

    info: _CprInfo = (narrow, width_pct, bottom, top)
    _cpr_cache[symbol] = (today, info)
    return info


def get_cpr_gate(symbol: str, today: str) -> tuple[bool, str]:
    """
    Returns (allowed, reason). allowed=True -> prior-day CPR is narrow enough to trade.
    When CPR_FILTER_ENABLED=False always returns (True, "disabled").
    """
    if not CPR_FILTER_ENABLED:
        return True, "disabled"

    info = _get_cpr(symbol, today)
    if info is None:
        return False, "cpr_no_data"
    narrow, width_pct, _bottom, _top = info
    reason = (f"cpr_narrow({width_pct:.2f}%<={CPR_NARROW_PCT}%)" if narrow
              else f"cpr_wide({width_pct:.2f}%>{CPR_NARROW_PCT}%)")
    return narrow, reason


def get_cpr_position_gate(symbol: str, price: float, direction: str, today: str) -> tuple[bool, str]:
    """
    Directional CPR confirmation: a LONG signal is only allowed if price is
    ABOVE the CPR top (TC), a SHORT signal only if price is BELOW the CPR
    bottom (BC). Price sitting inside the CPR band (between BC and TC) has no
    directional edge either way, so both directions are blocked there.
    When CPR_POSITION_FILTER_ENABLED=False always returns (True, "disabled").
    """
    if not CPR_POSITION_FILTER_ENABLED:
        return True, "disabled"

    info = _get_cpr(symbol, today)
    if info is None:
        return False, "cpr_no_data"
    _narrow, _width_pct, bottom, top = info

    if bottom <= price <= top:
        return False, f"cpr_inside_band({price:.2f} in [{bottom:.2f},{top:.2f}])"
    if direction == "LONG":
        allowed = price > top
        reason = (f"cpr_above_top({price:.2f}>{top:.2f})" if allowed
                   else f"cpr_below_top({price:.2f}<={top:.2f})")
    else:
        allowed = price < bottom
        reason = (f"cpr_below_bottom({price:.2f}<{bottom:.2f})" if allowed
                   else f"cpr_above_bottom({price:.2f}>={bottom:.2f})")
    return allowed, reason


def load_sector_snapshots(date_str: str, log_dir: str = "logs/breakout") -> list[dict]:
    """Load all sector snapshots for a given date from the JSONL file."""
    path = os.path.join(log_dir, f"sector_snapshots_{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    snapshots = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    snapshots.append(_json.loads(line))
                except Exception:
                    pass
    return snapshots


def get_sector_at_time(
    snapshots: list[dict], symbol: str, hhmm: str, direction: str
) -> tuple[bool, str]:
    """
    Returns (allowed, reason) for sector filter using the closest snapshot <= hhmm.
    Used in sweep replay for honest backtesting (no live cache needed).
    """
    if not snapshots:
        return True, "no_snapshots"
    best = None
    for snap in snapshots:
        if snap["time"] <= hhmm:
            best = snap
    if best is None:
        best = snapshots[0]
    memberships = best.get("stock_sectors", {}).get(symbol, [])
    if not memberships:
        return False, "not_in_sector"
    if direction == "LONG":
        qualifying = [s for s in memberships if s in best.get("long_sectors", [])]
    else:
        qualifying = [s for s in memberships if s in best.get("short_sectors", [])]
    if not qualifying:
        return False, f"mismatch({','.join(memberships)})"
    # Breadth filter — skip incoherent sectors (uses saved breadth from snapshot)
    if SECTOR_MIN_BREADTH > 0:
        breadths = [best.get("sector_breadth", {}).get(s, 100.0) for s in qualifying]
        if min(breadths) < SECTOR_MIN_BREADTH:
            return False, f"low_breadth({min(breadths):.0f}%)"
    # Top-N filter — only the strongest r_factor stocks in the sector (saved sector_top_n)
    if SECTOR_TOP_STOCKS_N > 0:
        top_n = best.get("sector_top_n", {})
        if top_n and not any(symbol in top_n.get(s, []) for s in qualifying):
            return False, f"not_in_top{SECTOR_TOP_STOCKS_N}"
    # Stock-level alignment check (uses saved change_pct from snapshot)
    stock_chg  = best.get("stock_change_pct", {}).get(symbol)
    stock_rfac = best.get("stock_rfactor", {}).get(symbol)
    if stock_chg is not None:
        if direction == "LONG"  and stock_chg < 0:
            return False, f"stock_negative({stock_chg:+.2f}%)"
        if direction == "SHORT" and stock_chg > 0:
            return False, f"stock_positive({stock_chg:+.2f}%)"
    if SECTOR_MIN_RFACTOR > 0 and stock_rfac is not None and stock_rfac < SECTOR_MIN_RFACTOR:
        return False, f"rfactor_weak({stock_rfac:.3f})"
    return True, f"ok({qualifying[0]})"


# ══════════════════════════════════════════════════════════════════════════════
# OPENALGO CLIENT + DATA
# ══════════════════════════════════════════════════════════════════════════════
_client: Optional[openalgo_api] = None


def _get_client() -> openalgo_api:
    global _client
    if _client is None:
        _client = openalgo_api(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST, ws_url=OPENALGO_WS_URL)
    return _client


def _analyzer_mode_on() -> Optional[bool]:
    """
    Query OpenAlgo's analyzer/sandbox state. Returns True if analyze_mode is ON (orders are
    paper-filled by the sandbox, not sent to the broker), False if OFF (LIVE orders!), or
    None if the status could not be determined.
    """
    try:
        r = requests.post(f"{OPENALGO_HOST}/api/v1/analyzer",
                          json={"apikey": OPENALGO_API_KEY}, timeout=8)
        data = r.json()
        if data.get("status") == "success":
            return bool(data.get("data", {}).get("analyze_mode"))
    except Exception as e:
        log.warning(f"analyzer status check failed: {e}")
    return None


def fetch_history(symbol: str, interval: str, days: int) -> Optional[pd.DataFrame]:
    end = date.today()
    start = end - timedelta(days=days)
    api_interval = {"1d": "D", "1w": "W", "1M": "M"}.get(interval, interval)
    try:
        df = _get_client().history(symbol=symbol, exchange=EXCHANGE, interval=api_interval,
                                   start_date=start.strftime("%Y-%m-%d"),
                                   end_date=end.strftime("%Y-%m-%d"))
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        return df.reset_index()   # SDK returns timestamp as DatetimeIndex
    except Exception as e:
        log.warning(f"history({symbol} {interval}): {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS  (hand-rolled — no extra deps; VWAP resets daily, ADX is Wilder)
# ══════════════════════════════════════════════════════════════════════════════

def _normalise(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    dt_col = next((c for c in ("timestamp", "datetime", "date") if c in df.columns), None)
    if dt_col is None:
        return None
    df = df.copy()
    df[dt_col] = pd.to_datetime(df[dt_col], utc=False)
    if getattr(df[dt_col].dt, "tz", None) is not None:
        df[dt_col] = df[dt_col].dt.tz_localize(None)
    df = df.sort_values(dt_col).reset_index(drop=True)
    df["_dt"] = df[dt_col]
    df["_date"] = df[dt_col].dt.strftime("%Y-%m-%d")
    df["_hm"] = df[dt_col].dt.hour * 60 + df[dt_col].dt.minute
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Intraday VWAP, reset each day (typical price x volume, cumulative)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    cum_pv = pv.groupby(df["_date"]).cumsum()
    cum_v = df["volume"].groupby(df["_date"]).cumsum().replace(0, np.nan)
    df["vwap"] = (cum_pv / cum_v).astype(float)
    return df


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder ADX (approximated with ewm alpha=1/period)."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    up = h.diff()
    dn = -l.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    alpha = 1.0 / period
    tr = tr.astype(float)
    plus_dm = plus_dm.astype(float)
    minus_dm = minus_dm.astype(float)
    atr = tr.ewm(alpha=alpha, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean().astype(float)
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder ATR (ta.atr in Pine) — used for ATR-based SL."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    alpha = 1.0 / period
    df["atr"] = tr.astype(float).ewm(alpha=alpha, adjust=False).mean()
    return df


def _vwap_chop_ok(df: pd.DataFrame, sig_idx: int) -> bool:
    """Pine g12: suppress signal if price touched VWAP > MAX_TOUCHES times in last N bars."""
    if not USE_VWAP_CHOP or "vwap" not in df.columns:
        return True
    start = max(0, sig_idx - VWAP_CHOP_LOOKBACK + 1)
    w = df.iloc[start: sig_idx + 1]
    touches = int(((w["high"] >= w["vwap"]) & (w["low"] <= w["vwap"])).sum())
    return touches <= VWAP_CHOP_MAX_TOUCHES


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL  (Pine buy1/buy2/sell1/sell2 + VWAP + ADX, on the just-closed bar)
# ══════════════════════════════════════════════════════════════════════════════

def _hm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[2:])


SESSION_START_MIN = _hm(SESSION_START_HM)
SESSION_END_MIN   = EXIT_HOUR * 60 + EXIT_MINUTE
ENTRY_CUTOFF_MIN  = _hm(ENTRY_CUTOFF_HM)


def _parse_interval_min(s: str) -> int:
    """Parse '5m' → 5, '15m' → 15, '1h' → 60."""
    s = s.lower()
    if s.endswith("m"):
        return int(s[:-1])
    if s.endswith("h"):
        return int(s[:-1]) * 60
    return 5


_INTERVAL_MIN = _parse_interval_min(INTERVAL)


def evaluate_signal(df5: pd.DataFrame, today: str) -> Optional[dict]:
    """
    Evaluate the Pine breakout on the LAST CLOSED bar of today's session.
    Returns {direction, entry_ref, sl, signal_ts, adx, vwap} or None.

    Drops the forming bar only when the clock confirms it is still in progress,
    so we never repaint mid-bar but also don't discard the signal bar when the
    API returns only closed bars right after a candle close.
    """
    df = _normalise(df5)
    if df is None or df.empty:
        return None
    # Warm ATR on ALL fetched data before filtering to today (cold-start on ~30 bars is 2x off).
    if USE_ATR_SL:
        df = add_atr(df, ATR_LEN)
    df = df[(df["_date"] == today) &
            (df["_hm"] >= SESSION_START_MIN) & (df["_hm"] <= SESSION_END_MIN)]
    if len(df) < 4:
        return None

    df = add_vwap(df)
    df = add_adx(df, ADX_LEN)

    # Drop the last bar only when it's still forming (clock-time check).
    # After bar close the API sometimes returns only closed bars (no partial next bar),
    # so blindly dropping iloc[-1] would discard the signal bar itself → late entry.
    last_hm = int(df.iloc[-1]["_hm"])
    if _cur_min() < last_hm + _INTERVAL_MIN:
        df = df.iloc[:-1]
    if len(df) < 3:
        return None

    b  = df.iloc[-1]   # closed signal bar
    p1 = df.iloc[-2]
    p2 = df.iloc[-3]

    # Only generate within the entry window
    if not (SESSION_START_MIN <= int(b["_hm"]) <= ENTRY_CUTOFF_MIN):
        return None

    o, h, c, lo = float(b["open"]), float(b["high"]), float(b["close"]), float(b["low"])
    o1, h1, c1, l1 = float(p1["open"]), float(p1["high"]), float(p1["close"]), float(p1["low"])
    o2, h2, c2, l2 = float(p2["open"]), float(p2["high"]), float(p2["close"]), float(p2["low"])

    is_green, is_red = c > o, c < o
    prev_green, prev_red = c1 > o1, c1 < o1
    prev2_green, prev2_red = c2 > o2, c2 < o2

    buy1  = ENABLE_SINGLE and ENABLE_BUY  and is_green and prev_red   and c > h1
    sell1 = ENABLE_SINGLE and ENABLE_SELL and is_red   and prev_green and c < l1
    buy2  = ENABLE_DOUBLE and ENABLE_BUY  and is_green and prev_green and prev2_red   and c > h2
    sell2 = ENABLE_DOUBLE and ENABLE_SELL and is_red   and prev_red   and prev2_green and c < l2

    vwap = float(b["vwap"]) if pd.notna(b["vwap"]) else None
    adx  = float(b["adx"]) if pd.notna(b["adx"]) else 0.0
    atr  = float(b["atr"]) if USE_ATR_SL and "atr" in b.index and pd.notna(b["atr"]) else None

    vwap_buy_ok  = (not USE_VWAP) or (vwap is not None and c > vwap)
    vwap_sell_ok = (not USE_VWAP) or (vwap is not None and c < vwap)
    adx_ok  = (not USE_ADX) or (adx >= ADX_THRESHOLD)
    chop_ok = _vwap_chop_ok(df, len(df) - 1)

    def _sl_long(candle_sl):
        if USE_ATR_SL and atr is not None:
            return round(c - atr * ATR_MULT, 2)
        return candle_sl

    def _sl_short(candle_sl):
        if USE_ATR_SL and atr is not None:
            return round(c + atr * ATR_MULT, 2)
        return candle_sl

    if (buy1 or buy2) and vwap_buy_ok and adx_ok and chop_ok:
        candle_sl = min(lo, l1) if buy1 else min(lo, l1, l2)
        sl = _sl_long(candle_sl)
        return {"direction": "LONG", "entry_ref": c, "sl": sl,
                "signal_ts": str(b["_dt"]), "adx": adx, "vwap": vwap,
                "kind": "single" if buy1 else "double"}

    if (sell1 or sell2) and vwap_sell_ok and adx_ok and chop_ok:
        candle_sl = max(h, h1) if sell1 else max(h, h1, h2)
        sl = _sl_short(candle_sl)
        return {"direction": "SHORT", "entry_ref": c, "sl": sl,
                "signal_ts": str(b["_dt"]), "adx": adx, "vwap": vwap,
                "kind": "single" if sell1 else "double"}

    return None


# ══════════════════════════════════════════════════════════════════════════════
# SIZING  (Pine risk calculator: qty = risk_budget / stop_distance, notional-capped)
# ══════════════════════════════════════════════════════════════════════════════

def compute_qty(entry: float, sl: float) -> int:
    sl_dist = abs(entry - sl)
    if sl_dist < 0.01 or entry <= 0:
        return 0
    risk_budget = BALANCE * (MAX_LOSS_PCT / 100.0)        # e.g. 311000 * 1% = 3110
    qty_risk = math.floor(risk_budget / sl_dist)          # risk constraint
    qty_cap = math.floor(CAPITAL_PER_TRADE / entry)       # notional constraint
    return max(0, min(qty_risk, qty_cap))


# ══════════════════════════════════════════════════════════════════════════════
# CHARGES  (Zerodha intraday equity — for the EOD summary)
# ══════════════════════════════════════════════════════════════════════════════

def compute_charges(entry: float, exit_p: float, qty: int) -> float:
    buy_v, sell_v = entry * qty, exit_p * qty
    total = buy_v + sell_v
    brok = min(20.0, buy_v * 0.0003) + min(20.0, sell_v * 0.0003)
    stt = sell_v * 0.00025
    txn = total * 0.0000307
    sebi = total * 0.000001
    stamp = buy_v * 0.00003
    gst = (brok + sebi + txn) * 0.18
    return brok + stt + txn + sebi + stamp + gst


def _breakeven_price(entry: float, qty: int, direction: str) -> float:
    """
    Breakeven SL price. With BREAKEVEN_COVER_CHARGES, shifts entry by charges-per-share
    so a stop-out at this level nets exactly zero (charges covered) instead of -charges.
    LONG  → entry + charges/qty  (SL slightly higher → covers cost on exit)
    SHORT → entry - charges/qty  (SL slightly lower  → covers cost on exit)
    """
    if not BREAKEVEN_COVER_CHARGES or qty <= 0:
        return entry
    per_share = compute_charges(entry, entry, qty) / qty
    return round(entry + per_share, 2) if direction == "LONG" else round(entry - per_share, 2)


# ══════════════════════════════════════════════════════════════════════════════
# SEBI COMPLIANCE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, delay: float = ORDER_DELAY_S):
        self._delay, self._last = delay, 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last = time.monotonic()


_rate_limiter = RateLimiter()


def _pre_trade_check(symbol, direction, qty, entry, sl) -> tuple[bool, str]:
    sl_dist = abs(entry - sl)
    if sl_dist < 0.01:
        return False, "SL distance < 0.01 (degenerate candle)"
    if direction == "LONG" and entry <= sl:
        return False, f"LONG entry {entry:.2f} <= SL {sl:.2f}"
    if direction == "SHORT" and entry >= sl:
        return False, f"SHORT entry {entry:.2f} >= SL {sl:.2f}"
    if qty <= 0:
        return False, "qty = 0"
    if qty * entry > CAPITAL_PER_TRADE * 1.05:
        return False, f"capital Rs.{qty*entry:,.0f} exceeds limit Rs.{CAPITAL_PER_TRADE:,.0f}"
    sl_pct = (sl_dist / entry) * 100
    if MIN_SL_DIST_PCT > 0 and sl_pct < MIN_SL_DIST_PCT:
        return False, f"SL distance {sl_pct:.2f}% < MIN_SL_DIST_PCT {MIN_SL_DIST_PCT}% (charges > profit)"
    return True, "OK"


# ══════════════════════════════════════════════════════════════════════════════
# ORDER PLUMBING
# ══════════════════════════════════════════════════════════════════════════════

def _place_order(symbol, action, qty, price_type="MARKET", price=0.0, trigger_price=0.0):
    _rate_limiter.wait()
    if DRY_RUN:
        fake = f"DRY-{symbol}-{action}-{int(time.time())}"
        log.info(f"[DRY] {action} {qty} {symbol} {price_type} tp={trigger_price:.2f} -> {fake}")
        return fake
    try:
        r = _get_client().placeorder(strategy=STRATEGY_NAME, symbol=symbol, exchange=EXCHANGE,
                                     action=action, quantity=qty, price_type=price_type,
                                     product=PRODUCT, price=price, trigger_price=trigger_price)
        if r.get("status") == "success":
            log.info(f"ORDER {action} {qty} {symbol} {price_type} tp={trigger_price:.2f} -> {r['orderid']}")
            return r["orderid"]
        log.error(f"Order FAIL {action} {symbol}: {r}")
    except Exception as e:
        log.error(f"placeorder exception {symbol}: {e}")
    return None


def _modify_sl(order_id, trigger_price, symbol, sl_action, qty) -> bool:
    if DRY_RUN:
        log.info(f"[DRY] MODIFY {order_id} {symbol} -> tp={trigger_price:.2f}")
        return True
    try:
        r = _get_client().modifyorder(order_id=order_id, strategy=STRATEGY_NAME, symbol=symbol,
                                      action=sl_action, exchange=EXCHANGE, price_type="SL-M",
                                      product=PRODUCT, quantity=qty, price="0",
                                      trigger_price=str(round(trigger_price, 2)),
                                      disclosed_quantity="0")
        return r.get("status") == "success"
    except Exception as e:
        log.warning(f"modifyorder exception {order_id} ({symbol}): {e}")
        return False


def _cancel_order(order_id) -> bool:
    if DRY_RUN or not order_id or str(order_id).startswith("DRY-"):
        return True
    try:
        return _get_client().cancelorder(order_id=order_id, strategy=STRATEGY_NAME).get("status") == "success"
    except Exception as e:
        log.warning(f"cancelorder exception {order_id}: {e}")
        return False


def _order_status(order_id) -> Optional[str]:
    if not order_id or str(order_id).startswith("DRY-"):
        return None
    try:
        r = _get_client().orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
        if r.get("status") != "success":
            return None
        s = str((r.get("data") or {}).get("order_status") or "").lower()
        if "complete" in s or "filled" in s:
            return "complete"
        if "cancel" in s:
            return "cancelled"
        if "reject" in s:
            return "rejected"
        return "open"
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# POSITION + STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol: str
    direction: str            # LONG | SHORT
    entry_price: float
    qty: int
    initial_sl: float
    current_sl: float
    target: float             # 0.0 = no fixed target (time-exit only)
    entry_order_id: str
    sl_order_id: Optional[str]
    active: bool = True
    exit_price: float = 0.0
    exit_reason: str = ""
    reached_1r: bool = False
    reached_be: bool = False        # breakeven SL already activated
    max_r_reached: float = 0.0      # highest R-multiple reached so far (for step-lock)
    step_lock_sl: float = 0.0       # current step-locked SL level (0 = not yet locked)
    # Signal metadata — stored for trade log
    signal_time: str = ""     # closed candle that fired the pattern (YYYY-MM-DD HH:MM:SS)
    entry_time: str = ""      # when the entry order was placed (HH:MM:SS IST)
    signal_adx: float = 0.0
    signal_vwap: float = 0.0
    kind: str = ""            # "single" | "double"


def _save_state(positions: dict, phase: str):
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump({"date": _RUN_DATE, "phase": phase,
                        "saved_at": _now_ist().strftime("%H:%M:%S"),
                        "positions": [asdict(p) for p in positions.values()]}, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        log.warning(f"_save_state failed: {e}")


def _load_state() -> Optional[dict]:
    if not os.path.exists(_STATE_FILE):
        return None
    try:
        with open(_STATE_FILE) as f:
            data = _json.load(f)
        return data if data.get("date") == _RUN_DATE else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now_ist() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def _cur_min() -> int:
    n = _now_ist()
    return n.hour * 60 + n.minute


# ══════════════════════════════════════════════════════════════════════════════
# POSITION MANAGEMENT  (trail / target / SL-hit detection)
# ══════════════════════════════════════════════════════════════════════════════

def _manage_position(pos: Position, today: str):
    """Per loop: detect SL fill, breakeven SL, trail SL, exit at target or time."""
    if not pos.active:
        return

    # 1. Did the broker SL-M already fill?
    if pos.sl_order_id:
        is_dry_sl = str(pos.sl_order_id).startswith("DRY-")
        broker_filled = (not is_dry_sl) and _order_status(pos.sl_order_id) == "complete"

        # DRY_RUN: simulate SL hit by checking if price breached the SL level
        dry_hit = False
        if is_dry_sl:
            df_sl = fetch_history(pos.symbol, INTERVAL, 1)
            if df_sl is not None:
                d_sl = _normalise(df_sl)
                if d_sl is not None:
                    d_sl = d_sl[d_sl["_date"] == today]
                    if not d_sl.empty:
                        bar_lo = float(d_sl.iloc[-1]["low"])
                        bar_hi = float(d_sl.iloc[-1]["high"])
                        dry_hit = (pos.direction == "LONG" and bar_lo <= pos.current_sl) or \
                                  (pos.direction == "SHORT" and bar_hi >= pos.current_sl)

        if broker_filled or dry_hit:
            pos.exit_price  = pos.current_sl
            pos.exit_reason = "SL_HIT"
            pos.active      = False
            log.info(f"  {'[DRY] ' if dry_hit else ''}SL HIT {pos.symbol} @ {pos.current_sl:.2f}")
            _jlog("SL_HIT", symbol=pos.symbol, exit=pos.current_sl, qty=pos.qty, dry=dry_hit)
            _write_trade_csv(pos)
            return

    df = fetch_history(pos.symbol, INTERVAL, 1)
    if df is None:
        return
    d = _normalise(df)
    if d is None:
        return
    d = d[d["_date"] == today]
    if d.empty:
        return
    last   = d.iloc[-1]               # latest (forming) bar — fine for LTP-ish read
    closed = d.iloc[-2] if len(d) >= 2 else last
    ltp    = float(last["close"])

    # 2. Fixed target hit → exit MARKET  (skipped when TARGET_RR=0)
    if pos.target > 0:
        hit_t = (pos.direction == "LONG" and ltp >= pos.target) or \
                (pos.direction == "SHORT" and ltp <= pos.target)
        if hit_t:
            if pos.sl_order_id:
                _cancel_order(pos.sl_order_id)
                time.sleep(0.2)
            exit_action = "SELL" if pos.direction == "LONG" else "BUY"
            _place_order(pos.symbol, exit_action, pos.qty, "MARKET")
            pos.exit_price, pos.exit_reason, pos.active = ltp, "TARGET", False
            log.info(f"  TARGET {pos.symbol} @ {ltp:.2f}")
            _jlog("TARGET", symbol=pos.symbol, exit=ltp, qty=pos.qty)
            _write_trade_csv(pos)
            return

    # 3. Breakeven SL — move SL to (cost-to-cost) breakeven once trade is in profit.
    #    Preferred trigger is R-based (BREAKEVEN_START_R); falls back to BREAKEVEN_PCT.
    if not pos.reached_be and pos.sl_order_id:
        be_triggered = False
        trig_label   = ""
        if BREAKEVEN_START_R > 0:
            # R-based: use intrabar excursion (high for LONG, low for SHORT)
            dist = abs(pos.entry_price - pos.initial_sl)
            if dist > 0:
                if pos.direction == "LONG":
                    cur_r = (float(last["high"]) - pos.entry_price) / dist
                else:
                    cur_r = (pos.entry_price - float(last["low"])) / dist
                if cur_r >= BREAKEVEN_START_R:
                    be_triggered = True
                    trig_label   = f"{cur_r:.1f}R"
        elif BREAKEVEN_PCT > 0:
            be_triggered = (
                pos.direction == "LONG"  and ltp >= pos.entry_price * (1 + BREAKEVEN_PCT / 100)
            ) or (
                pos.direction == "SHORT" and ltp <= pos.entry_price * (1 - BREAKEVEN_PCT / 100)
            )
            trig_label = f"{BREAKEVEN_PCT}%"
        if be_triggered:
            sl_action = "SELL" if pos.direction == "LONG" else "BUY"
            be_sl = _breakeven_price(pos.entry_price, pos.qty, pos.direction)
            if _modify_sl(pos.sl_order_id, be_sl, pos.symbol, sl_action, pos.qty):
                log.info(f"  BREAKEVEN {pos.symbol} SL {pos.current_sl:.2f} → {be_sl:.2f} "
                         f"(trigger={trig_label}, cost-covered)")
                _jlog("BREAKEVEN", symbol=pos.symbol, old_sl=pos.current_sl,
                      new_sl=be_sl, trigger=trig_label)
                pos.current_sl = be_sl
                pos.reached_be = True

    # 3b. Sector-reversal breakeven: if the stock's sector has CONFIRMED reversed
    #     (outside active range for SECTOR_REVERSAL_POLLS consecutive polls) and
    #     current SL is still below entry (i.e. we haven't already moved to breakeven),
    #     move SL to entry so a continued reversal costs nothing.
    if (SECTOR_REVERSAL_BREAKEVEN
            and pos.sl_order_id
            and not pos.reached_be
            and pos.active):
        with _sector_lock:
            confirmed_rev = set(_sector_cache["confirmed_reversed"])
            stock_secs    = _sector_cache["stock_sectors"].get(pos.symbol, [])
        if any(s in confirmed_rev for s in stock_secs):
            sl_action = "SELL" if pos.direction == "LONG" else "BUY"
            be_sl     = _breakeven_price(pos.entry_price, pos.qty, pos.direction)
            # Only move if SL is still on the loss side of entry
            sl_is_loss = (
                (pos.direction == "LONG"  and pos.current_sl < pos.entry_price) or
                (pos.direction == "SHORT" and pos.current_sl > pos.entry_price)
            )
            if sl_is_loss and _modify_sl(pos.sl_order_id, be_sl, pos.symbol, sl_action, pos.qty):
                rev_secs = [s for s in stock_secs if s in confirmed_rev]
                log.info(f"  SECTOR-REVERSAL BE {pos.symbol} SL {pos.current_sl:.2f} → {be_sl:.2f} "
                         f"(sector reversed: {rev_secs})")
                _jlog("SECTOR_REVERSAL_BE", symbol=pos.symbol,
                      old_sl=pos.current_sl, new_sl=be_sl, reversed_sectors=rev_secs)
                pos.current_sl = be_sl
                pos.reached_be = True

    # 3c. Step-Lock Trail: ratchet SL up to (N-1)R every time price hits NR milestone
    #     SL = max(step_lock_level, candle_trail_level) — combines with existing trail
    if TRAIL_STEPS_ENABLED and pos.sl_order_id and pos.active and TRAIL_STEP_R > 0:
        dist = abs(pos.entry_price - pos.initial_sl)
        if dist > 0:
            # Current R-multiple based on intrabar high/low (best excursion this bar)
            if pos.direction == "LONG":
                best_price = float(last["high"])
                current_R  = (best_price - pos.entry_price) / dist
            else:
                best_price = float(last["low"])
                current_R  = (pos.entry_price - best_price) / dist

            if current_R > pos.max_r_reached:
                pos.max_r_reached = current_R

            # Locking only begins once price has reached TRAIL_LOCK_START_R — this keeps
            # the original SL through the noisy early pullback, then ratchets from breakeven.
            if pos.max_r_reached >= TRAIL_LOCK_START_R:
                steps  = int((pos.max_r_reached - TRAIL_LOCK_START_R) / TRAIL_STEP_R)
                lock_r = steps * TRAIL_STEP_R   # 0 at start → breakeven, then 1R, 2R, ...
                # At the 0R lock use cost-to-cost breakeven (covers charges); higher locks
                # are already deep in profit so plain entry+lock_r*dist is fine.
                if lock_r == 0.0:
                    new_step_sl = _breakeven_price(pos.entry_price, pos.qty, pos.direction)
                elif pos.direction == "LONG":
                    new_step_sl = round(pos.entry_price + lock_r * dist, 2)
                else:
                    new_step_sl = round(pos.entry_price - lock_r * dist, 2)
                if pos.direction == "LONG":
                    new_step_sl = max(new_step_sl, pos.current_sl)  # ratchet — never lower
                else:
                    new_step_sl = min(new_step_sl, pos.current_sl)  # ratchet — never higher

                sl_moved = (
                    (pos.direction == "LONG"  and new_step_sl > pos.current_sl + 0.01) or
                    (pos.direction == "SHORT" and new_step_sl < pos.current_sl - 0.01)
                )
                if sl_moved:
                    sl_action = "SELL" if pos.direction == "LONG" else "BUY"
                    if _modify_sl(pos.sl_order_id, new_step_sl, pos.symbol, sl_action, pos.qty):
                        log.info(f"  STEP-LOCK {pos.symbol} step={step_n}R "
                                 f"SL {pos.current_sl:.2f} → {new_step_sl:.2f} "
                                 f"(locked {lock_r:.1f}R, max_R={pos.max_r_reached:.2f})")
                        _jlog("STEP_LOCK", symbol=pos.symbol, step_n=step_n,
                              old_sl=pos.current_sl, new_sl=new_step_sl,
                              locked_r=lock_r, max_r=round(pos.max_r_reached, 2))
                        pos.current_sl   = new_step_sl
                        pos.step_lock_sl = new_step_sl
                        pos.reached_be   = True  # step-lock implies breakeven at minimum

    # 4. Trail SL on the last CLOSED candle (mode: none | after_1R | full)
    if TRAIL_MODE != "none" and pos.sl_order_id:
        dist = abs(pos.entry_price - pos.initial_sl)
        # mark +1R favourable excursion (intrabar) for after_1R mode
        if not pos.reached_1r and dist > 0:
            if (pos.direction == "LONG" and float(last["high"]) >= pos.entry_price + dist) or \
               (pos.direction == "SHORT" and float(last["low"]) <= pos.entry_price - dist):
                pos.reached_1r = True
        if TRAIL_MODE == "full":
            allow = (pos.direction == "LONG" and float(closed["close"]) > pos.entry_price) or \
                    (pos.direction == "SHORT" and float(closed["close"]) < pos.entry_price)
        else:  # after_1R
            allow = pos.reached_1r
        if allow:
            sl_action = "SELL" if pos.direction == "LONG" else "BUY"
            if pos.direction == "LONG":
                new_sl = max(pos.current_sl, float(closed["low"]))
                improved = new_sl > pos.current_sl + 0.01
            else:
                new_sl = min(pos.current_sl, float(closed["high"]))
                improved = new_sl < pos.current_sl - 0.01
            if improved and _modify_sl(pos.sl_order_id, new_sl, pos.symbol, sl_action, pos.qty):
                log.info(f"  TRAIL {pos.symbol} SL {pos.current_sl:.2f} -> {new_sl:.2f}")
                _jlog("TRAIL", symbol=pos.symbol, new_sl=new_sl)
                pos.current_sl = new_sl


def _process_manual_commands(positions: dict, today: str) -> None:
    """
    Read manual_commands.txt and execute commands immediately.
    Commands are removed from the file after execution.

    Supported:
        EXIT SYMBOL        — cancel SL + market exit
        MOVESL SYMBOL 564  — move the SL-M to a new price
        EXITALL            — square off everything now
    """
    if not os.path.exists(MANUAL_CMD_FILE):
        return
    try:
        with open(MANUAL_CMD_FILE, "r") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return
        remaining = []
        for line in lines:
            parts = line.upper().split()
            if not parts:
                continue
            cmd = parts[0]

            if cmd == "EXITALL":
                log.info(f"[MANUAL] EXITALL — squaring off all positions")
                _jlog("MANUAL_CMD", cmd="EXITALL")
                _square_off_all(positions, today)

            elif cmd == "EXIT" and len(parts) >= 2:
                sym = parts[1]
                pos = positions.get(sym)
                if not pos or not pos.active:
                    log.warning(f"[MANUAL] EXIT {sym}: no active position found")
                    continue
                log.info(f"[MANUAL] EXIT {sym} — cancelling SL + market exit")
                _jlog("MANUAL_CMD", cmd="EXIT", symbol=sym)
                if pos.sl_order_id:
                    _cancel_order(pos.sl_order_id)
                    time.sleep(0.3)
                exit_action = "BUY" if pos.direction == "SHORT" else "SELL"
                _place_order(sym, exit_action, pos.qty, "MARKET")
                df = fetch_history(sym, INTERVAL, 1)
                if df is not None:
                    d = _normalise(df)
                    if d is not None and not d[d["_date"] == today].empty:
                        pos.exit_price = float(d[d["_date"] == today].iloc[-1]["close"])
                pos.exit_reason = "MANUAL_EXIT"
                pos.active      = False
                _write_trade_csv(pos)
                _jlog("MANUAL_CLOSED", symbol=sym, exit=pos.exit_price,
                      gross=round((pos.exit_price - pos.entry_price) * pos.qty *
                                  (1 if pos.direction == "LONG" else -1), 2))

            elif cmd == "MOVESL" and len(parts) >= 3:
                sym = parts[1]
                try:
                    new_sl = float(parts[2])
                except ValueError:
                    log.warning(f"[MANUAL] MOVESL {sym}: invalid price '{parts[2]}'")
                    remaining.append(line)
                    continue
                pos = positions.get(sym)
                if not pos or not pos.active or not pos.sl_order_id:
                    log.warning(f"[MANUAL] MOVESL {sym}: no active position with SL order")
                    continue
                sl_action = "SELL" if pos.direction == "LONG" else "BUY"
                if _modify_sl(pos.sl_order_id, new_sl, sym, sl_action, pos.qty):
                    log.info(f"[MANUAL] MOVESL {sym}: {pos.current_sl:.2f} → {new_sl:.2f}")
                    _jlog("MANUAL_CMD", cmd="MOVESL", symbol=sym,
                          old_sl=pos.current_sl, new_sl=new_sl)
                    pos.current_sl = new_sl
                    pos.reached_be = True
                else:
                    log.error(f"[MANUAL] MOVESL {sym}: modify order failed")

            else:
                log.warning(f"[MANUAL] Unknown command: '{line}' — ignored")

        # Rewrite file with only unprocessed lines
        with open(MANUAL_CMD_FILE, "w") as f:
            for l in remaining:
                f.write(l + "\n")
        if not remaining:
            open(MANUAL_CMD_FILE, "w").close()   # clear file

    except Exception as e:
        log.exception(f"_process_manual_commands error: {e}")


def _square_off_all(positions: dict, today: str):
    log.info("Square-off — flattening all open positions")
    for pos in positions.values():
        if not pos.active:
            continue
        if pos.sl_order_id and _order_status(pos.sl_order_id) == "complete":
            pos.exit_price, pos.exit_reason, pos.active = pos.current_sl, "SL_HIT", False
            _write_trade_csv(pos)
            continue
        if pos.sl_order_id:
            _cancel_order(pos.sl_order_id)
            time.sleep(0.2)
        exit_action = "SELL" if pos.direction == "LONG" else "BUY"
        _place_order(pos.symbol, exit_action, pos.qty, "MARKET")
        df = fetch_history(pos.symbol, INTERVAL, 1)
        if df is not None:
            d = _normalise(df)
            if d is not None and not d[d["_date"] == today].empty:
                pos.exit_price = float(d[d["_date"] == today].iloc[-1]["close"])
        pos.exit_reason, pos.active = pos.exit_reason or "TIME_EXIT", False
        _jlog("SQUAREOFF", symbol=pos.symbol, exit=pos.exit_price, qty=pos.qty)
        _write_trade_csv(pos)
        time.sleep(0.3)
    _save_state(positions, "DONE")


def _daily_loss_ok(positions: dict) -> bool:
    # Closed P&L
    realised = sum((p.exit_price - p.entry_price) * p.qty * (1 if p.direction == "LONG" else -1)
                   for p in positions.values() if not p.active and p.exit_price > 0)
    # Unrealized: use current_sl as worst-case proxy for open positions
    unrealised = sum((p.current_sl - p.entry_price) * p.qty * (1 if p.direction == "LONG" else -1)
                     for p in positions.values() if p.active)
    total = realised + unrealised
    if total < -MAX_DAILY_LOSS_RS:
        log.error(f"DAILY LOSS LIMIT HIT: realised=Rs.{realised:,.0f} worst-case=Rs.{total:,.0f} "
                  f"(limit Rs.{MAX_DAILY_LOSS_RS:,.0f})")
        return False
    return True


def _config_summary() -> str:
    """Compact one-line record of the levers that drove the day (for daily_summary.csv)."""
    return (f"target={TARGET_RR}R trail={TRAIL_MODE} steps={int(TRAIL_STEPS_ENABLED)} "
            f"be_start={BREAKEVEN_START_R}R sector_only={int(SECTOR_ONLY_MODE)} "
            f"breadth={SECTOR_MIN_BREADTH:.0f} top_n={SECTOR_TOP_STOCKS_N} "
            f"min_rf={SECTOR_MIN_RFACTOR} lev={LEVERAGE:.0f}x cap={CAPITAL_PER_TRADE:.0f}")


def _hhmm_to_min(s: str) -> int:
    """Parse 'HH:MM' or 'HH:MM:SS' (optionally with date prefix) to minutes-of-day. -1 on fail."""
    if not s:
        return -1
    t = s.strip()
    if len(t) >= 19 and t[4] == "-":   # 'YYYY-MM-DD HH:MM:SS'
        t = t[11:]
    parts = t.split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return -1


def append_daily_summary(trade_csv: str = None, summary_csv: str = None,
                         run_date: str = None, dry_run: bool = None,
                         config_summary: str = None, leverage: float = None) -> dict | None:
    """
    Read a day's trade CSV and append one aggregate row to daily_summary.csv.
    Computes peak concurrent margin (notional/leverage) and max drawdown. Reused by
    both the live strategy (end of session) and sector_replay.py (per replayed day).
    Returns the summary dict, or None if there were no trades.
    """
    trade_csv   = trade_csv   or _TRADE_CSV
    summary_csv = summary_csv or os.path.join(_LOG_DIR, "daily_summary.csv")
    run_date    = run_date    or _RUN_DATE
    dry_run     = DRY_RUN if dry_run is None else dry_run
    config_summary = config_summary or _config_summary()
    leverage    = leverage if leverage is not None else LEVERAGE

    if not os.path.exists(trade_csv):
        return None
    rows = []
    with open(trade_csv, newline="") as f:
        for r in _csv.DictReader(f):
            rows.append(r)
    rows = [r for r in rows if r.get("exit_price") not in (None, "", "0.0", "0")]
    if not rows:
        return None

    def _f(r, k):
        try:
            return float(r.get(k) or 0)
        except Exception:
            return 0.0

    nets   = [_f(r, "net") for r in rows]
    gross  = sum(_f(r, "gross") for r in rows)
    charges= sum(_f(r, "charges") for r in rows)
    net    = sum(nets)
    wins   = sum(1 for x in nets if x > 0)
    n      = len(rows)

    # Peak concurrent margin: minute-by-minute timeline of notional/leverage
    events = []
    for r in rows:
        em = _hhmm_to_min(r.get("entry_time", ""))
        xm = _hhmm_to_min(r.get("exit_time", ""))
        notion = _f(r, "notional") or (_f(r, "entry_price") * _f(r, "qty"))
        marg = notion / leverage if leverage else notion
        if em >= 0:
            events.append((em, marg))
            events.append((xm if xm >= 0 else 930, -marg))
    peak_margin = cur = 0.0
    peak_pos = cur_pos = 0
    for tm in range(540, 935):
        for et, dm in events:
            if et == tm:
                cur += dm
                cur_pos += (1 if dm > 0 else -1)
        if cur > peak_margin:
            peak_margin, peak_pos = cur, cur_pos

    # Max drawdown: peak-to-trough of cumulative net ordered by exit time
    ordered = sorted(rows, key=lambda r: _hhmm_to_min(r.get("exit_time", "")))
    cum = run_peak = 0.0
    max_dd = 0.0
    for r in ordered:
        cum += _f(r, "net")
        run_peak = max(run_peak, cum)
        max_dd = min(max_dd, cum - run_peak)

    summary = {
        "date":            run_date,
        "dry_run":         int(bool(dry_run)),
        "n_trades":        n,
        "wins":            wins,
        "win_rate":        round(wins / n * 100, 1) if n else 0,
        "gross":           round(gross, 0),
        "charges":         round(charges, 0),
        "net":             round(net, 0),
        "peak_positions":  peak_pos,
        "peak_margin":     round(peak_margin, 0),
        "max_drawdown_rs": round(max_dd, 0),
        "best_trade":      round(max(nets), 0),
        "worst_trade":     round(min(nets), 0),
        "exit_reasons":    _json.dumps(_count_reasons(rows)),
        "config_summary":  config_summary,
    }

    header = list(summary.keys())
    write_header = not os.path.exists(summary_csv)
    with open(summary_csv, "a", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=header)
        if write_header:
            w.writeheader()
        w.writerow(summary)
    return summary


def _count_reasons(rows) -> dict:
    out: dict = {}
    for r in rows:
        k = r.get("reason", "?")
        out[k] = out.get(k, 0) + 1
    return out


def _print_summary(positions: dict):
    log.info("=" * 96)
    log.info("  BREAKOUT INTRADAY — TRADE SUMMARY")
    log.info(f"  Date={_RUN_DATE}  DRY_RUN={DRY_RUN}  adx>={ADX_THRESHOLD}  trail={TRAIL_MODE}  "
             f"be={BREAKEVEN_PCT}%  target={'{}R'.format(TARGET_RR) if TARGET_RR>0 else 'time'}  "
             f"exit={EXIT_TIME_HM}  reentry={ALLOW_REENTRY}")
    log.info("=" * 96)
    log.info(f"{'SYMBOL':<12} {'DIR':<6} {'KIND':<7} {'SIG_T':<6} {'ENTRY':>9} {'EXIT':>9} "
             f"{'QTY':>5} {'GROSS':>9} {'CHRG':>8} {'NET':>9}  {'REASON':<10} BE  1R")
    tg = tn = tc = 0.0
    wins = losses = 0
    by_reason: dict = {}
    for p in sorted(positions.values(), key=lambda x: x.signal_time):
        if p.exit_price == 0.0:
            continue
        gross = (p.exit_price - p.entry_price) * p.qty * (1 if p.direction == "LONG" else -1)
        ch    = compute_charges(p.entry_price, p.exit_price, p.qty)
        net   = gross - ch
        tg   += gross;  tn += net;  tc += ch
        wins += 1 if net > 0 else 0
        losses += 1 if net <= 0 else 0
        by_reason[p.exit_reason] = by_reason.get(p.exit_reason, 0) + 1
        sig_t = p.signal_time[11:16] if len(p.signal_time) >= 16 else p.signal_time
        log.info(f"{p.symbol:<12} {p.direction:<6} {p.kind:<7} {sig_t:<6} "
                 f"{p.entry_price:>9.2f} {p.exit_price:>9.2f} "
                 f"{p.qty:>5} {gross:>+9.0f} {ch:>8.0f} {net:>+9.0f}  "
                 f"{p.exit_reason:<10} {'Y' if p.reached_be else 'N'}   {'Y' if p.reached_1r else 'N'}")
    n = wins + losses
    log.info("-" * 96)
    log.info(f"{'TOTAL':<12} {'':6} {'':7} {'':6} {'':>9} {'':>9} "
             f"{'':>5} {tg:>+9.0f} {tc:>8.0f} {tn:>+9.0f}")
    log.info(f"  Trades={n}  Wins={wins}({int(wins/n*100) if n else 0}%)  Losses={losses}  "
             f"Charges=Rs.{tc:,.0f}  Exits: {by_reason}")
    log.info(f"  Log files: {_LOG_DIR}/breakout_*_{_RUN_DATE}.*")
    # Append the machine-readable daily row (peak margin, drawdown) for the 15-day report
    try:
        s = append_daily_summary()
        if s:
            log.info(f"  Daily summary → {_LOG_DIR}/daily_summary.csv  "
                     f"NET=Rs.{s['net']:,.0f}  peak_margin=Rs.{s['peak_margin']:,.0f}  "
                     f"maxDD=Rs.{s['max_drawdown_rs']:,.0f}  peak_pos={s['peak_positions']}")
    except Exception as e:
        log.warning(f"daily summary write failed: {e}")
    log.info("=" * 96)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LIVE LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_live():
    # ── SAFETY GUARD ──────────────────────────────────────────────────────────
    # When placing real orders (DRY_RUN=false) for a paper test, OpenAlgo MUST be in
    # analyzer/sandbox mode or those orders hit the live broker. Refuse to start otherwise.
    if not DRY_RUN and REQUIRE_ANALYZER:
        amode = _analyzer_mode_on()
        if amode is None:
            log.error("ABORT: could not verify OpenAlgo analyzer mode (server down?). "
                      "Refusing to place real orders. Set DRY_RUN=true or fix OpenAlgo.")
            return
        if amode is False:
            log.error("ABORT: DRY_RUN=false but OpenAlgo analyzer/sandbox mode is OFF — "
                      "orders would go LIVE to the broker. Turn ON Analyzer mode in OpenAlgo "
                      "(/analyzer) for the paper test, or set REQUIRE_ANALYZER=false to override.")
            return
        log.info("SAFETY: OpenAlgo analyzer/sandbox mode is ON — orders are paper-filled (fake money).")

    log.info("=" * 84)
    _amode_str = "SANDBOX(analyzer)" if (not DRY_RUN) else "INTERNAL-SIM"
    log.info(f"  BREAKOUT INTRADAY — starting   Exchange={EXCHANGE}  Interval={INTERVAL}  "
             f"DRY_RUN={DRY_RUN}  paper_via={_amode_str}")
    log.info(f"  ── SIGNAL  single={ENABLE_SINGLE} double={ENABLE_DOUBLE}  "
             f"buy={ENABLE_BUY} sell={ENABLE_SELL}  VWAP={USE_VWAP}  ADX={USE_ADX}>={ADX_THRESHOLD}(len={ADX_LEN})")
    tgt_str = f"{TARGET_RR}R" if TARGET_RR > 0 else "NONE(time-exit)"
    be_str  = f"{BREAKEVEN_PCT}%" if BREAKEVEN_PCT > 0 else "off"
    log.info(f"  ── EXIT    Target={tgt_str}  Trail={TRAIL_MODE}  Breakeven={be_str}  "
             f"ExitTime={EXIT_TIME_HM}  HardExit={HARD_EXIT_TIME_HM}")
    log.info(f"  ── SIZING  Balance=Rs.{BALANCE:,.0f}  Risk={MAX_LOSS_PCT}%/trade=Rs.{BALANCE*MAX_LOSS_PCT/100:,.0f}  "
             f"NotionalCap=Rs.{CAPITAL_PER_TRADE:,.0f}")
    log.info(f"  ── SESSION {SESSION_START_HM}-{EXIT_TIME_HM}  EntryCutoff={ENTRY_CUTOFF_HM}  "
             f"MaxPos={MAX_OPEN_POSITIONS}  MaxDailyLoss=Rs.{MAX_DAILY_LOSS_RS:,.0f}  "
             f"Reentry={ALLOW_REENTRY}")
    log.info(f"  ── MANUAL  cmd file: {MANUAL_CMD_FILE}")
    log.info(f"             EXIT SYM | MOVESL SYM PRICE | EXITALL")
    if SECTOR_ONLY_MODE:
        log.info(f"  ── SECTOR-ONLY MODE  top_long={SECTOR_TOP_N_LONG} top_short={SECTOR_TOP_N_SHORT}  "
                 f"poll={SECTOR_POLL_SEC}s  exclude={SECTOR_EXCLUDE_RAW}  (TF IntradayBoost disabled)")
    elif SECTOR_FILTER_ENABLED:
        log.info(f"  ── SECTOR FILTER  top_long={SECTOR_TOP_N_LONG} top_short={SECTOR_TOP_N_SHORT}  "
                 f"poll={SECTOR_POLL_SEC}s  exclude={SECTOR_EXCLUDE_RAW}  rule={SECTOR_MULTI_SECTOR_RULE}")
    if SECTOR_FILTER_ENABLED or SECTOR_ONLY_MODE:
        log.info(f"  ── SECTOR QUALITY  breadth>={SECTOR_MIN_BREADTH:.0f}%  "
                 f"top_stocks={SECTOR_TOP_STOCKS_N}  min_rf={SECTOR_MIN_RFACTOR}")
    log.info("=" * 84)
    _jlog("STARTUP", dry_run=DRY_RUN, exchange=EXCHANGE, interval=INTERVAL,
          risk_per_trade=BALANCE * MAX_LOSS_PCT / 100, target_rr=TARGET_RR)

    today = _now_ist().strftime("%Y-%m-%d")
    positions: dict[str, Position] = {}

    # Resume open positions after a restart
    saved = _load_state()
    if saved and saved.get("phase") == "MONITORING":
        for pd_ in saved.get("positions", []):
            positions[pd_["symbol"]] = Position(**pd_)
        log.info(f"RECOVERY — resumed {len(positions)} position(s) from earlier run")

    # Start background pollers
    if TF_RANK_CAP > 0:
        log.info(f"TF_RANK_CAP={TF_RANK_CAP}: only trading stocks ranked <= {TF_RANK_CAP} in TF list")
    if CPR_FILTER_ENABLED:
        log.info(f"CPR_FILTER_ENABLED: only trading stocks with prior-day CPR width <= {CPR_NARROW_PCT}%")
    if CPR_POSITION_FILTER_ENABLED:
        log.info("CPR_POSITION_FILTER_ENABLED: LONG only above CPR top, SHORT only below CPR bottom "
                  "(blocked while price is inside the CPR band)")
    start_tf_rank_poller(stop_event)
    if SECTOR_FILTER_ENABLED:
        start_sector_poller(stop_event)

    watchlist: list[dict] = []
    last_wl_fetch = 0.0
    last_bar_ts: dict[str, str] = {}      # per-symbol last evaluated closed-bar timestamp
    hard_exit_min = HARD_EXIT_HOUR * 60 + HARD_EXIT_MINUTE

    while not stop_event.is_set():
        cm = _cur_min()

        # Before session — idle wait
        if cm < SESSION_START_MIN:
            log.info(f"Pre-session ({_now_ist():%H:%M}); waiting for {SESSION_START_HM}")
            stop_event.wait(min(60, (SESSION_START_MIN - cm) * 60))
            continue

        # Session over — square off and finish
        if cm >= SESSION_END_MIN or cm >= hard_exit_min:
            _square_off_all(positions, today)
            break

        if not _daily_loss_ok(positions):
            _square_off_all(positions, today)
            break

        # 1. Refresh TradeFinder watchlist (throttled)
        if time.time() - last_wl_fetch >= WATCHLIST_REFRESH_SEC:
            if SECTOR_ONLY_MODE:
                # Build watchlist directly from sector cache — no TF IntradayBoost call needed
                with _sector_lock:
                    snap_time  = _sector_cache["snapshot_time"]
                    long_secs  = set(_sector_cache["long_sectors"])
                    short_secs = set(_sector_cache["short_sectors"])
                    stock_secs = dict(_sector_cache["stock_sectors"])
                if snap_time:
                    with _sector_lock:
                        stock_active_cnt = dict(_sector_cache["stock_active_sectors"])
                        stock_rfac_map   = dict(_sector_cache["stock_rfactor"])
                    # Build eligible stock list, sorted by:
                    # 1. active sector count desc (multi-sector stocks first)
                    # 2. r_factor desc (strongest momentum first)
                    eligible = []
                    seen: set[str] = set()
                    for sym_s, sectors in stock_secs.items():
                        if sym_s in seen:
                            continue
                        if any(s in long_secs or s in short_secs for s in sectors):
                            eligible.append(sym_s)
                            seen.add(sym_s)
                    eligible.sort(
                        key=lambda s: (
                            -(stock_active_cnt.get(s, 0)),   # multi-sector first
                            -(stock_rfac_map.get(s, 0.0)),   # then strongest r_factor
                        )
                    )
                    wl = [{"symbol": s, "ltp": 0, "score": stock_rfac_map.get(s, 0.0)}
                          for s in eligible]
                    watchlist = wl
                    ts = _now_ist().strftime("%H:%M:%S")
                    log.info(f"[{ts}] Sector watchlist: {len(watchlist)} stocks  "
                             f"LONG({', '.join(sorted(long_secs))})  "
                             f"SHORT({', '.join(sorted(short_secs))})")
                    _jlog("WATCHLIST", source="sector", count=len(watchlist),
                          long_sectors=sorted(long_secs), short_sectors=sorted(short_secs))
                elif watchlist:
                    log.warning(f"[{_now_ist():%H:%M:%S}] ⚠ Sector cache not yet populated — "
                                f"using stale watchlist ({len(watchlist)} stocks). "
                                f"Sector poller starting up...")
                else:
                    log.warning(f"[{_now_ist():%H:%M:%S}] ⚠ Sector cache empty — waiting for first poll "
                                f"(up to {SECTOR_POLL_SEC}s). No trades until sector data arrives.")
            else:
                # Original mode: TF IntradayBoost as watchlist source
                wl = fetch_intraday_boost()
                if wl:
                    watchlist = wl[:MAX_WATCHLIST] if MAX_WATCHLIST > 0 else wl
                    log.info(f"[{_now_ist():%H:%M:%S}] Watchlist: {len(watchlist)} stocks "
                             f"(top: {', '.join(s['symbol'] for s in watchlist[:5])})")
                    _jlog("WATCHLIST", source="tf", count=len(watchlist),
                          top5=[s["symbol"] for s in watchlist[:5]])
                elif watchlist:
                    log.warning(f"[{_now_ist():%H:%M:%S}] ⚠ TradeFinder fetch failed — "
                                f"using stale watchlist ({len(watchlist)} stocks). JWT expired?")
                else:
                    log.error(f"[{_now_ist():%H:%M:%S}] ✗ No watchlist — TF fetch failed and no prior list. "
                              f"Set TF_JWT_TOKEN to a fresh token.")
            last_wl_fetch = time.time()

        entries_open = cm <= ENTRY_CUTOFF_MIN

        # 2. Scan watchlist for fresh signals
        for stock in watchlist:
            if stop_event.is_set():
                break
            sym = stock["symbol"]
            # Skip if position already open on this symbol
            if sym in positions and positions[sym].active:
                continue
            # If re-entry disabled, skip symbols that already traded today
            if not ALLOW_REENTRY and sym in positions:
                continue
            if not entries_open or len([p for p in positions.values() if p.active]) >= MAX_OPEN_POSITIONS:
                continue

            df = fetch_history(sym, INTERVAL, 10)  # 10 days to warm ATR(14) ewm
            if df is None:
                continue
            sig = evaluate_signal(df, today)
            if not sig:
                continue
            if last_bar_ts.get(sym) == sig["signal_ts"]:
                continue                              # already acted on this bar
            last_bar_ts[sym] = sig["signal_ts"]

            entry_ref, sl, direction = sig["entry_ref"], sig["sl"], sig["direction"]

            # TF rank gate: skip if stock is not in top TF_RANK_CAP at signal time
            if TF_RANK_CAP > 0:
                rank = get_tf_rank(sym)
                if rank is None or rank > TF_RANK_CAP:
                    log.info(f"  {sym:12s} {direction} skip: TF rank {rank} > cap {TF_RANK_CAP}")
                    continue

            # Sector gate: skip if stock's sector does not align with signal direction
            if SECTOR_FILTER_ENABLED:
                allowed, sec_reason = get_sector_gate(sym, direction)
                if not allowed:
                    log.info(f"  {sym:12s} {direction} skip: {sec_reason}")
                    continue

            # CPR gate: skip if prior-day Central Pivot Range is not narrow enough
            if CPR_FILTER_ENABLED:
                cpr_ok, cpr_reason = get_cpr_gate(sym, today)
                if not cpr_ok:
                    log.info(f"  {sym:12s} {direction} skip: {cpr_reason}")
                    continue

            # CPR position gate: LONG only above CPR top, SHORT only below CPR
            # bottom; skip if price is inside the CPR band (no directional edge)
            if CPR_POSITION_FILTER_ENABLED:
                cpr_pos_ok, cpr_pos_reason = get_cpr_position_gate(sym, entry_ref, direction, today)
                if not cpr_pos_ok:
                    log.info(f"  {sym:12s} {direction} skip: {cpr_pos_reason}")
                    continue

            qty = compute_qty(entry_ref, sl)
            ok, reason = _pre_trade_check(sym, direction, qty, entry_ref, sl)
            if not ok:
                log.info(f"  {sym:12s} {direction} skip: {reason}")
                continue

            sl_dist = abs(entry_ref - sl)
            # TARGET_RR=0 → no fixed target, hold till EXIT_TIME or SL
            target = (entry_ref + TARGET_RR * sl_dist if direction == "LONG"
                      else entry_ref - TARGET_RR * sl_dist) if TARGET_RR > 0 else 0.0
            tgt_str = f"{target:.2f}" if target > 0 else "NONE(time-exit)"
            log.info(f"  SIGNAL {sym:12s} {direction} [{sig['kind']}] entry~{entry_ref:.2f} "
                     f"SL={sl:.2f} T={tgt_str} qty={qty} adx={sig['adx']:.1f} "
                     f"be={BREAKEVEN_PCT}%")
            _jlog("SIGNAL", symbol=sym, direction=direction, kind=sig["kind"], entry=entry_ref,
                  sl=sl, target=target, qty=qty, adx=round(sig["adx"], 2))

            # Entry MARKET + broker-side SL-M
            entry_action = "BUY" if direction == "LONG" else "SELL"
            entry_id = _place_order(sym, entry_action, qty, "MARKET")
            if not entry_id:
                continue
            sl_action = "SELL" if direction == "LONG" else "BUY"
            sl_id = _place_order(sym, sl_action, qty, "SL-M", trigger_price=round(sl, 2))
            if not sl_id:
                log.error(f"  ⚠ SL ORDER FAILED for {sym} — cancelling entry to avoid naked position")
                _cancel_order(entry_id)
                continue
            positions[sym] = Position(
                symbol=sym, direction=direction, entry_price=entry_ref,
                qty=qty, initial_sl=sl, current_sl=sl, target=target,
                entry_order_id=entry_id, sl_order_id=sl_id,
                signal_time=sig["signal_ts"],
                entry_time=_now_ist().strftime("%H:%M:%S"),
                signal_adx=round(sig["adx"], 2),
                signal_vwap=round(sig["vwap"] or 0.0, 2), kind=sig["kind"],
            )
            _jlog("POSITION_OPEN", symbol=sym, direction=direction, entry=entry_ref,
                  sl=sl, target=target, qty=qty, kind=sig["kind"], adx=round(sig["adx"], 2))
            _save_state(positions, "MONITORING")

        # 3. Process manual commands (EXIT / MOVESL / EXITALL from command file)
        _process_manual_commands(positions, today)

        # 4. Manage open positions
        for pos in list(positions.values()):
            if stop_event.is_set():
                break
            _manage_position(pos, today)
        _save_state(positions, "MONITORING")

        active = sum(1 for p in positions.values() if p.active)
        log.info(f"[{_now_ist():%H:%M:%S}] heartbeat — watchlist={len(watchlist)} active_pos={active}")
        stop_event.wait(POLL_INTERVAL_SEC)

    _print_summary(positions)
    try:
        _get_client().disconnect()
    except Exception:
        pass
    log.info("Shutdown complete")


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS  (python breakout_intraday_strategy.py --test)
# ══════════════════════════════════════════════════════════════════════════════

def run_diagnostics():
    today = _now_ist().strftime("%Y-%m-%d")
    log.info("=" * 72)
    log.info("  BREAKOUT INTRADAY — DIAGNOSTICS")
    log.info(f"  Date={today}  Interval={INTERVAL}  VWAP={USE_VWAP} ADX={USE_ADX}>={ADX_THRESHOLD}")
    log.info("=" * 72)

    log.info("\nSTEP 1 — TradeFinder IntradayBoost")
    stocks = fetch_intraday_boost()
    if not stocks:
        log.error("  Could not fetch stocks. Check TF_JWT_TOKEN / network.")
        return
    log.info(f"  Received {len(stocks)} stocks. Top 10:")
    for i, s in enumerate(stocks[:10], 1):
        log.info(f"   {i:>2}  {s['symbol']:<14} score={s['score']:>7.3f} ltp={s['ltp']:>9.2f} chg={s['change_pct']:>+6.2f}%")

    log.info(f"\nSTEP 2 — signal check on first {min(10, len(stocks))} stocks")
    for stock in stocks[:10]:
        sym = stock["symbol"]
        df = fetch_history(sym, INTERVAL, 2)
        if df is None:
            log.info(f"  {sym:12s}  no data")
            continue
        sig = evaluate_signal(df, today)
        if sig:
            qty = compute_qty(sig["entry_ref"], sig["sl"])
            sl_dist = abs(sig["entry_ref"] - sig["sl"])
            tgt = sig["entry_ref"] + TARGET_RR * sl_dist if sig["direction"] == "LONG" else sig["entry_ref"] - TARGET_RR * sl_dist
            cpr_note = ""
            if CPR_FILTER_ENABLED:
                cpr_ok, cpr_reason = get_cpr_gate(sym, today)
                cpr_note += f"  [{cpr_reason}]" + ("" if cpr_ok else " SKIP")
            if CPR_POSITION_FILTER_ENABLED:
                cpr_pos_ok, cpr_pos_reason = get_cpr_position_gate(
                    sym, sig["entry_ref"], sig["direction"], today)
                cpr_note += f"  [{cpr_pos_reason}]" + ("" if cpr_pos_ok else " SKIP")
            log.info(f"  {sym:12s}  {sig['direction']} [{sig['kind']}] entry~{sig['entry_ref']:.2f} "
                     f"SL={sig['sl']:.2f} T={tgt:.2f} qty={qty} adx={sig['adx']:.1f}{cpr_note}")
        else:
            log.info(f"  {sym:12s}  no signal on last closed {INTERVAL} bar")
        time.sleep(0.2)
    log.info("\nDiagnostics complete.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="one diagnostic pass, no orders")
    p.add_argument("--mode", choices=["live"], default=os.getenv("MODE", "live"))
    args = p.parse_args()
    if args.test or os.getenv("TEST_MODE", "false").lower() == "true":
        run_diagnostics()
    else:
        run_live()


if __name__ == "__main__":
    main()
