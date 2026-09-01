#!/usr/bin/env python
"""
Survivor Options Strategy - FAITHFUL REPLICA OF THE UPSTREAM ORIGINAL
=====================================================================

Behavioural twin of github.com/Raahi-Bhushan/trading-algo -> strategy/survivor.py
(local clone: backtesting/trading-algo/strategy/survivor.py), re-expressed
against the OpenAlgo SDK so it can be uploaded and run/compared head-to-head
with our maintained version.

WHY THIS FILE EXISTS
--------------------
Our maintained version (strategies/Survivor.py, deployed as
strategies/scripts/survivor_upload_20260409093947.py) deliberately deviates
from upstream in a few places. This file does NOT carry those deviations, so
you can A/B the two and see which behaviour you actually want live.

EXACT DIFFERENCES vs OUR VERSION
--------------------------------
1. STRIKE SEARCH STEP.
   Original decrements the strike-search gap by lot_size; ours uses the
   strike interval (strike_difference).
       original:  temp_gap -= self.lot_size          # 65 today
       ours:      temp_gap -= self.strike_difference # 50 today
   From a 200pt gap the ladders diverge:
       original: 200 -> 135 -> 70 -> 5 -> -60 ...
       ours:     200 -> 150 -> 100 -> 50 -> 0 (stops)
   Upstream decrements a PRICE gap by a QUANTITY, which is dimensionally
   wrong, but it is what the validated live version did - so it is preserved
   here verbatim.

2. UNBOUNDED SEARCH LOOP  ** READ THIS BEFORE RUNNING LIVE **
   Original uses `while True:` with no lower bound on temp_gap. Once temp_gap
   goes negative the sign flips inside _find_option_strike, and a "PE 200
   points below spot" request silently becomes a PE ABOVE spot - i.e. a deep
   IN-THE-MONEY put, which has a fat premium, sails past the
   min_price_to_sell check, and gets SOLD. Selling ITM options is a very
   different risk profile from the intended OTM premium capture.
   Ours guards this with `while temp_gap > 0`. This file does not, because
   the point is to replicate upstream. Reproduced deliberately, flagged
   loudly - do not run this unattended without understanding it.

3. NO EXPIRY AUTO-ROLL.
   Upstream treats symbol_initials as a static config value (survivor.py:86)
   and has no rollover logic at all, so this file needs symbol_initials
   hand-edited every expiry. Ours auto-discovers the live nearest expiry via
   client.expiry() and rolls automatically, with a noon cutoff on expiry day.

4. RAW QUANTITY, NOT LOT-DERIVED.
   Upstream: total_quantity = sell_multiplier * pe_quantity, where
   pe_quantity is a raw number (upstream default 75). If the exchange lot
   size changes, that number silently becomes an invalid lot multiple and
   every order is rejected. Ours derives quantity from the live master
   contract lot size while preserving the intended number of lots.
   NOTE: upstream's default of 75 matched NIFTY's lot size at the time. The
   default below is 65 (today's actual NIFTY lot) purely so this file is
   runnable right now - the LOGIC is upstream's, unchanged.

BEHAVIOURS THAT ARE IDENTICAL IN BOTH (i.e. upstream characteristics, not
ours to blame or fix here):
  - Multiplier-breach returns BEFORE advancing the reference value
    (survivor.py:239-244), so a move larger than
    sell_multiplier_threshold * gap freezes that side until price retraces
    back inside the window. On the PE side _reset_reference_values cannot
    rescue it, because that only fires when price falls BELOW the reference.
  - No stop-loss, no target, no square-off. Pure SELL, positions ride to
    expiry/settlement.

STRATEGY: Sells OTM options when NIFTY moves beyond gap thresholds.
- NIFTY moves UP   beyond pe_gap -> sell OTM PUT
- NIFTY moves DOWN beyond ce_gap -> sell OTM CALL

Upload via OpenAlgo UI -> Python Strategy -> New Strategy.
Strongly recommended: run under Analyzer Mode (/analyzer) first - orders are
intercepted server-side and never reach the broker.
"""
from openalgo import api
import os
import time
import pandas as pd
from datetime import datetime

