#!/usr/bin/env python
"""
Survivor Options Strategy — Full Backtest Engine (Hedged & Optimized)
====================================================================
Pre-caches daily data into memory to maximize performance.
"""

import duckdb
import json
import os
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from tqdm import tqdm
import pandas as pd

warnings.filterwarnings("ignore")

# --- CONFIG ---
BACKTEST_START = "2024-10-01"
BACKTEST_END   = "2026-04-30"
DB_PATH = "../db/historify.duckdb"
RESULTS_DIR = "results"

STRATEGY = {
    "pe_gap": 20, "ce_gap": 20,
    "pe_symbol_gap": 200, "ce_symbol_gap": 200, "hedge_dist": 500,
    "pe_quantity": 75, "ce_quantity": 75,
    "min_price_to_sell": 15, "sell_multiplier_threshold": 5,
    "pe_reset_gap": 30, "ce_reset_gap": 30,
    "lot_size": 75, "strike_interval": 50,
    "disaster_exit": 250, "tp_absolute": 0.50, "max_multiplier": 50
}

# --- DB LAYER ---
class DataLoader:
    def __init__(self, db_path):
        self.conn = duckdb.connect(db_path, read_only=True)
        self.all_expiries = [d.date() for d in pd.to_datetime(self.conn.execute("SELECT DISTINCT expiry_date FROM expired_fno_contracts WHERE upstox_key = 'NSE_INDEX|Nifty 50' ORDER BY expiry_date").fetchdf()["expiry_date"])]

    def get_expiry(self, ref_date):
        for exp in self.all_expiries:
            if exp >= ref_date: return exp
        return ref_date + timedelta(days=(3 - ref_date.weekday()) % 7)

    def load_day_cache(self, day):
        start_ts = int(datetime.combine(day, datetime.min.time()).timestamp())
        end_ts   = int(datetime.combine(day, datetime.max.time()).timestamp())
        
        # Spot
        spot = self.conn.execute(f"SELECT timestamp, close FROM market_data WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND timestamp BETWEEN {start_ts} AND {end_ts} ORDER BY timestamp").fetchdf()
        if not spot.empty: spot["ts"] = pd.to_datetime(spot["timestamp"], unit="s")
        
        # Options - Cache all 1m bars for the day
        opts = self.conn.execute(f"SELECT symbol, timestamp, close FROM market_data WHERE exchange='NFO' AND timestamp BETWEEN {start_ts} AND {end_ts}").fetchdf()
        cache = {}
        if not opts.empty:
            for sym, group in opts.groupby("symbol"):
                cache[sym] = dict(zip(group["timestamp"], group["close"]))
        
        return spot, cache

    def get_lot_size(self, expiry):
        res = self.conn.execute(f"SELECT lot_size FROM expired_fno_contracts WHERE openalgo_symbol='NIFTY' AND expiry_date='{expiry}' LIMIT 1").fetchone()
        return int(res[0]) if res else 75

# --- LOGIC ---
class Position:
    def __init__(self, **kwargs):
        for k,v in kwargs.items(): setattr(self, k, v)
        self.realised_pnl = 0.0

    def update(self, s_px, l_px, ts, reason=None):
        net_entry = self.short_entry_price - self.long_entry_price
        net_exit = s_px - l_px
        self.realised_pnl = (net_entry - net_exit) * self.qty
        if reason: self.exit_time, self.exit_reason, self.short_exit_price, self.long_exit_price = ts, reason, s_px, l_px

