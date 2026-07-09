import numpy as np
import pandas as pd
from scipy.optimize import nnls

def compute_lwma(series: pd.Series, window: int) -> pd.Series:
    if len(series) < window:
        return pd.Series([np.nan] * len(series), index=series.index)
    weights = np.arange(1, window + 1)
    return series.rolling(window).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def prepare_ml_dataset(df: pd.DataFrame, horizon: int = 5, window: int = 10):
    df = df.copy()
    df['log_ret'] = np.log(df['close'] / df['close'].shift(1))
    
    if horizon == 1:
        df['target_vol'] = np.abs(df['log_ret'].shift(-1)) * np.sqrt(252) * 100
    else:
        future_var = df['log_ret'].shift(-horizon).rolling(horizon).var()
        df['target_vol'] = np.sqrt(252 * future_var) * 100
    
    estimators = ['vol_yz_ann', 'vol_rs_ann', 'vol_gk_ann', 'vol_parkinson_ann']
    X_cols = []
    for est in estimators:
        col_name = f'lwma_{est}'
        df[col_name] = compute_lwma(df[est], window)
        X_cols.append(col_name)
    
    ml_df = df.dropna(subset=['target_vol'] + X_cols)
    return ml_df[X_cols], ml_df['target_vol'], X_cols

def train_nnls_model(X, y):
    weights, rnorm = nnls(X, y)
    return weights, rnorm
