import pandas as pd
import numpy as np
import warnings
from database.historify_db import get_ohlcv, get_connection
from utils.volatility import compute_ohlc_volatility
from utils.ml_volatility import prepare_ml_dataset, train_nnls_model, compute_lwma

warnings.filterwarnings("ignore")


def get_historify_symbols(exchange='NSE', interval='D'):
    """Fetch all symbols that have Daily data in the Historify DB."""
    try:
        with get_connection() as conn:
            result = conn.execute("""
                SELECT DISTINCT symbol
                FROM market_data
                WHERE exchange = ? AND interval = ?
                ORDER BY symbol
            """, [exchange.upper(), interval]).fetchall()
        return [row[0] for row in result]
    except Exception as e:
        print(f"⚠️  Could not fetch symbols from Historify DB: {e}")
        return []

def run_production_scan():
    print("\n" + "="*70)
    print("🚀 OPENALGO TRADER: TOP 5 EXPLOSIVE SQUEEZE PICKS")
    print("="*70)
    
    universe = get_historify_symbols(exchange='NSE', interval='D')
    if not universe:
        print("\n❌ No symbols found in Historify DB. Please download Daily (D) data first.")
        return

    print(f"Scanning {len(universe)} symbols from Historify DB...")
    
    results = []
    MIN_DAYS = 30 
    
    for symbol in universe:
        try:
            # Fetch directly from Historify DB
            df = get_ohlcv(symbol=symbol, exchange='NSE', interval='D')
            
            if df is None or len(df) < MIN_DAYS:
                continue

            # 1. Calc Volatility
            df_vol = compute_ohlc_volatility(df)
            
            # 2. Squeeze Ratio (Targeting < 0.95 like backtest)
            current_yz = df_vol['vol_yz_ann'].iloc[-1]
            avg_yz = df_vol['vol_yz_ann'].rolling(10).mean().iloc[-1]
            squeeze = current_yz / avg_yz if avg_yz > 0 else 1.0
            
            # 3. NNLS ML Prediction for Tomorrow
            X, y, _ = prepare_ml_dataset(df_vol, horizon=1, window=10)
            if len(X) < 10: continue
            
            weights, _ = train_nnls_model(X.values, y.values)
            latest_feat = [compute_lwma(df_vol[est], 10).iloc[-1] for est in ['vol_yz_ann', 'vol_rs_ann', 'vol_gk_ann', 'vol_parkinson_ann']]
            
            pred_vol = np.dot(latest_feat, weights)
            exp_pot = pred_vol / current_yz if current_yz > 0 else 1.0
            
            # 4. Indicators
            volm_spike = df['volume'].iloc[-1] / df['volume'].tail(5).mean()
            sma20 = df['close'].rolling(20).mean().iloc[-1]
            curr_price = df['close'].iloc[-1]
            trend = "🟢 BULL" if curr_price > sma20 else "🔴 BEAR"

            # Filter like the master backtest
            if squeeze < 1.0:
                results.append({
                    "Symbol": symbol,
                    "Price": round(curr_price, 2),
                    "Squeeze": round(squeeze, 2),
                    "Trend": trend,
                    "Exp_Pot": round(exp_pot, 2),
                    "Score": round(exp_pot / squeeze * volm_spike, 2)
                })
        except Exception:
            continue

    if not results:
        print("\n❌ No setups found. Ensure you have 'Daily (D)' data in Historify.")
        return

    # THE TOP 5 LIST
    df_final = pd.DataFrame(results).sort_values(by="Score", ascending=False).head(5)
    
    print("\n✅ ANALYSIS COMPLETE. HERE ARE YOUR TOP 5 PICKS FOR TOMORROW:")
    print("-" * 75)
    print(df_final.to_string(index=False))
    print("-" * 75)
    
    print("\nTRADING INSTRUCTIONS:")
    print("1. 🟢 BULL Trend: BUY only if price breaks TODAY'S High.")
    print("2. 🔴 BEAR Trend: SELL only if price breaks TODAY'S Low.")
    print("3. Target: 2.0% - 2.5% | Stop-Loss: 1.0% (from entry price).")

if __name__ == "__main__":
    run_production_scan()
