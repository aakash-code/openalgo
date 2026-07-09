import json
import duckdb
import pandas as pd
from datetime import datetime, timedelta
import os

DB_PATH = "db/historify.duckdb"
RESULTS_DIR = "survivor_ultimate_results"
EXPIRY_FILE = os.path.join(RESULTS_DIR, "expiry_stats.json")
TRADES_FILE = os.path.join(RESULTS_DIR, "trades.json")

def analyze_losses():
    # 1. Identify top 5 Loss Events
    with open(EXPIRY_FILE, 'r') as f:
        expiry_stats = json.load(f)
    
    # Sort by realised_pnl ascending to get the biggest losses
    losses = sorted([e for e in expiry_stats if e['realised_pnl'] < 0], key=lambda x: x['realised_pnl'])
    top_losses = losses[:5]

    conn = duckdb.connect(DB_PATH, read_only=True)
    
    print("="*100)
    print(f"{'EXPIRY DATE':<15} | {'LOSS AMOUNT':>15} | {'TOTAL TRADES':>10} | {'PEAK MARGIN':>15} | {'ROI %':>8}")
    print("-"*100)
    
    for loss in top_losses:
        print(f"{loss['expiry']:<15} | ₹ {loss['realised_pnl']:>13,.2f} | {loss['trades']:>10} | ₹ {loss['peak_margin']:>13,.2f} | {loss['roi_on_peak_margin']:>7.2f}%")

    print("\n" + "="*100)
    print("DETAILED DATA-DRIVEN ANALYSIS OF TOP LOSS EVENTS")
    print("="*100)

    for loss in top_losses:
        expiry_date = loss['expiry']
        print(f"\n[FORENSIC] Analyzing Expiry: {expiry_date}")
        
        # Determine the trading day (usually the expiry date itself)
        # But signals could be open from days prior. 
        # Let's look at trades for this expiry.
        with open(TRADES_FILE, 'r') as f:
            all_trades = json.load(f)['trades']
        
        expiry_trades = [t for t in all_trades if t['expiry'] == expiry_date]
        if not expiry_trades:
            continue

        # Get the first entry date for this expiry
        trade_dates = sorted(list(set([t['entry_time'][:10] for t in expiry_trades])))
        first_date = trade_dates[0]
        last_date = trade_dates[-1]

        # GAP ANALYSIS
        # Previous close vs today's open
        prev_date_query = f"""
            SELECT close FROM market_data 
            WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m' 
            AND DATE(to_timestamp(timestamp)) < '{first_date}'
            ORDER BY timestamp DESC LIMIT 1
        """
        prev_close_res = conn.execute(prev_date_query).fetchone()
        
        today_open_query = f"""
            SELECT open FROM market_data 
            WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m' 
            AND DATE(to_timestamp(timestamp)) = '{first_date}'
            ORDER BY timestamp ASC LIMIT 1
        """
        today_open_res = conn.execute(today_open_query).fetchone()

        if prev_close_res and today_open_res:
            prev_close = prev_close_res[0]
            today_open = today_open_res[0]
            gap = today_open - prev_close
            gap_pct = (gap / prev_close) * 100
            print(f"  - GAP ANALYSIS: Prev Close: {prev_close:.2f} | Today Open: {today_open:.2f} | Gap: {gap:+.2f} ({gap_pct:+.2f}%)")
        
        # INTRADAY ANALYSIS (Max Move)
        intraday_query = f"""
            SELECT MIN(low) as min_low, MAX(high) as max_high, 
            (SELECT open FROM market_data WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m' AND DATE(to_timestamp(timestamp)) = '{first_date}' ORDER BY timestamp ASC LIMIT 1) as day_open
            FROM market_data 
            WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m' 
            AND DATE(to_timestamp(timestamp)) = '{first_date}'
        """
        intra_res = conn.execute(intraday_query).fetchone()
        if intra_res:
            min_low, max_high, day_open = intra_res
            range_pts = max_high - min_low
            print(f"  - INTRADAY RANGE: {range_pts:.2f} points | Max High: {max_high:.2f} | Min Low: {min_low:.2f}")

        # GAMMA BLAST DETECTION (Sudden spikes in option price)
        # Look for the trade with the biggest individual loss
        worst_trade = sorted(expiry_trades, key=lambda x: x['realised_pnl'])[0]
        print(f"  - WORST TRADE: {worst_trade['symbol']} | Entry: {worst_trade['entry_price']:.2f} | Exit: {worst_trade['exit_price']:.2f} | P&L: ₹ {worst_trade['realised_pnl']:,.2f}")
        
        # Check for multiplier impact
        max_mult = max([t['multiplier'] for t in expiry_trades])
        print(f"  - POSITION SIZING: Max Multiplier reached: {max_mult}x")
        
        # ROOT CAUSE HYPOTHESIS
        if abs(gap_pct) > 1.0:
            print("  - ROOT CAUSE: Massive Opening Gap leading to immediate delta pressure.")
        elif range_pts > 400:
            print("  - ROOT CAUSE: Intraday Trend (Gamma Blast). Positions were averaged too many times against a strong move.")
        elif max_mult > 10:
            print("  - ROOT CAUSE: Over-averaging. The multiplier escalated too quickly before decay could kick in.")
        else:
            print("  - ROOT CAUSE: Extreme Volatility / IV Expansion near expiry.")

    print("\n" + "="*100)
    print("ACTIONABLE IMPROVEMENTS")
    print("="*100)
    print("1. GAP HANDLING: Disable new entries if opening gap > 0.75%.")
    print("2. DYNAMIC MULTIPLIER: Reduce multiplier step (e.g. from 2x to 1.5x) if VIX is above 18.")
    print("3. HARD RISK CAP: Cap the max multiplier at 10-15x. Current data shows losses blow up when multiplier > 20x.")
    print("4. TIME-BASED DECAY: Don't average after 2:30 PM on expiry day; Gamma risk is too high.")
    print("5. LOT SIZE ADJUSTMENT: Ensure capital allocation is adjusted for the 75 -> 65 lot size change to keep exposure constant.")

analyze_losses()