class Backtest:
    def __init__(self):
        self.loader = DataLoader(DB_PATH)
        self.open_pos = []
        self.closed_pos = []
        self.total_pnl = 0.0
        self.peak_margin = 0.0

    def run(self):
        dates = pd.date_range(BACKTEST_START, BACKTEST_END)
        for day_ts in tqdm(dates, desc="Processing"):
            day = day_ts.date()
            spot_df, opt_cache = self.loader.load_day_cache(day)
            if spot_df.empty: continue
            
            expiry = self.loader.get_expiry(day)
            lot_size = self.loader.get_lot_size(expiry)
            pe_ref = ce_ref = float(spot_df.iloc[0]["close"])
            
            for _, row in spot_df.iterrows():
                ts_val, price = int(row["timestamp"]), float(row["close"])
                ts_dt = row["ts"]
                
                # Check Exits
                for pos in list(self.open_pos):
                    s_px, l_px = opt_cache.get(pos.symbol, {}).get(ts_val), opt_cache.get(pos.long_symbol, {}).get(ts_val)
                    if s_px and l_px:
                        if (s_px - l_px) <= STRATEGY["tp_absolute"]: self.close(pos, s_px, l_px, ts_dt, "TP")
                        elif s_px >= STRATEGY["disaster_exit"]: self.close(pos, s_px, l_px, ts_dt, "DISASTER")

                # Resets
                if price < pe_ref - STRATEGY["pe_reset_gap"]: pe_ref = price
                if price > ce_ref + STRATEGY["ce_reset_gap"]: ce_ref = price
                
                # Signals
                diff_pe = price - pe_ref
                if diff_pe >= STRATEGY["pe_gap"]:
                    m = int(diff_pe // STRATEGY["pe_gap"])
                    pe_ref += m * STRATEGY["pe_gap"]
                    if m <= STRATEGY["sell_multiplier_threshold"]:
                        strike = int(round((price - STRATEGY["pe_symbol_gap"])/50)*50)
                        l_strike = strike - STRATEGY["hedge_dist"]
                        s_sym, l_sym = f"NIFTY{expiry.strftime('%d%b%y').upper()}{strike}PE", f"NIFTY{expiry.strftime('%d%b%y').upper()}{l_strike}PE"
                        s_e, l_e = opt_cache.get(s_sym, {}).get(ts_val), opt_cache.get(l_sym, {}).get(ts_val)
                        if s_e and l_e and s_e >= STRATEGY["min_price_to_sell"]:
                            mgn = min(50000, abs(strike-l_strike)*lot_size) * (m * STRATEGY["pe_quantity"] / lot_size)
                            self.open_pos.append(Position(symbol=s_sym, long_symbol=l_sym, qty=m*STRATEGY["pe_quantity"], short_entry_price=s_e, long_entry_price=l_e, margin_used=mgn, expiry=expiry))

                diff_ce = ce_ref - price
                if diff_ce >= STRATEGY["ce_gap"]:
                    m = int(diff_ce // STRATEGY["ce_gap"])
                    ce_ref -= m * STRATEGY["ce_gap"]
                    if m <= STRATEGY["sell_multiplier_threshold"]:
                        strike = int(round((price + STRATEGY["ce_symbol_gap"])/50)*50)
                        l_strike = strike + STRATEGY["hedge_dist"]
                        s_sym, l_sym = f"NIFTY{expiry.strftime('%d%b%y').upper()}{strike}CE", f"NIFTY{expiry.strftime('%d%b%y').upper()}{l_strike}CE"
                        s_e, l_e = opt_cache.get(s_sym, {}).get(ts_val), opt_cache.get(l_sym, {}).get(ts_val)
                        if s_e and l_e and s_e >= STRATEGY["min_price_to_sell"]:
                            mgn = min(50000, abs(strike-l_strike)*lot_size) * (m * STRATEGY["ce_quantity"] / lot_size)
                            self.open_pos.append(Position(symbol=s_sym, long_symbol=l_sym, qty=m*STRATEGY["ce_quantity"], short_entry_price=s_e, long_entry_price=l_e, margin_used=mgn, expiry=expiry))

                self.peak_margin = max(self.peak_margin, sum(p.margin_used for p in self.open_pos))

            if day == expiry:
                ts_last = int(spot_df.iloc[-1]["timestamp"])
                for pos in list(self.open_pos):
                    if pos.expiry == day:
                        s_px, l_px = opt_cache.get(pos.symbol, {}).get(ts_last, 0), opt_cache.get(pos.long_symbol, {}).get(ts_last, 0)
                        self.close(pos, s_px, l_px, spot_df.iloc[-1]["ts"], "EXPIRY")

        print(f"\nFinal P&L: Rs {self.total_pnl:,.2f} | Peak Margin: Rs {self.peak_margin:,.2f}")

    def close(self, pos, s_px, l_px, ts, reason):
        pos.update(s_px, l_px, ts, reason)
        self.total_pnl += pos.realised_pnl
        self.open_pos.remove(pos)
        self.closed_pos.append(pos)

if __name__ == "__main__":
    Backtest().run()
