#!/usr/bin/env python
"""
Survivor Options Trading Strategy - OpenAlgo Upload Ready
==========================================================

READABLE REFERENCE COPY - NOT THE LIVE DEPLOYMENT.
The strategy actually scheduled/run by OpenAlgo is
strategies/scripts/survivor_upload_20260409093947.py (registered in
strategy_configs.json as "Survivor Upload"). Editing THIS file has no effect
on live trading; edit the file above, or re-upload this one via OpenAlgo UI.

STRATEGY: Sells OTM options when NIFTY moves beyond gap thresholds.
- When NIFTY moves UP beyond pe_gap -> Sells OTM Put options
- When NIFTY moves DOWN beyond ce_gap -> Sells OTM Call options

Upload this file via OpenAlgo UI -> Python Strategy -> New Strategy
Configure schedule: Mon-Fri, 09:15 to 15:20 IST

CONFIGURATION: Edit the CONFIG section below before uploading.
"""
from openalgo import api
import os
import time
import pandas as pd
from datetime import datetime

# How often (seconds) to check whether a newer expiry series is now the
# nearest live one. Expiries only change weekly, so there's no need to hit
# client.expiry() on every 1s poll tick.
EXPIRY_CHECK_INTERVAL_SEC = 300

# If a rollover is detected but the instrument reload fails (network blip),
# retry this soon instead of waiting a full EXPIRY_CHECK_INTERVAL_SEC - until
# it succeeds we are deliberately still trading the OLD series, so the window
# should be short.
EXPIRY_RETRY_INTERVAL_SEC = 30

# ============================================================================
# CONFIGURATION - Edit these values before uploading
# ============================================================================
CONFIG = {
    # Symbol Configuration
    "index_symbol": "NIFTY",            # Underlying index for price tracking (OpenAlgo format)
    "index_exchange": "NSE_INDEX",      # Exchange for index quotes
    "symbol_initials": "NIFTY13APR26",  # Option series prefix (DDMMMYY expiry, e.g. NIFTY07AUG25).
                                         # FALLBACK ONLY - the strategy auto-discovers and rolls to
                                         # the live nearest expiry via client.expiry() at startup and
                                         # every EXPIRY_CHECK_INTERVAL_SEC; this is only used if that
                                         # lookup fails. No need to hand-edit it week to week.
    "option_exchange": "NFO",           # Exchange for options

    # Gap Parameters (Trade Triggers)
    "pe_gap": 20,             # Points NIFTY must rise to trigger PE sell
    "ce_gap": 20,             # Points NIFTY must fall to trigger CE sell

    # Strike Selection (distance from spot in points)
    "pe_symbol_gap": 200,     # PE strike = spot - 200
    "ce_symbol_gap": 200,     # CE strike = spot + 200

    # Position Sizing (must match broker lot size - currently 65 for NIFTY)
    "pe_quantity": 65,        # Base PE sell quantity (lot size)
    "ce_quantity": 65,        # Base CE sell quantity (lot size)

    # Risk Management
    "min_price_to_sell": 15,           # Min option premium to sell
    "sell_multiplier_threshold": 5,    # Max multiplier for position scaling

    # Reset Parameters (pulls reference back toward market)
    "pe_reset_gap": 30,       # PE reference reset threshold
    "ce_reset_gap": 30,       # CE reference reset threshold

    # Starting Reference (0 = use current LTP at startup)
    "pe_start_point": 0,
    "ce_start_point": 0,

    # Order Execution
    "product": "NRML",        # Product type for options
    "strategy_name": "Survivor",  # Strategy tag for order tracking

    # Polling Interval
    "poll_interval": 1,       # Seconds between quote checks

    # Expiry-day premium-spike guard: once it's the CURRENT series' own
    # expiry day, stop opening new positions in it after this IST time and
    # switch to next week's series instead (still selects fresh option
    # strikes normally from there - only WHICH series changes). Avoids
    # selling into the last few hours of an expiring contract, where gamma
    # risk and premium spikes are worst. Does not touch positions already
    # open in the expiring series - those still ride to settlement as before.
    "expiry_day_cutoff_hour": 12,
    "expiry_day_cutoff_minute": 0,

    # After a REJECTED order, wait this long before retrying that side. The
    # reference value is no longer consumed by a failed order (so the signal
    # isn't silently lost), which means without a cooldown the strategy would
    # re-attempt on every 1s poll and hammer the broker while it's rejecting -
    # e.g. the SEBI static-IP block returns 403 on every single call.
    "order_retry_cooldown_sec": 30,
}

