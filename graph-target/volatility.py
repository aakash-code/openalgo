import numpy as np
import pandas as pd

def compute_ohlc_volatility(df: pd.DataFrame, window: int = 21, trading_days: int = 252) -> pd.DataFrame:
    """
    Computes various OHLC-based historical volatility measures.
    
    Args:
        df: DataFrame with columns ['open', 'high', 'low', 'close']
        window: Rolling window for Yang-Zhang calculation (default 21)
        trading_days: Annualization factor (default 252 for stocks, 365 for crypto)
        
    Returns:
        DataFrame with added volatility columns (annualized percentages)
    """
    # Standardize column names to lowercase for consistency
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    
    # 1. Log Returns
    # Close-to-Close
    df['ret_cc'] = np.log(c / c.shift(1))
    # Close-to-Open (Overnight)
    df['ret_co'] = np.log(o / c.shift(1))
    # Open-to-Close (Daytime)
    df['ret_oc'] = np.log(c / o)
    
    # 2. Rogers-Satchell (1991) - Drift Independent
    # Daily variance
    rs_var = (np.log(h/o) * np.log(h/c) + np.log(l/o) * np.log(l/c))
    df['vol_rs_ann'] = np.sqrt(trading_days * rs_var) * 100
    
    # 3. Yang-Zhang (2000) - Minimum Variance Unbiased
    # k = constant for minimum variance
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    
    # Rolling variances
    var_overnight = df['ret_co'].rolling(window).var()
    var_daytime = df['ret_oc'].rolling(window).var()
    var_rs = rs_var.rolling(window).mean()
    
    yz_var = var_overnight + k * var_daytime + (1.0 - k) * var_rs
    df['vol_yz_ann'] = np.sqrt(trading_days * yz_var) * 100
    
    # 4. Garman-Klass (1980) - Extension of Parkinson (includes Open/Close)
    # 0.5 * ln(H/L)^2 - (2*ln(2) - 1) * ln(C/O)^2
    gk_var = 0.5 * (np.log(h/l)**2) - (2 * np.log(2) - 1) * (np.log(c/o)**2)
    df['vol_gk_ann'] = np.sqrt(trading_days * gk_var) * 100
    
    # 5. Parkinson (High-Low)
    park_var = (1.0 / (4.0 * np.log(2.0))) * (np.log(h/l)**2)
    df['vol_parkinson_ann'] = np.sqrt(trading_days * park_var) * 100
    
    return df
