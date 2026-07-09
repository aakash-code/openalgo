
import sys
import duckdb
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date, time as dtime
from pathlib import Path
import os

# ============================================================================
# CONFIG
# ============================================================================
SMA_SHORT_PERIOD = 68
SMA_LONG_PERIOD  = 90
EMA_PERIOD       = 340

# Spread parameters
LOTS            = 1
LOT_SIZE        = 50
SPREAD_WIDTH    = 100
STRIKE_INTERVAL = 50

# Exit thresholds
SL_PCT          = 0.0075   # 0.75% Stop Loss from entry price (Spot)

_script_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_script_dir / ".." / ".." / "db" / "historify.duckdb")

def get_nifty_spot_data():
    """Fetch NIFTY spot data and resample to 5m"""
    con = duckdb.connect(DUCKDB_PATH)
    query = """
    SELECT timestamp, open, high, low, close 
    FROM market_data 
    WHERE symbol = 'NIFTY' AND exchange = 'NSE_INDEX'
    ORDER BY timestamp
    """
    df = con.execute(query).df()
    con.close()
    
    if df.empty:
        return df
        
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
    df.set_index('timestamp', inplace=True)
    
    # Resample to 5-minute bars
    df_5m = df.resample('5min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last'
    }).dropna()
    
    return df_5m

def calculate_indicators(df):
    df['sma68'] = df['close'].rolling(window=SMA_SHORT_PERIOD).mean()
    df['sma90'] = df['close'].rolling(window=SMA_LONG_PERIOD).mean()
    df['ema340'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    return df

def run_backtest():
    print("🚀 Starting 5-Minute NIFTY Options Backtest (SMA/EMA)...")
    df = get_nifty_spot_data()
    if df.empty:
        print("❌ No data found.")
        return

    df = calculate_indicators(df)
    trades = []
    active_trade = None
    
    # Reset index to use integer loop for easier lookback
    df_reset = df.reset_index()
    
    for i in range(len(df_reset)):
        row = df_reset.iloc[i]
        if pd.isna(row['sma90']) or pd.isna(row['ema340']):
            continue
            
        # Entry Logic (Checked at the close of every 5m bar)
        if not active_trade:
            if row['sma68'] > row['sma90'] and row['close'] > row['ema340']:
                active_trade = {
                    'type': 'BULL_PUT',
                    'entry_time': row['timestamp'],
                    'entry_spot': row['close'],
                    'sl_price': row['close'] * (1 - SL_PCT)
                }
            elif row['sma68'] < row['sma90'] and row['close'] < row['ema340']:
                active_trade = {
                    'type': 'BEAR_CALL',
                    'entry_time': row['timestamp'],
                    'entry_spot': row['close'],
                    'sl_price': row['close'] * (1 + SL_PCT)
                }
        
        # Exit Logic
        else:
            is_exit = False
            reason = ""
            
            # 1. Stop Loss Check
            if active_trade['type'] == 'BULL_PUT' and row['low'] <= active_trade['sl_price']:
                is_exit, reason = True, "SL Hit"
            elif active_trade['type'] == 'BEAR_CALL' and row['high'] >= active_trade['sl_price']:
                is_exit, reason = True, "SL Hit"
                
            # 2. Trend Reversal Check (indicators flipping)
            if not is_exit:
                if active_trade['type'] == 'BULL_PUT' and (row['sma68'] < row['sma90'] or row['close'] < row['ema340']):
                    is_exit, reason = True, "Trend Reversal"
                elif active_trade['type'] == 'BEAR_CALL' and (row['sma68'] > row['sma90'] or row['close'] > row['ema340']):
                    is_exit, reason = True, "Trend Reversal"

            if is_exit:
                active_trade['exit_time'] = row['timestamp']
                active_trade['exit_spot'] = row['close']
                active_trade['exit_reason'] = reason
                active_trade['pnl_points'] = (row['close'] - active_trade['entry_spot']) if active_trade['type'] == 'BULL_PUT' else (active_trade['entry_spot'] - row['close'])
                trades.append(active_trade)
                active_trade = None

    if trades:
        res_df = pd.DataFrame(trades)
        print("\n" + "="*50)
        print("📊 5-MINUTE STRATEGY SUMMARY")
        print("="*50)
        print(f"Total Trades: {len(res_df)}")
        print(f"Win Rate (Spot): {(res_df['pnl_points'] > 0).sum() / len(res_df) * 100:.2f}%")
        print(f"Avg Pts per Trade: {res_df['pnl_points'].mean():.2f}")
        print("="*50)
        
        # Show last 5 trades
        print("\nRecent Trades:")
        print(res_df[['entry_time', 'type', 'exit_reason', 'pnl_points']].tail())
    else:
        print("No trades generated.")

if __name__ == "__main__":
    run_backtest()
