from database.historify_db import get_ohlcv
from utils.volatility import compute_ohlc_volatility
import pandas as pd

def simulate_move(symbol, exchange, pct_move):
    """
    Fetches the last 21 days of data, adds a simulated move for 'today', 
    and recalculates volatility.
    """
    df = get_ohlcv(symbol=symbol, exchange=exchange, interval='D')
    
    if df.empty:
        df = get_ohlcv(symbol=symbol, exchange=exchange, interval='1m')
        if df.empty:
            return f"No data found for {symbol}"
        
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        df.set_index('timestamp', inplace=True)
        df = df.resample('D').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
        }).dropna()
    else:
        # If D data exists, convert the numeric timestamp column to index
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            df.set_index('timestamp', inplace=True)

    # 2. Get the last known close
    last_real_close = df['close'].iloc[-1]
    
    # Simulate a move (e.g., 5% up)
    simulated_close = last_real_close * (1 + (pct_move / 100))
    
    # Create the simulated today row
    simulated_row = pd.DataFrame([{
        'open': last_real_close,
        'high': max(last_real_close, simulated_close),
        'low': min(last_real_close, simulated_close),
        'close': simulated_close
    }], index=[df.index[-1] + pd.Timedelta(days=1)])

    # 3. Create a "What If" dataset
    df_simulated = pd.concat([df, simulated_row])

    # 4. Compute Volatility
    real_vol = compute_ohlc_volatility(df)
    sim_vol = compute_ohlc_volatility(df_simulated)

    v_before = real_vol['vol_yz_ann'].iloc[-1]
    v_after = sim_vol['vol_yz_ann'].iloc[-1]

    return {
        "symbol": symbol,
        "last_price": round(last_real_close, 2),
        "sim_price": round(simulated_close, 2),
        "vol_before": round(v_before, 2),
        "vol_after": round(v_after, 2),
        "vol_spike": round(v_after - v_before, 2)
    }

def main():
    targets = [
        {"symbol": "COLPAL", "exchange": "NSE"},
        {"symbol": "PIDILITIND", "exchange": "NSE"}
    ]
    
    move_percent = 5.0 
    
    print(f"=== Volatility Simulation: What if stocks move {move_percent}% today? ===\n")
    
    for t in targets:
        res = simulate_move(t['symbol'], t['exchange'], move_percent)
        if isinstance(res, dict):
            print(f"Symbol: {res['symbol']}")
            print(f"  Current Price: {res['last_price']}")
            print(f"  Simulated Price (+{move_percent}%): {res['sim_price']}")
            print(f"  Vol Before: {res['vol_before']}%")
            print(f"  Vol After:  {res['vol_after']}%")
            print(f"  Impact: +{res['vol_spike']}% volatility spike\n")
        else:
            print(res)

if __name__ == "__main__":
    main()