# ============================================================================
# CONFIGURATION - Edit these values before uploading
# ============================================================================
CONFIG = {
    # Symbol Configuration
    "index_symbol": "NIFTY",            # Underlying index for price tracking
    "index_exchange": "NSE_INDEX",      # Exchange for index quotes
    # STATIC, as upstream. No auto-roll in this file - you MUST update this
    # by hand every expiry (format: [UNDERLYING][DD][MMM][YY]).
    "symbol_initials": "NIFTY25AUG26",
    "option_exchange": "NFO",           # Exchange for options

    # Gap Parameters (Trade Triggers)  - upstream defaults
    "pe_gap": 20,             # Points NIFTY must rise to trigger PE sell
    "ce_gap": 20,             # Points NIFTY must fall to trigger CE sell

    # Strike Selection (distance from spot in points) - upstream defaults
    "pe_symbol_gap": 200,     # PE strike = spot - 200
    "ce_symbol_gap": 200,     # CE strike = spot + 200

    # Position Sizing - RAW quantity, upstream semantics (see header note 4).
    # Upstream default was 75; 65 here = today's real NIFTY lot size.
    "pe_quantity": 65,        # total qty = pe_quantity * sell_multiplier
    "ce_quantity": 65,        # total qty = ce_quantity * sell_multiplier

    # Risk Management - upstream defaults
    "min_price_to_sell": 15,           # Min option premium to sell
    "sell_multiplier_threshold": 5,    # Max multiplier for position scaling

    # Reset Parameters - upstream defaults
    "pe_reset_gap": 30,       # PE reference reset threshold
    "ce_reset_gap": 30,       # CE reference reset threshold

    # Starting Reference (0 = use current LTP at startup)
    "pe_start_point": 0,
    "ce_start_point": 0,

    # Order Execution
    "product": "NRML",        # upstream used ProductType.MARGIN (== NRML)
    "strategy_name": "Survivor_Original",  # distinct tag so live order logs
                                           # never mix with our version

    # Polling Interval
    "poll_interval": 1,       # Seconds between quote checks
}

# ============================================================================
# STRATEGY CODE
# ============================================================================
api_key = "1c8082e53ae0e56b26cfba2c4ffe95dd181c2322eb60c5eeeb3d22540ca6540c"
# Alternative: api_key = os.getenv('OPENALGO_APIKEY')

if not api_key or api_key == "YOUR_OPENALGO_API_KEY_HERE":
    print("ERROR: Please set your OpenAlgo API key in the strategy file.")
    print("You can find your API key at: http://127.0.0.1:5000/apikey")
    exit(1)

client = api(api_key=api_key, host='http://127.0.0.1:5000')


