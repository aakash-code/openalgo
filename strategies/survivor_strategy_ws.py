#!/usr/bin/env python3
"""
Survivor Options Strategy with WebSocket Streaming for OpenAlgo
================================================================
Enhanced version with real-time WebSocket price updates for faster execution.

This version uses:
- WebSocket streaming for real-time option premium updates
- Immediate response to price movements
- Lower latency compared to polling

Author: Converted to OpenAlgo
License: MIT
"""

from openalgo import api
import argparse
import time
import logging
from datetime import datetime, time as time_module
from typing import Dict, List, Optional
import sys
import os
import threading


class SurvivorStrategyWebSocket:
    """
    Survivor Strategy with WebSocket streaming for real-time monitoring
    """

    def __init__(
        self,
        api_key: str,
        host: str,
        ws_url: str,
        symbol_initials: str,
        pe_gap: int,
        ce_gap: int,
        pe_quantity: int,
        ce_quantity: int,
        min_price_to_sell: float,
        max_loss_per_lot: float = 100.0,
        target_profit_per_lot: float = 50.0
    ):
        self.api_key = api_key
        self.host = host
        self.ws_url = ws_url
        self.symbol_initials = symbol_initials
        self.pe_gap = pe_gap
        self.ce_gap = ce_gap
        self.pe_quantity = pe_quantity
        self.ce_quantity = ce_quantity
        self.min_price_to_sell = min_price_to_sell
        self.max_loss_per_lot = max_loss_per_lot
        self.target_profit_per_lot = target_profit_per_lot

        # Initialize OpenAlgo client
        self.client = api(api_key=api_key, host=host, ws_url=ws_url)

        # Strategy state
        self.pe_symbol: Optional[str] = None
        self.ce_symbol: Optional[str] = None
        self.pe_entry_price: float = 0.0
        self.ce_entry_price: float = 0.0
        self.positions_entered: bool = False
        self.strategy_name = "Survivor Strategy WS"

        # Real-time market data cache (thread-safe)
        self.market_data: Dict[str, Dict] = {}
        self.market_data_lock = threading.Lock()

        # WebSocket connection state
        self.ws_connected = False
        self.ws_subscribed = False

        # Setup logging
        self._setup_logging()

    def _setup_logging(self):
        """Configure logging for the strategy"""
        log_dir = "log/strategies"
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(
            log_dir,
            f"survivor_ws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("Survivor Strategy with WebSocket initialized")

    def is_market_hours(self) -> bool:
        """Check if current time is within market hours"""
        now = datetime.now().time()
        market_open = time_module(9, 15)
        market_close = time_module(15, 30)
        return market_open <= now <= market_close

    def on_quote_update(self, data):
        """
        Callback for WebSocket quote updates

        Args:
            data: Quote data from WebSocket
        """
        try:
            with self.market_data_lock:
                if 'symbol' in data and 'ltp' in data:
                    symbol = data['symbol']
                    self.market_data[symbol] = {
                        'ltp': float(data.get('ltp', 0)),
                        'bid': float(data.get('bid', 0)),
                        'ask': float(data.get('ask', 0)),
                        'volume': int(data.get('volume', 0)),
                        'timestamp': data.get('timestamp', datetime.now().isoformat())
                    }
                    # self.logger.debug(f"Updated: {symbol} LTP={data.get('ltp')}")
        except Exception as e:
            self.logger.error(f"Error in quote update callback: {e}")

    def connect_websocket(self):
        """Connect to OpenAlgo WebSocket"""
        try:
            self.logger.info("Connecting to WebSocket...")
            self.client.connect()
            self.ws_connected = True
            self.logger.info("WebSocket connected successfully")
        except Exception as e:
            self.logger.error(f"Failed to connect to WebSocket: {e}")
            self.ws_connected = False

    def subscribe_instruments(self, instruments: List[Dict]):
        """
        Subscribe to instruments via WebSocket

        Args:
            instruments: List of instrument dicts with 'symbol' and 'exchange'
        """
        try:
            if not self.ws_connected:
                self.connect_websocket()

            if self.ws_connected:
                self.logger.info(f"Subscribing to {len(instruments)} instruments...")
                self.client.subscribe_quote(
                    instruments,
                    on_data_received=self.on_quote_update
                )
                self.ws_subscribed = True
                self.logger.info("Subscription successful")
        except Exception as e:
            self.logger.error(f"Failed to subscribe to instruments: {e}")
            self.ws_subscribed = False

    def unsubscribe_instruments(self, instruments: List[Dict]):
        """Unsubscribe from instruments"""
        try:
            if self.ws_connected and self.ws_subscribed:
                self.client.unsubscribe_quote(instruments)
                self.logger.info("Unsubscribed from instruments")
        except Exception as e:
            self.logger.error(f"Error unsubscribing: {e}")

    def disconnect_websocket(self):
        """Disconnect from WebSocket"""
        try:
            if self.ws_connected:
                self.client.disconnect()
                self.ws_connected = False
                self.ws_subscribed = False
                self.logger.info("WebSocket disconnected")
        except Exception as e:
            self.logger.error(f"Error disconnecting WebSocket: {e}")

    def get_ltp_from_cache(self, symbol: str) -> Optional[float]:
        """
        Get LTP from WebSocket cache

        Args:
            symbol: Symbol to get LTP for

        Returns:
            LTP or None if not available
        """
        with self.market_data_lock:
            if symbol in self.market_data:
                return self.market_data[symbol]['ltp']
        return None

    def get_underlying_ltp(self, symbol: str, exchange: str) -> Optional[float]:
        """Get Last Traded Price of underlying using REST API"""
        try:
            response = self.client.quotes(symbol=symbol, exchange=exchange)
            if response.get('status') == 'success':
                ltp = response['data']['ltp']
                self.logger.info(f"{symbol} LTP: {ltp}")
                return ltp
            else:
                self.logger.error(f"Failed to get LTP for {symbol}: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Error getting LTP for {symbol}: {e}")
            return None

    def calculate_strikes(self, underlying_ltp: float, strike_gap: int = 50) -> tuple:
        """Calculate PE and CE strike prices"""
        atm_strike = round(underlying_ltp / strike_gap) * strike_gap
        pe_strike = atm_strike - self.pe_gap
        ce_strike = atm_strike + self.ce_gap

        self.logger.info(f"Underlying LTP: {underlying_ltp}, ATM: {atm_strike}")
        self.logger.info(f"PE Strike: {pe_strike}, CE Strike: {ce_strike}")

        return pe_strike, ce_strike

    def construct_option_symbol(self, strike: float, option_type: str) -> str:
        """Construct option symbol"""
        return f"{self.symbol_initials}{int(strike)}{option_type}"

    def get_option_premium(self, symbol: str, exchange: str = "NFO") -> Optional[float]:
        """Get option premium using REST API"""
        try:
            response = self.client.quotes(symbol=symbol, exchange=exchange)
            if response.get('status') == 'success':
                ltp = response['data']['ltp']
                self.logger.info(f"{symbol} Premium: {ltp}")
                return ltp
            else:
                self.logger.warning(f"Failed to get premium for {symbol}: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Error getting premium for {symbol}: {e}")
            return None

    def sell_option(self, symbol: str, quantity: int, exchange: str = "NFO",
                    product: str = "MIS") -> bool:
        """Sell an option"""
        try:
            response = self.client.placeorder(
                strategy=self.strategy_name,
                symbol=symbol,
                action="SELL",
                exchange=exchange,
                price_type="MARKET",
                product=product,
                quantity=quantity
            )

            if response.get('status') == 'success':
                order_id = response.get('orderid')
                self.logger.info(f"Sell order: {symbol} x {quantity}, OrderID: {order_id}")
                return True
            else:
                self.logger.error(f"Failed sell order for {symbol}: {response}")
                return False
        except Exception as e:
            self.logger.error(f"Error placing sell order for {symbol}: {e}")
            return False

    def buy_option(self, symbol: str, quantity: int, exchange: str = "NFO",
                   product: str = "MIS") -> bool:
        """Buy an option to close position"""
        try:
            response = self.client.placeorder(
                strategy=self.strategy_name,
                symbol=symbol,
                action="BUY",
                exchange=exchange,
                price_type="MARKET",
                product=product,
                quantity=quantity
            )

            if response.get('status') == 'success':
                order_id = response.get('orderid')
                self.logger.info(f"Buy order: {symbol} x {quantity}, OrderID: {order_id}")
                return True
            else:
                self.logger.error(f"Failed buy order for {symbol}: {response}")
                return False
        except Exception as e:
            self.logger.error(f"Error placing buy order for {symbol}: {e}")
            return False

    def get_positions(self) -> Dict:
        """Get current positions"""
        try:
            response = self.client.positionbook()
            if response.get('status') == 'success':
                positions = {}
                for pos in response.get('data', []):
                    symbol = pos['symbol']
                    positions[symbol] = {
                        'quantity': int(pos['quantity']),
                        'average_price': float(pos['average_price']),
                        'ltp': float(pos['ltp']),
                        'pnl': float(pos['pnl'])
                    }
                return positions
            else:
                self.logger.error(f"Failed to get positions: {response}")
                return {}
        except Exception as e:
            self.logger.error(f"Error getting positions: {e}")
            return {}

    def enter_positions(self):
        """Enter initial positions"""
        self.logger.info("=" * 60)
        self.logger.info("ENTERING POSITIONS")
        self.logger.info("=" * 60)

        # Determine underlying
        if "NIFTY" in self.symbol_initials and "BANK" not in self.symbol_initials:
            underlying = "NIFTY"
            underlying_exchange = "NSE_INDEX"
            strike_gap = 50
        elif "BANKNIFTY" in self.symbol_initials or "BANK" in self.symbol_initials:
            underlying = "BANKNIFTY"
            underlying_exchange = "NSE_INDEX"
            strike_gap = 100
        else:
            self.logger.error(f"Unknown underlying in symbol: {self.symbol_initials}")
            return

        # Get underlying LTP
        underlying_ltp = self.get_underlying_ltp(underlying, underlying_exchange)
        if not underlying_ltp:
            self.logger.error("Failed to get underlying LTP")
            return

        # Calculate strikes
        pe_strike, ce_strike = self.calculate_strikes(underlying_ltp, strike_gap)

        # Construct option symbols
        self.pe_symbol = self.construct_option_symbol(pe_strike, "PE")
        self.ce_symbol = self.construct_option_symbol(ce_strike, "CE")

        self.logger.info(f"PE Symbol: {self.pe_symbol}")
        self.logger.info(f"CE Symbol: {self.ce_symbol}")

        # Get option premiums
        pe_premium = self.get_option_premium(self.pe_symbol)
        ce_premium = self.get_option_premium(self.ce_symbol)

        if not pe_premium or not ce_premium:
            self.logger.error("Failed to get option premiums")
            return

        # Check minimum price threshold
        if pe_premium < self.min_price_to_sell or ce_premium < self.min_price_to_sell:
            self.logger.warning(
                f"Premiums below threshold: PE={pe_premium}, CE={ce_premium}, "
                f"Min={self.min_price_to_sell}"
            )
            return

        # Sell options
        pe_success = self.sell_option(self.pe_symbol, self.pe_quantity)
        if not pe_success:
            return

        time.sleep(1)

        ce_success = self.sell_option(self.ce_symbol, self.ce_quantity)
        if not ce_success:
            self.buy_option(self.pe_symbol, self.pe_quantity)
            return

        # Store entry prices
        self.pe_entry_price = pe_premium
        self.ce_entry_price = ce_premium
        self.positions_entered = True

        # Subscribe to options via WebSocket for real-time monitoring
        instruments = [
            {"exchange": "NFO", "symbol": self.pe_symbol},
            {"exchange": "NFO", "symbol": self.ce_symbol}
        ]
        self.subscribe_instruments(instruments)

        self.logger.info("=" * 60)
        self.logger.info("POSITIONS ENTERED SUCCESSFULLY")
        self.logger.info(f"PE: {self.pe_symbol} @ {self.pe_entry_price} x {self.pe_quantity}")
        self.logger.info(f"CE: {self.ce_symbol} @ {self.ce_entry_price} x {self.ce_quantity}")
        self.logger.info("=" * 60)

    def monitor_positions(self):
        """Monitor positions using WebSocket data"""
        # Get current prices from WebSocket cache
        pe_current_price = self.get_ltp_from_cache(self.pe_symbol)
        ce_current_price = self.get_ltp_from_cache(self.ce_symbol)

        # Fallback to REST API if WebSocket data not available
        if pe_current_price is None:
            pe_current_price = self.get_option_premium(self.pe_symbol)
        if ce_current_price is None:
            ce_current_price = self.get_option_premium(self.ce_symbol)

        if pe_current_price is None or ce_current_price is None:
            self.logger.warning("Unable to get current prices")
            return

        # Calculate P&L
        pe_pnl = (self.pe_entry_price - pe_current_price) * self.pe_quantity
        ce_pnl = (self.ce_entry_price - ce_current_price) * self.ce_quantity
        total_pnl = pe_pnl + ce_pnl

        # Log status
        self.logger.info("-" * 60)
        self.logger.info("POSITION MONITORING (WebSocket)")
        self.logger.info(f"PE: {self.pe_symbol}")
        self.logger.info(f"  Entry: {self.pe_entry_price}, Current: {pe_current_price}, P&L: {pe_pnl:.2f}")
        self.logger.info(f"CE: {self.ce_symbol}")
        self.logger.info(f"  Entry: {self.ce_entry_price}, Current: {ce_current_price}, P&L: {ce_pnl:.2f}")
        self.logger.info(f"Total P&L: {total_pnl:.2f}")
        self.logger.info("-" * 60)

        # Check exit conditions
        should_exit = False
        exit_reason = ""

        if total_pnl < -self.max_loss_per_lot:
            should_exit = True
            exit_reason = f"STOP LOSS: Total P&L {total_pnl:.2f} < -{self.max_loss_per_lot}"

        elif total_pnl > self.target_profit_per_lot:
            should_exit = True
            exit_reason = f"TARGET PROFIT: Total P&L {total_pnl:.2f} > {self.target_profit_per_lot}"

        pe_loss_threshold = self.pe_entry_price * 2
        ce_loss_threshold = self.ce_entry_price * 2

        if pe_current_price > pe_loss_threshold:
            should_exit = True
            exit_reason = f"PE STOP LOSS: {pe_current_price} > {pe_loss_threshold}"

        if ce_current_price > ce_loss_threshold:
            should_exit = True
            exit_reason = f"CE STOP LOSS: {ce_current_price} > {ce_loss_threshold}"

        if should_exit:
            self.logger.warning("=" * 60)
            self.logger.warning(f"EXIT SIGNAL: {exit_reason}")
            self.logger.warning("=" * 60)
            self.exit_positions()

    def exit_positions(self):
        """Exit all positions"""
        self.logger.info("=" * 60)
        self.logger.info("EXITING ALL POSITIONS")
        self.logger.info("=" * 60)

        # Unsubscribe from WebSocket
        if self.pe_symbol and self.ce_symbol:
            instruments = [
                {"exchange": "NFO", "symbol": self.pe_symbol},
                {"exchange": "NFO", "symbol": self.ce_symbol}
            ]
            self.unsubscribe_instruments(instruments)

        try:
            response = self.client.closeposition(strategy=self.strategy_name)

            if response.get('status') == 'success':
                self.logger.info("All positions closed successfully")
            else:
                self.logger.error(f"Failed to close positions: {response}")
                # Fallback
                if self.pe_symbol:
                    self.buy_option(self.pe_symbol, self.pe_quantity)
                if self.ce_symbol:
                    self.buy_option(self.ce_symbol, self.ce_quantity)

        except Exception as e:
            self.logger.error(f"Error closing positions: {e}")

        self.positions_entered = False
        self.logger.info("Strategy execution completed")

    def run(self):
        """Main strategy execution loop"""
        self.logger.info("=" * 60)
        self.logger.info("SURVIVOR STRATEGY WITH WEBSOCKET STARTED")
        self.logger.info("=" * 60)
        self.logger.info(f"Symbol: {self.symbol_initials}")
        self.logger.info(f"PE Gap: {self.pe_gap}, CE Gap: {self.ce_gap}")
        self.logger.info(f"Quantities: PE={self.pe_quantity}, CE={self.ce_quantity}")
        self.logger.info("=" * 60)

        try:
            while True:
                if not self.is_market_hours():
                    if self.positions_entered:
                        self.logger.warning("Market closed, exiting positions")
                        self.exit_positions()

                    self.logger.info("Market closed. Waiting...")
                    time.sleep(60)
                    continue

                if not self.positions_entered:
                    self.enter_positions()
                    if self.positions_entered:
                        time.sleep(5)
                    else:
                        time.sleep(30)
                    continue

                # Monitor positions using WebSocket data
                self.monitor_positions()
                time.sleep(5)  # Faster checks with WebSocket

        except KeyboardInterrupt:
            self.logger.info("Strategy interrupted")
            if self.positions_entered:
                self.exit_positions()

        except Exception as e:
            self.logger.error(f"Unexpected error: {e}", exc_info=True)
            if self.positions_entered:
                self.exit_positions()

        finally:
            self.disconnect_websocket()
            self.logger.info("Strategy stopped")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Survivor Strategy with WebSocket")

    parser.add_argument('--api-key', type=str, default=os.getenv('OPENALGO_API_KEY'))
    parser.add_argument('--symbol-initials', type=str, required=True)
    parser.add_argument('--pe-gap', type=int, default=25)
    parser.add_argument('--ce-gap', type=int, default=25)
    parser.add_argument('--pe-quantity', type=int, default=50)
    parser.add_argument('--ce-quantity', type=int, default=50)
    parser.add_argument('--min-price-to-sell', type=float, default=15.0)
    parser.add_argument('--host', type=str, default=os.getenv('OPENALGO_HOST', 'http://127.0.0.1:5000'))
    parser.add_argument('--ws-url', type=str, default=os.getenv('OPENALGO_WS_URL', 'ws://127.0.0.1:8765'))
    parser.add_argument('--max-loss', type=float, default=100.0)
    parser.add_argument('--target-profit', type=float, default=50.0)

    args = parser.parse_args()

    if not args.api_key:
        print("Error: API key required")
        sys.exit(1)

    strategy = SurvivorStrategyWebSocket(
        api_key=args.api_key,
        host=args.host,
        ws_url=args.ws_url,
        symbol_initials=args.symbol_initials,
        pe_gap=args.pe_gap,
        ce_gap=args.ce_gap,
        pe_quantity=args.pe_quantity,
        ce_quantity=args.ce_quantity,
        min_price_to_sell=args.min_price_to_sell,
        max_loss_per_lot=args.max_loss,
        target_profit_per_lot=args.target_profit
    )

    strategy.run()


if __name__ == "__main__":
    main()
