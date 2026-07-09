#!/usr/bin/env python
"""
Survivor Options Strategy — Naked Original Version
==================================================
Original logic: Naked Selling without Hedges.
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
DB_PATH = "../../db/historify.duckdb"
RESULTS_DIR = "results"

STRATEGY = {
    "pe_gap": 20, "ce_gap": 20,
    "pe_symbol_gap": 200, "ce_symbol_gap": 200,
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
        spot = self.conn.execute(f"SELECT timestamp, close FROM market_data WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND timestamp BETWEEN {start_ts} AND {end_ts} ORDER BY timestamp").fetchdf()
        if not spot.empty: spot["ts"] = pd.to_datetime(spot["timestamp"], unit="s")
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
        self.unrealised_pnl = 0.0
        self.exit_time = None
        self.exit_reason = None
        self.exit_price = 0.0

    def update(self, px, ts, reason=None):
        self.realised_pnl = (self.entry_price - px) * self.qty
        self.unrealised_pnl = self.realised_pnl
        if reason:
            self.exit_time, self.exit_reason, self.exit_price = ts, reason, px

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "long_symbol": "NAKED",
            "option_type": self.option_type,
            "qty": self.qty,
            "entry_time": str(self.entry_time),
            "entry_price": self.entry_price,
            "short_entry": self.entry_price,
            "long_entry": 0.0,
            "net_credit": self.entry_price,
            "nifty_at_entry": self.nifty_at_entry,
            "margin_used": self.margin_used,
            "expiry": str(self.expiry),
            "exit_time": str(self.exit_time) if self.exit_time else None,
            "exit_reason": self.exit_reason,
            "short_exit": self.exit_price,
            "long_exit": 0.0,
            "exit_price": self.exit_price,
            "realised_pnl": self.realised_pnl,
            "lot_size": 75
        }

class Backtest:
    def __init__(self):
        self.loader = DataLoader(DB_PATH)
        self.open_pos = []
        self.closed_pos = []
        self.total_pnl = 0.0
        self.peak_margin = 0.0
        self.daily_stats = []
        self.expiry_stats = {}

    def run(self):
        dates = pd.date_range(BACKTEST_START, BACKTEST_END)
        for day_ts in tqdm(dates, desc="Processing Naked"):
            day = day_ts.date()
            spot_df, opt_cache = self.loader.load_day_cache(day)
            if spot_df.empty: continue
            
            expiry = self.loader.get_expiry(day)
            lot_size = self.loader.get_lot_size(expiry)
            pe_ref = ce_ref = float(spot_df.iloc[0]["close"])
            
            for _, row in spot_df.iterrows():
                ts_val, price = int(row["timestamp"]), float(row["close"])
                ts_dt = row["ts"]
                
                for pos in list(self.open_pos):
                    px = opt_cache.get(pos.symbol, {}).get(ts_val)
                    if px:
                        pos.update(px, ts_dt)
                        if px <= STRATEGY["tp_absolute"]: self.close(pos, px, ts_dt, "TP")
                        elif px >= STRATEGY["disaster_exit"]: self.close(pos, px, ts_dt, "DISASTER")

                if price < pe_ref - STRATEGY["pe_reset_gap"]: pe_ref = price
                if price > ce_ref + STRATEGY["ce_reset_gap"]: ce_ref = price
                
                diff_pe = price - pe_ref
                if diff_pe >= STRATEGY["pe_gap"]:
                    m = int(diff_pe // STRATEGY["pe_gap"])
                    pe_ref += m * STRATEGY["pe_gap"]
                    if m <= STRATEGY["sell_multiplier_threshold"]:
                        strike = int(round((price - STRATEGY["pe_symbol_gap"])/50)*50)
                        sym = f"NIFTY{expiry.strftime('%d%b%y').upper()}{strike}PE"
                        e = opt_cache.get(sym, {}).get(ts_val)
                        if e and e >= STRATEGY["min_price_to_sell"]:
                            mgn = (price * 75 * 0.055) * m
                            self.open_pos.append(Position(symbol=sym, qty=m*75, entry_price=e, entry_time=ts_dt, margin_used=mgn, expiry=expiry, nifty_at_entry=price, option_type="PE"))
                            self._record_trade(expiry, mgn, e * m * 75)

                diff_ce = ce_ref - price
                if diff_ce >= STRATEGY["ce_gap"]:
                    m = int(diff_ce // STRATEGY["ce_gap"])
                    ce_ref -= m * STRATEGY["ce_gap"]
                    if m <= STRATEGY["sell_multiplier_threshold"]:
                        strike = int(round((price + STRATEGY["ce_symbol_gap"])/50)*50)
                        sym = f"NIFTY{expiry.strftime('%d%b%y').upper()}{strike}CE"
                        e = opt_cache.get(sym, {}).get(ts_val)
                        if e and e >= STRATEGY["min_price_to_sell"]:
                            mgn = (price * 75 * 0.055) * m
                            self.open_pos.append(Position(symbol=sym, qty=m*75, entry_price=e, entry_time=ts_dt, margin_used=mgn, expiry=expiry, nifty_at_entry=price, option_type="CE"))
                            self._record_trade(expiry, mgn, e * m * 75)

                current_margin = sum(p.margin_used for p in self.open_pos)
                self.peak_margin = max(self.peak_margin, current_margin)
                
                # Update peak margin in expiry stats
                for p in self.open_pos:
                    key = str(p.expiry)
                    exp_mgn = sum(pos.margin_used for pos in self.open_pos if pos.expiry == p.expiry)
                    if exp_mgn > self.expiry_stats[key]["peak_margin"]:
                        self.expiry_stats[key]["peak_margin"] = exp_mgn

            day_unrealised = sum(p.unrealised_pnl for p in self.open_pos)
            self.daily_stats.append({
                "date": str(day),
                "cumulative_rpnl": round(self.total_pnl, 2),
                "current_margin": round(sum(p.margin_used for p in self.open_pos), 2),
                "open_positions": len(self.open_pos),
                "unrealised_pnl": round(day_unrealised, 2)
            })

            if day == expiry:
                ts_last = int(spot_df.iloc[-1]["timestamp"])
                for pos in list(self.open_pos):
                    if pos.expiry == day:
                        self.close(pos, opt_cache.get(pos.symbol, {}).get(ts_last, 0), spot_df.iloc[-1]["ts"], "EXPIRY")

        self._save_results()
        print(f"\nNaked Final P&L: Rs {self.total_pnl:,.2f} | Peak Margin: Rs {self.peak_margin:,.2f}")

    def _record_trade(self, expiry, margin, credit):
        key = str(expiry)
        if key not in self.expiry_stats: self.expiry_stats[key] = {"expiry": key, "trades": 0, "total_credit": 0, "realised_pnl": 0, "peak_margin": 0}
        self.expiry_stats[key]["trades"] += 1
        self.expiry_stats[key]["total_credit"] += credit

    def close(self, pos, px, ts, reason):
        pos.update(px, ts, reason)
        self.total_pnl += pos.realised_pnl
        exp_key = str(pos.expiry)
        if exp_key in self.expiry_stats: self.expiry_stats[exp_key]["realised_pnl"] += pos.realised_pnl
        self.open_pos.remove(pos)
        self.closed_pos.append(pos)

    def _save_results(self):
        os.makedirs(RESULTS_DIR, exist_ok=True)
        summary = {
            "total_trades": len(self.closed_pos),
            "total_realised_pnl": round(self.total_pnl, 2),
            "peak_concurrent_margin": round(self.peak_margin, 2),
            "total_credit_collected": round(sum(p.entry_price * p.qty for p in self.closed_pos), 2)
        }
        full_output = {
            "backtest_period": f"{BACKTEST_START} to {BACKTEST_END}",
            "generated_at": datetime.now().isoformat(),
            "trades": [p.to_dict() for p in self.closed_pos],
            "summary": summary,
            "strategy_config": STRATEGY
        }
        with open(Path(RESULTS_DIR) / "trades.json", "w") as f:
            json.dump(full_output, f, indent=2)
        with open(Path(RESULTS_DIR) / "daily_stats.json", "w") as f:
            json.dump(self.daily_stats, f, indent=2)
        
        exp_list = list(self.expiry_stats.values())
        for e in exp_list:
            e["roi_on_peak_margin"] = (e["realised_pnl"] / e["peak_margin"] * 100) if e["peak_margin"] > 0 else 0
        with open(Path(RESULTS_DIR) / "expiry_stats.json", "w") as f:
            json.dump(exp_list, f, indent=2)

if __name__ == "__main__":
    Backtest().run()