class SurvivorOriginalStrategy:
    """Upstream-faithful Survivor. See module docstring for the deltas."""

    def __init__(self, config):
        self.config = config
        self.instruments_df = None
        self.strike_difference = None
        self.lot_size = None

        # State (upstream: pe_reset_gap_flag / ce_reset_gap_flag)
        self.pe_reset_flag = 0
        self.ce_reset_flag = 0
        self.nifty_pe_last_value = 0
        self.nifty_ce_last_value = 0

        self._load_instruments()
        self._initialize_state()

    def _load_instruments(self):
        """
        Download and filter instruments for the STATIC configured series.
        Upstream has no expiry discovery/rollover - symbol_initials is used
        exactly as configured.
        """
        print("Downloading NFO instruments...")
        result = client.instruments(exchange="NFO")

        if not (isinstance(result, pd.DataFrame) and not result.empty):
            print(f"ERROR: Failed to download instruments: {result}")
            return

        prefix = self.config['symbol_initials']
        self.instruments_df = result[result['symbol'].str.startswith(prefix)]
        print(f"Found {len(self.instruments_df)} instruments for {prefix}")

        if self.instruments_df.empty:
            print(f"ERROR: No instruments found for {prefix}")
            print("This file has NO auto-roll - update symbol_initials by hand.")
            print("Format: [UNDERLYING][DD][MMM][YY] e.g. NIFTY25AUG26")
            return

        self.lot_size = int(self.instruments_df['lotsize'].iloc[0])
        print(f"Lot size: {self.lot_size}")
        self._calculate_strike_difference()

    def _calculate_strike_difference(self):
        """Strike interval for the series (upstream _get_strike_difference)."""
        if 'instrumenttype' in self.instruments_df.columns:
            ce = self.instruments_df[self.instruments_df['instrumenttype'] == 'CE'].copy()
        else:
            ce = self.instruments_df[self.instruments_df['symbol'].str.endswith('CE')].copy()

        if len(ce) < 2:
            print("ERROR: Not enough CE instruments to calculate strike difference")
            self.strike_difference = 50
            return

        ce['strike'] = pd.to_numeric(ce['strike'], errors='coerce')
        top2 = ce.sort_values('strike').head(2)
        self.strike_difference = abs(float(top2.iloc[1]['strike']) - float(top2.iloc[0]['strike']))
        print(f"Strike difference: {self.strike_difference}")

    def _initialize_state(self):
        """Initialize PE/CE reference values from LTP (upstream lines 118-137)."""
        quote = client.quotes(symbol=self.config['index_symbol'],
                              exchange=self.config['index_exchange'])
        if quote.get('status') == 'success':
            ltp = float(quote.get('data', {}).get('ltp', 0))
        else:
            print(f"WARNING: Could not get index quote, using 0: {quote}")
            ltp = 0

        self.nifty_pe_last_value = self.config['pe_start_point'] or ltp
        self.nifty_ce_last_value = self.config['ce_start_point'] or ltp
        print(f"Initialized - PE ref: {self.nifty_pe_last_value}, "
              f"CE ref: {self.nifty_ce_last_value}, LTP: {ltp}")

    def get_nifty_ltp(self):
        quote = client.quotes(symbol=self.config['index_symbol'],
                              exchange=self.config['index_exchange'])
        if quote.get('status') == 'success':
            return float(quote.get('data', {}).get('ltp', 0))
        return 0

    def process_tick(self, current_price):
        """Upstream on_ticks_update (line 160)."""
        if current_price <= 0:
            return
        self._handle_pe_trade(current_price)
        self._handle_ce_trade(current_price)
        self._reset_reference_values(current_price)

    def _handle_pe_trade(self, current_price):
        """Sell PE when NIFTY moves UP beyond pe_gap (upstream line 205)."""
        if current_price <= self.nifty_pe_last_value:
            return

        price_diff = round(current_price - self.nifty_pe_last_value, 0)
        if price_diff <= self.config['pe_gap']:
            return

        sell_multiplier = int(price_diff / self.config['pe_gap'])

        # Upstream returns HERE, before advancing the reference - so a large
        # move freezes this side until price retraces. Preserved verbatim.
        if sell_multiplier > self.config['sell_multiplier_threshold']:
            print(f"WARNING: Sell multiplier {sell_multiplier} breached the threshold "
                  f"{self.config['sell_multiplier_threshold']}")
            return

        self.nifty_pe_last_value += self.config['pe_gap'] * sell_multiplier
        # Upstream: raw config quantity, NOT derived from the live lot size.
        total_qty = sell_multiplier * self.config['pe_quantity']

        option_symbol = self._find_option_strike("PE", current_price,
                                                 self.config['pe_symbol_gap'])
        if option_symbol:
            self._place_sell_order(option_symbol, total_qty)
            self.pe_reset_flag = 1

    def _handle_ce_trade(self, current_price):
        """Sell CE when NIFTY moves DOWN beyond ce_gap (upstream line 282)."""
        if current_price >= self.nifty_ce_last_value:
            return

        price_diff = round(self.nifty_ce_last_value - current_price, 0)
        if price_diff <= self.config['ce_gap']:
            return

        sell_multiplier = int(price_diff / self.config['ce_gap'])

        if sell_multiplier > self.config['sell_multiplier_threshold']:
            print(f"WARNING: Sell multiplier {sell_multiplier} breached the threshold "
                  f"{self.config['sell_multiplier_threshold']}")
            return

        self.nifty_ce_last_value -= self.config['ce_gap'] * sell_multiplier
        total_qty = sell_multiplier * self.config['ce_quantity']

        option_symbol = self._find_option_strike("CE", current_price,
                                                 self.config['ce_symbol_gap'])
        if option_symbol:
            self._place_sell_order(option_symbol, total_qty)
            self.ce_reset_flag = 1

    def _reset_reference_values(self, current_price):
        """Upstream _reset_reference_values (line 356)."""
        if (self.nifty_pe_last_value - current_price) > self.config['pe_reset_gap'] \
                and self.pe_reset_flag:
            new_val = current_price + self.config['pe_reset_gap']
            print(f"Resetting PE value from {self.nifty_pe_last_value} to {new_val}")
            self.nifty_pe_last_value = new_val

        if (current_price - self.nifty_ce_last_value) > self.config['ce_reset_gap'] \
                and self.ce_reset_flag:
            new_val = current_price - self.config['ce_reset_gap']
            print(f"Resetting CE value from {self.nifty_ce_last_value} to {new_val}")
            self.nifty_ce_last_value = new_val

    def _find_option_strike(self, option_type, ltp, gap):
        """
        Upstream _find_nifty_symbol_from_gap + _find_price_eligible_symbol.

        ** UNBOUNDED, AS UPSTREAM ** - `while True` with `temp_gap -= lot_size`
        and no lower bound. Once temp_gap goes negative the target flips to
        the wrong side of spot (a "PE below spot" request becomes an ITM PE),
        which will pass the premium floor and be sold. See header note 2.
        """
        if self.instruments_df is None or self.instruments_df.empty:
            return None

        temp_gap = gap
        while True:  # upstream: no temp_gap > 0 guard
            # Upstream: PE -> ltp - gap, CE -> ltp + gap. With a negative
            # temp_gap this deliberately inverts, exactly as upstream does.
            target_strike = ltp - temp_gap if option_type == "PE" else ltp + temp_gap

            if 'instrumenttype' in self.instruments_df.columns:
                df = self.instruments_df[
                    self.instruments_df['instrumenttype'] == option_type
                ].copy()
            else:
                df = self.instruments_df[
                    self.instruments_df['symbol'].str.endswith(option_type)
                ].copy()

            if df.empty:
                return None

            df['strike'] = pd.to_numeric(df['strike'], errors='coerce')
            df['strike_diff'] = (df['strike'] - target_strike).abs()

            tolerance = self.strike_difference / 2 if self.strike_difference else 25
            df = df[df['strike_diff'] <= tolerance]

            if df.empty:
                print(f"No instrument found for {option_type} within {tolerance} "
                      f"of {target_strike}")
                return None

            best = df.sort_values('strike_diff').iloc[0]
            symbol = best['symbol']

            quote = client.quotes(symbol=symbol, exchange=self.config['option_exchange'])
            if quote.get('status') != 'success':
                print(f"Quote failed for {symbol}: {quote}")
                return None

            premium = float(quote.get('data', {}).get('ltp', 0))
            if premium >= self.config['min_price_to_sell']:
                print(f"Found {option_type} strike: {symbol} (premium: {premium})")
                return symbol

            print(f"Last price {premium} is less than min price to sell "
                  f"{self.config['min_price_to_sell']}")
            # Upstream steps by LOT SIZE, not by the strike interval.
            step = self.lot_size if self.lot_size else 50
            temp_gap -= step

    def _place_sell_order(self, symbol, quantity):
        print(f"Execute {self.config['strategy_name']} sell @ {symbol} x {quantity}, Market Price")
        response = client.placeorder(
            symbol=symbol,
            action="SELL",
            exchange=self.config['option_exchange'],
            quantity=quantity,
            price_type="MARKET",
            product=self.config['product'],
            strategy=self.config['strategy_name'],
        )
        if response.get('status') == 'success':
            print(f"Order placed: {response.get('orderid', 'N/A')} | SELL {symbol} x {quantity}")
        else:
            print(f"Order FAILED: {response.get('message', response)}")