# ============================================================================
# STRATEGY CODE - No changes needed below this line
# ============================================================================

# ============================================================================
# API KEY CONFIGURATION
# ============================================================================
# Option 1: Hardcode your API key here (get it from OpenAlgo UI -> API Key page)
api_key = "1c8082e53ae0e56b26cfba2c4ffe95dd181c2322eb60c5eeeb3d22540ca6540c"

# Option 2: Use environment variable (set OPENALGO_APIKEY before starting OpenAlgo)
# api_key = os.getenv('OPENALGO_APIKEY')

if not api_key or api_key == "YOUR_OPENALGO_API_KEY_HERE":
    print("ERROR: Please set your OpenAlgo API key in the strategy file.")
    print("Open survivor_upload.py and replace 'YOUR_OPENALGO_API_KEY_HERE' with your actual API key.")
    print("You can find your API key at: http://127.0.0.1:5000/apikey")
    exit(1)

# Initialize OpenAlgo client
client = api(api_key=api_key, host='http://127.0.0.1:5000')


class SurvivorStrategy:
    """Survivor Options Trading Strategy using OpenAlgo SDK"""

    def __init__(self, config):
        self.config = config
        self.instruments_df = None
        self.strike_difference = None
        self.lot_size = None

        # State
        self.pe_reset_flag = 0
        self.ce_reset_flag = 0
        self.nifty_pe_last_value = 0
        self.nifty_ce_last_value = 0

        # Last time we checked client.expiry() for a rollover (monotonic
        # seconds) - gates ensure_current_expiry() to EXPIRY_CHECK_INTERVAL_SEC.
        self._last_expiry_check = 0

        # Per-side backoff after a rejected order: monotonic timestamp of the
        # last failure, and how many have failed back to back.
        self._order_block_until = {"PE": 0.0, "CE": 0.0}
        self._consecutive_failures = {"PE": 0, "CE": 0}

        # Initialize
        self._load_instruments()
        self._last_expiry_check = time.monotonic()  # startup load already fetched it fresh
        self._initialize_state()

    def _compute_current_prefix(self):
        """
        Ask OpenAlgo's live master-contract table for the nearest live expiry
        (already sorted nearest-first, already excludes expired dates - see
        services/expiry_service.py) and turn it into the symbol_initials
        format this script already uses, e.g. "13-APR-26" -> "NIFTY13APR26".
        Returns None on any failure/empty response so callers keep whatever
        series is already working rather than clearing it over a transient
        API hiccup.

        Expiry-day guard: if the nearest series expires TODAY and it's at or
        past expiry_day_cutoff_hour:minute (IST), switch to the NEXT series
        instead - avoids opening new positions into the last few hours of an
        expiring contract, where premium can spike on gamma risk with almost
        no time value left to cushion it. Before the cutoff on expiry day
        itself, the expiring series is still used normally (that's when
        theta decay is fastest, the whole point of selling it).
        """
        try:
            resp = client.expiry(
                symbol=self.config['index_symbol'],
                exchange=self.config['option_exchange'],
                instrumenttype='options',
            )
        except Exception as e:
            print(f"WARNING: client.expiry() failed: {e}")
            return None

        if resp.get('status') != 'success':
            print(f"WARNING: client.expiry() returned non-success: {resp}")
            return None

        dates = resp.get('data') or []
        if not dates:
            print("WARNING: client.expiry() returned no live expiry dates")
            return None

        chosen = dates[0]
        try:
            nearest_date = datetime.strptime(dates[0], "%d-%b-%y").date()
        except ValueError:
            nearest_date = None  # unparseable - fall through, use dates[0] as-is

        now = datetime.now()
        if nearest_date == now.date():
            cutoff = now.replace(
                hour=self.config['expiry_day_cutoff_hour'],
                minute=self.config['expiry_day_cutoff_minute'],
                second=0, microsecond=0,
            )
            if now >= cutoff:
                if len(dates) > 1:
                    print(f"Expiry-day cutoff reached ({now.strftime('%H:%M')}) - "
                          f"switching from today's expiring {dates[0]} to next series {dates[1]}")
                    chosen = dates[1]
                else:
                    print("WARNING: expiry-day cutoff reached but no next expiry "
                          f"available yet - staying on {dates[0]}")

        nearest = chosen.replace('-', '').upper()  # "13-APR-26" -> "13APR26"
        return self.config['index_symbol'] + nearest

    def ensure_current_expiry(self, force=False):
        """
        Re-check the nearest live expiry and reload instruments if it has
        rolled since we last loaded. Called once per main-loop tick but only
        actually hits client.expiry() every EXPIRY_CHECK_INTERVAL_SEC - safe
        to call as often as you like.

        Deliberately does NOT touch nifty_pe_last_value/nifty_ce_last_value/
        reset flags - those track NIFTY spot price gaps, unrelated to which
        option series is being sold, and must survive a rollover untouched.
        """
        now = time.monotonic()
        if not force and (now - self._last_expiry_check) < EXPIRY_CHECK_INTERVAL_SEC:
            return
        self._last_expiry_check = now

        current = self._compute_current_prefix()
        if current is None:
            return  # keep trading the last known-good series
        if current == self.config['symbol_initials']:
            return  # no rollover

        previous = self.config['symbol_initials']
        print(f"Expiry rolled: {previous} -> {current}")
        # Pass the prefix explicitly: _load_instruments only commits it (and
        # the matching instruments_df) if the load actually succeeds, so a
        # failure here leaves us cleanly on the previous series rather than
        # half-rolled. Also avoids a second redundant client.expiry() call.
        if not self._load_instruments(prefix=current):
            print(f"WARNING: rollover to {current} FAILED - still trading {previous}. "
                  f"Retrying in {EXPIRY_RETRY_INTERVAL_SEC}s.")
            self._last_expiry_check = now - EXPIRY_CHECK_INTERVAL_SEC + EXPIRY_RETRY_INTERVAL_SEC

    def _load_instruments(self, prefix=None):
        """
        Download and filter instruments for an option series.

        ATOMIC: nothing on self is mutated unless the load fully succeeds.
        Previously a failed download left self.instruments_df holding the
        PREVIOUS series' rows while symbol_initials had already advanced to
        the new one - so a network blip during a rollover meant the strategy
        kept selling the expiring series while reporting the new one.

        Args:
            prefix: series to load, e.g. "NIFTY25AUG26". None = auto-discover
                    the current live series via _compute_current_prefix(),
                    falling back to the configured symbol_initials.
        Returns:
            True if instruments were loaded and committed, False otherwise
            (caller keeps whatever series was already working).
        """
        if prefix is None:
            # Prefer the live nearest expiry from OpenAlgo's master contract
            # table over the hardcoded CONFIG value - that value is now only a
            # fallback for when the expiry API itself is unreachable at startup.
            prefix = self._compute_current_prefix()
            if prefix:
                if prefix != self.config['symbol_initials']:
                    print(f"Using live expiry {prefix} (config had {self.config['symbol_initials']})")
            else:
                prefix = self.config['symbol_initials']
                print(f"WARNING: could not resolve live expiry, falling back to configured "
                      f"symbol_initials={prefix}")

        print(f"Downloading NFO instruments...")

        # Retry: market open is exactly when this call is slowest — OpenAlgo is
        # refreshing master contracts, the broker is busiest, and the NFO dump is
        # ~7.5 MB. A single timed-out attempt used to be fatal: on 2026-08-26 it
        # exited the strategy at 09:14, and the same endpoint answered in 1.9s
        # moments later. Losing the whole session to one slow response is a far
        # worse outcome than waiting a few seconds.
        attempts = 3
        result = None
        for attempt in range(1, attempts + 1):
            result = client.instruments(exchange="NFO")
            if isinstance(result, pd.DataFrame) and not result.empty:
                break
            if attempt < attempts:
                delay = 2 ** attempt  # 2s, then 4s
                print(f"WARNING: instrument download attempt {attempt}/{attempts} "
                      f"failed ({result}); retrying in {delay}s")
                time.sleep(delay)

        if not (isinstance(result, pd.DataFrame) and not result.empty):
            print(f"ERROR: Failed to download instruments after {attempts} attempts: {result}")
            return False

        # Filter for our option series using symbol prefix
        # OpenAlgo symbols: NIFTY07AUG2521500CE, NIFTY07AUG2521000PE, etc.
        df = result[result['symbol'].str.startswith(prefix)]
        print(f"Found {len(df)} instruments for {prefix}")

        if df.empty:
            print(f"ERROR: No instruments found for {prefix}")
            print("Check that symbol_initials matches the current expiry series.")
            print("Format: [UNDERLYING][DD][MMM][YY] e.g. NIFTY07AUG25")
            return False

        # All checks passed - commit the new series as one unit.
        self.instruments_df = df
        self.config['symbol_initials'] = prefix

        # Get lot size
        self.lot_size = int(df['lotsize'].iloc[0])
        print(f"Lot size: {self.lot_size}")
        for side, key in (("PE", "pe_quantity"), ("CE", "ce_quantity")):
            configured = self.config[key]
            if configured % self.lot_size != 0:
                print(f"NOTE: {key}={configured} is not a multiple of the live lot size "
                      f"{self.lot_size} - {side} orders will be rounded to the nearest "
                      f"whole lot (the exchange rejects non-lot-multiple quantities)")

        # Calculate strike difference
        self._calculate_strike_difference()
        return True

    def _calculate_strike_difference(self):
        """Calculate strike price interval for the option series"""
        # Use instrumenttype column if available, otherwise fallback to symbol suffix
        if 'instrumenttype' in self.instruments_df.columns:
            ce_instruments = self.instruments_df[
                self.instruments_df['instrumenttype'] == 'CE'
            ].copy()
        else:
            ce_instruments = self.instruments_df[
                self.instruments_df['symbol'].str.endswith('CE')
            ].copy()

        if len(ce_instruments) < 2:
            print("ERROR: Not enough CE instruments to calculate strike difference")
            self.strike_difference = 50  # Default
            return

        ce_instruments['strike'] = pd.to_numeric(ce_instruments['strike'], errors='coerce')
        ce_sorted = ce_instruments.sort_values('strike')
        top2 = ce_sorted.head(2)
        self.strike_difference = abs(float(top2.iloc[1]['strike']) - float(top2.iloc[0]['strike']))
        print(f"Strike difference: {self.strike_difference}")

    def _initialize_state(self):
        """Initialize PE/CE reference values"""
        quote = client.quotes(symbol=self.config['index_symbol'], exchange=self.config['index_exchange'])

        if quote.get('status') == 'success':
            ltp = float(quote.get('data', {}).get('ltp', 0))
        else:
            print(f"WARNING: Could not get index quote, using 0: {quote}")
            ltp = 0

        self.nifty_pe_last_value = self.config['pe_start_point'] if self.config['pe_start_point'] != 0 else ltp
        self.nifty_ce_last_value = self.config['ce_start_point'] if self.config['ce_start_point'] != 0 else ltp

        print(f"Initialized - PE ref: {self.nifty_pe_last_value}, CE ref: {self.nifty_ce_last_value}, LTP: {ltp}")

    def get_nifty_ltp(self):
        """Get current NIFTY LTP"""
        quote = client.quotes(symbol=self.config['index_symbol'], exchange=self.config['index_exchange'])
        if quote.get('status') == 'success':
            return float(quote.get('data', {}).get('ltp', 0))
        return 0

    def _base_qty(self, side):
        """
        Quantity for ONE multiplier unit.

        Derives from the REAL lot size in the master contract rather than the
        hardcoded config number. NIFTY's lot size has already changed 25 -> 75
        -> 65 historically; the exchange rejects any quantity that isn't an
        exact lot multiple, so a hardcoded 65 silently breaks every order the
        next time SEBI revises it.

        The configured value still decides HOW MANY LOTS to trade (it was
        written against whatever lot size applied at the time), so
        pe_quantity=130 with a 65 lot means 2 lots, and stays 2 lots (=150) if
        the lot size becomes 75. Never rounds down to zero.
        """
        configured = self.config['pe_quantity' if side == "PE" else 'ce_quantity']
        if not self.lot_size:
            return configured  # lot size unknown - fall back to config as-is
        lots = max(1, round(configured / self.lot_size))
        return lots * self.lot_size

    def process_tick(self, current_price):
        """Main strategy logic - called on each tick"""
        if current_price <= 0:
            return

        self._handle_pe_trade(current_price)
        self._handle_ce_trade(current_price)
        self._reset_reference_values(current_price)

    def _handle_pe_trade(self, current_price):
        """Sell PE when NIFTY moves UP beyond pe_gap"""
        if current_price <= self.nifty_pe_last_value:
            return

        price_diff = round(current_price - self.nifty_pe_last_value, 0)
        if price_diff <= self.config['pe_gap']:
            return

        sell_multiplier = int(price_diff / self.config['pe_gap'])

        if sell_multiplier > self.config['sell_multiplier_threshold']:
            print(f"WARNING: PE multiplier {sell_multiplier} exceeds threshold {self.config['sell_multiplier_threshold']}")
            return

        if self._is_backing_off("PE"):
            return

        total_qty = sell_multiplier * self._base_qty("PE")

        # Find suitable PE strike
        option_symbol = self._find_option_strike("PE", current_price, self.config['pe_symbol_gap'])
        if not option_symbol:
            return  # no strike found - leave the reference alone so this retries

        if not self._place_sell_order(option_symbol, total_qty, side="PE"):
            return  # rejected - reference NOT consumed, retried after cooldown

        # Only now that the position actually exists do we consume the trigger.
        # Advancing before this point (as the original did) meant a rejected
        # order silently ate the signal: the reference moved on as if filled,
        # the trade was never retried, and reset logic armed for a position we
        # did not hold.
        self.nifty_pe_last_value += self.config['pe_gap'] * sell_multiplier
        self.pe_reset_flag = 1

    def _handle_ce_trade(self, current_price):
        """Sell CE when NIFTY moves DOWN beyond ce_gap"""
        if current_price >= self.nifty_ce_last_value:
            return

        price_diff = round(self.nifty_ce_last_value - current_price, 0)
        if price_diff <= self.config['ce_gap']:
            return

        sell_multiplier = int(price_diff / self.config['ce_gap'])

        if sell_multiplier > self.config['sell_multiplier_threshold']:
            print(f"WARNING: CE multiplier {sell_multiplier} exceeds threshold {self.config['sell_multiplier_threshold']}")
            return

        if self._is_backing_off("CE"):
            return

        total_qty = sell_multiplier * self._base_qty("CE")

        # Find suitable CE strike
        option_symbol = self._find_option_strike("CE", current_price, self.config['ce_symbol_gap'])
        if not option_symbol:
            return  # no strike found - leave the reference alone so this retries

        if not self._place_sell_order(option_symbol, total_qty, side="CE"):
            return  # rejected - reference NOT consumed, retried after cooldown

        # See the matching comment in _handle_pe_trade: consume the trigger
        # only after the order is actually confirmed.
        self.nifty_ce_last_value -= self.config['ce_gap'] * sell_multiplier
        self.ce_reset_flag = 1

    def _reset_reference_values(self, current_price):
        """Reset references when market moves favorably"""
        # PE Reset: price dropped below PE reference
        if (self.nifty_pe_last_value - current_price) > self.config['pe_reset_gap'] and self.pe_reset_flag:
            new_val = current_price + self.config['pe_reset_gap']
            print(f"Resetting PE ref: {self.nifty_pe_last_value} -> {new_val}")
            self.nifty_pe_last_value = new_val

        # CE Reset: price rose above CE reference
        if (current_price - self.nifty_ce_last_value) > self.config['ce_reset_gap'] and self.ce_reset_flag:
            new_val = current_price - self.config['ce_reset_gap']
            print(f"Resetting CE ref: {self.nifty_ce_last_value} -> {new_val}")
            self.nifty_ce_last_value = new_val

    def _find_option_strike(self, option_type, ltp, gap):
        """
        Find the best matching option symbol at the given gap from spot.
        Adjusts to closer strikes if premium is below minimum.
        """
        if self.instruments_df is None or self.instruments_df.empty:
            return None

        temp_gap = gap
        while temp_gap > 0:
            target_strike = ltp - temp_gap if option_type == "PE" else ltp + temp_gap

            # Filter matching instruments using instrumenttype column
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
                print(f"No {option_type} strike found near {target_strike}")
                return None

            best = df.sort_values('strike_diff').iloc[0]
            symbol = best['symbol']

            # Check if premium meets minimum threshold
            quote = client.quotes(symbol=symbol, exchange=self.config['option_exchange'])
            if quote.get('status') == 'success':
                premium = float(quote.get('data', {}).get('ltp', 0))
                if premium >= self.config['min_price_to_sell']:
                    print(f"Found {option_type} strike: {symbol} (premium: {premium})")
                    return symbol
                else:
                    print(f"{symbol} premium {premium} < min {self.config['min_price_to_sell']}, trying closer")
                    temp_gap -= (self.strike_difference if self.strike_difference else 50)
                    continue
            else:
                print(f"Quote failed for {symbol}: {quote}")
                return None

        return None

    def _is_backing_off(self, side):
        """True while `side` is in its post-rejection cooldown."""
        remaining = self._order_block_until[side] - time.monotonic()
        return remaining > 0

    def _place_sell_order(self, symbol, quantity, side=None):
        """
        Place a SELL MARKET order via OpenAlgo SDK.

        Returns True only if the broker accepted the order. Callers rely on
        this to decide whether to consume the trigger - previously this
        returned None either way, so a rejection was indistinguishable from a
        fill and the reference advanced regardless.
        """
        print(f"Placing SELL order: {symbol} x {quantity} @ MARKET")

        try:
            response = client.placeorder(
                symbol=symbol,
                action="SELL",
                exchange=self.config['option_exchange'],
                quantity=quantity,
                price_type="MARKET",
                product=self.config['product'],
                strategy=self.config['strategy_name'],
            )
        except Exception as e:
            # A transport-level failure is still a failed order, not a fill.
            print(f"Order FAILED (exception): {e}")
            response = {"status": "error", "message": str(e)}

        if isinstance(response, dict) and response.get('status') == 'success':
            print(f"Order placed: {response.get('orderid', 'N/A')} | SELL {symbol} x {quantity}")
            if side:
                self._consecutive_failures[side] = 0
                self._order_block_until[side] = 0.0
            return True

        message = response.get('message', response) if isinstance(response, dict) else response
        print(f"Order FAILED: {message}")

        if side:
            self._consecutive_failures[side] += 1
            cooldown = self.config['order_retry_cooldown_sec']
            self._order_block_until[side] = time.monotonic() + cooldown
            print(f"{side} side backing off {cooldown}s "
                  f"(consecutive failures: {self._consecutive_failures[side]}). "
                  f"Signal NOT consumed - will retry.")
        return False


