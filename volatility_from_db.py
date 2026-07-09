from database.historify_db import get_ohlcv, get_available_symbols
from utils.volatility import compute_ohlc_volatility
import pandas as pd
import sys

def main():
    # 1. Fetch available symbols
    symbols = get_available_symbols()
    if not symbols:
        print("No data found in the Historify database.")
        return

    # 2. Focus on RELIANCE for this demo
    target = next((s for s in symbols if s['symbol'] == 'RELIANCE'), symbols[0])
    symbol = target['symbol']
    exchange = target['exchange']
    
    print(f"=== Real Data Demo: {symbol} ({exchange}) ===")
    
    # 3. Get ORIGINAL data from DB
    # We try daily ('D') first, if not we try '1m' and resample
    df = get_ohlcv(symbol=symbol, exchange=exchange, interval='D')
    
    if df.empty:
        print(f"No daily data for {symbol}, checking 1m data...")
        df = get_ohlcv(symbol=symbol, exchange=exchange, interval='1m')
        if df.empty:
            print("No data available for this symbol.")
            return
        
        # If it's 1m data, we should resample it to daily for volatility estimators
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        df.set_index('timestamp', inplace=True)
        df = df.resample('D').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        print(f"Resampled 1m data to {len(df)} daily candles.")

    # 4. Compute Volatility using your ORIGINAL data
    df_vol = compute_ohlc_volatility(df, window=21, trading_days=252)

    # 5. Show Results
    print(f"\nVolatility report for {symbol} using database data:")
    cols = ['open', 'high', 'low', 'close', 'vol_yz_ann', 'vol_rs_ann']
    print(df_vol[cols].tail(10))

if __name__ == "__main__":
    main()
