"""Indian F&O charge model + slippage.

Generalised per-leg version of
``backtesting/nifty_options_selling/nifty_options_selling_backtest.py``'s
``compute_charges`` so it works for any number of legs (naked 2-leg strangle or
4-leg iron condor). Same component rates, so results stay comparable to the
existing repo backtests.

A "leg" is one option contract held open->close:
  * short = sell-to-open, buy-to-close   (STT on the open sell, stamp on the close buy)
  * long  = buy-to-open,  sell-to-close  (STT on the close sell, stamp on the open buy)
"""

from __future__ import annotations

import os

# Component rates (identical to the nifty_options_selling backtest).
BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.001        # 0.1% on the sell-side premium turnover
TXN_CHARGE_PCT = 0.0003553  # NSE options txn charge on premium turnover
SEBI_PER_CRORE = 10.0
GST_PCT = 0.18
STAMP_BUY_PCT = 0.00003     # 0.003% on the buy-side premium turnover

# Premium points lost to slippage per leg per fill (entry + exit => 2x).
SLIPPAGE_PTS = float(os.environ.get("SLIPPAGE_PTS", 1.0))


def leg_charges(side: str, entry_px: float, exit_px: float, qty: int) -> float:
    """Round-trip charges for a single option leg (entry order + exit order)."""
    brokerage = BROKERAGE_PER_ORDER * 2  # open + close
    turnover = (entry_px + exit_px) * qty
    txn = TXN_CHARGE_PCT * turnover
    sebi = SEBI_PER_CRORE * turnover / 1e7
    if side == "short":
        stt = STT_SELL_PCT * (entry_px * qty)     # sold to open
        stamp = STAMP_BUY_PCT * (exit_px * qty)   # bought to close
    else:  # long
        stt = STT_SELL_PCT * (exit_px * qty)      # sold to close
        stamp = STAMP_BUY_PCT * (entry_px * qty)  # bought to open
    gst = GST_PCT * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + gst + stamp