def main():
    """Main entry point"""
    print("=" * 60)
    print(f"SURVIVOR STRATEGY STARTING")
    print(f"Symbol: {CONFIG['symbol_initials']}")
    print(f"Index: {CONFIG['index_symbol']} ({CONFIG['index_exchange']})")
    print(f"PE Gap: {CONFIG['pe_gap']} | CE Gap: {CONFIG['ce_gap']}")
    print(f"PE Strike Gap: {CONFIG['pe_symbol_gap']} | CE Strike Gap: {CONFIG['ce_symbol_gap']}")
    print(f"PE Qty: {CONFIG['pe_quantity']} | CE Qty: {CONFIG['ce_quantity']}")
    print(f"Min Premium: {CONFIG['min_price_to_sell']}")
    print(f"Polling: every {CONFIG['poll_interval']}s")
    print("=" * 60)

    strategy = SurvivorStrategy(CONFIG)

    if strategy.instruments_df is None or strategy.instruments_df.empty:
        print("FATAL: No instruments loaded. Exiting.")
        return

    print("Strategy initialized. Starting monitoring loop...")

    while True:
        try:
            strategy.ensure_current_expiry()  # no-op unless the check interval has elapsed

            ltp = strategy.get_nifty_ltp()
            if ltp > 0:
                strategy.process_tick(ltp)
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"NIFTY: {ltp} | PE ref: {strategy.nifty_pe_last_value} | "
                    f"CE ref: {strategy.nifty_ce_last_value}"
                )
            else:
                print(f"WARNING: Got zero LTP, skipping tick")

            time.sleep(CONFIG['poll_interval'])

        except KeyboardInterrupt:
            print("Strategy stopped by user")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(5)
            continue

    print("SURVIVOR STRATEGY STOPPED")


if __name__ == "__main__":
    main()