def main():
    print("=" * 60)
    print("SURVIVOR (UPSTREAM REPLICA) STARTING")
    print(f"Symbol: {CONFIG['symbol_initials']}  [STATIC - no auto-roll]")
    print(f"Index: {CONFIG['index_symbol']} ({CONFIG['index_exchange']})")
    print(f"PE Gap: {CONFIG['pe_gap']} | CE Gap: {CONFIG['ce_gap']}")
    print(f"PE Qty: {CONFIG['pe_quantity']} | CE Qty: {CONFIG['ce_quantity']} [raw, not lot-derived]")
    print(f"Max Multiplier: {CONFIG['sell_multiplier_threshold']}x")
    print(f"Strategy tag: {CONFIG['strategy_name']}")
    print("=" * 60)

    strategy = SurvivorOriginalStrategy(CONFIG)

    if strategy.instruments_df is None or strategy.instruments_df.empty:
        print("FATAL: No instruments loaded. Exiting.")
        return

    print("Strategy initialized. Starting monitoring loop...")

    while True:
        try:
            ltp = strategy.get_nifty_ltp()
            if ltp > 0:
                strategy.process_tick(ltp)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"NIFTY: {ltp} | PE ref: {strategy.nifty_pe_last_value} | "
                      f"CE ref: {strategy.nifty_ce_last_value}")
            else:
                print("WARNING: Got zero LTP, skipping tick")

            time.sleep(CONFIG['poll_interval'])

        except KeyboardInterrupt:
            print("Strategy stopped by user")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(5)
            continue

    print("SURVIVOR (UPSTREAM REPLICA) STOPPED")


if __name__ == "__main__":
    main()
