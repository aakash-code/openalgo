#!/usr/bin/env python3
"""
Survivor Options Strategy for OpenAlgo
=====================================
A short straddle/strangle strategy that sells both PE and CE options at specific strikes.
Monitors positions in real-time and exits when prices hit certain thresholds.

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


class SurvivorStrategy:
    """
    Survivor Options Strategy Implementation

    This strategy:
    1. Sells PE and CE options at strikes based on ATM +/- gap
    2. Monitors positions continuously during market hours
    3. Exits positions when profit targets or stop losses are hit
    4. Supports configurable quantities and price thresholds
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
        """
        Initialize the Survivor Strategy

        Args:
            api_key: OpenAlgo API key
            host: OpenAlgo host URL (e.g., http://127.0.0.1:5000)
            ws_url: OpenAlgo WebSocket URL (e.g., ws://127.0.0.1:8765)
            symbol_initials: Option symbol prefix (e.g., NIFTY25JAN30)
            pe_gap: Strike gap for PE option from ATM (e.g., 25)
            ce_gap: Strike gap for CE option from ATM (e.g., 25)
            pe_quantity: Quantity of PE options to trade
            ce_quantity: Quantity of CE options to trade
            min_price_to_sell: Minimum premium to collect when selling
            max_loss_per_lot: Maximum loss per lot before exit (default 100)
            target_profit_per_lot: Target profit per lot (default 50)
        """
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
        self.strategy_name = "Survivor Strategy"

        # Market data cache
        self.market_data: Dict[str, Dict] = {}

        # Setup logging
        self._setup_logging()

    def _setup_logging(self):
        """Configure logging for the strategy"""
        log_dir = "log/strategies"
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(
            log_dir,
            f"survivor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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
        self.logger.info("Survivor Strategy initialized")

    def is_market_hours(self) -> bool:
        """Check if current time is within market hours (9:15 AM - 3:30 PM IST)"""
        now = datetime.now().time()
        market_open = time_module(9, 15)
        market_close = time_module(15, 30)
        return market_open <= now <= market_close

    def get_underlying_ltp(self, symbol: str, exchange: str) -> Optional[float]:
        """
        Get Last Traded Price of underlying instrument

        Args:
            symbol: Symbol name (e.g., NIFTY, BANKNIFTY)
            exchange: Exchange (e.g., NSE_INDEX, NFO)

        Returns:
            Last traded price or None if error
        """
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
        """
        Calculate PE and CE strike prices based on ATM and gaps

        Args:
            underlying_ltp: Current LTP of underlying
            strike_gap: Strike interval (default 50 for NIFTY)

        Returns:
            Tuple of (pe_strike, ce_strike)
        """
        # Round to nearest strike
        atm_strike = round(underlying_ltp / strike_gap) * strike_gap

        pe_strike = atm_strike - self.pe_gap
        ce_strike = atm_strike + self.ce_gap

        self.logger.info(f"Underlying LTP: {underlying_ltp}, ATM: {atm_strike}")
        self.logger.info(f"PE Strike: {pe_strike}, CE Strike: {ce_strike}")

        return pe_strike, ce_strike

    def construct_option_symbol(self, strike: float, option_type: str) -> str:
        """
        Construct option symbol from components

        Args:
            strike: Strike price
            option_type: 'PE' or 'CE'

        Returns:
            Option symbol string (e.g., NIFTY25JAN3024000PE)
        """
        symbol = f"{self.symbol_initials}{int(strike)}{option_type}"
        return symbol

    def get_option_premium(self, symbol: str, exchange: str = "NFO") -> Optional[float]:
        """
        Get current premium (LTP) of an option

        Args:
            symbol: Option symbol
            exchange: Exchange (default NFO)

        Returns:
            Option premium or None if error
        """
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

    def sell_option(
        self,
        symbol: str,
        quantity: int,
        exchange: str = "NFO",
        product: str = "MIS"
    ) -> bool:
        """
        Sell an option using OpenAlgo API

        Args:
            symbol: Option symbol to sell
            quantity: Quantity to sell
            exchange: Exchange (default NFO)
            product: Product type (default MIS)

        Returns:
            True if order placed successfully, False otherwise
        """
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
                self.logger.info(f"Sell order placed: {symbol} x {quantity}, OrderID: {order_id}")
                return True
            else:
                self.logger.error(f"Failed to place sell order for {symbol}: {response}")
                return False

        except Exception as e:
            self.logger.error(f"Error placing sell order for {symbol}: {e}")
            return False

    def buy_option(
        self,
        symbol: str,
        quantity: int,
        exchange: str = "NFO",
        product: str = "MIS"
    ) -> bool:
        """
        Buy an option to close position (square off)

        Args:
            symbol: Option symbol to buy
            quantity: Quantity to buy
            exchange: Exchange (default NFO)
            product: Product type (default MIS)

        Returns:
            True if order placed successfully, False otherwise
        """
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
                self.logger.info(f"Buy order placed: {symbol} x {quantity}, OrderID: {order_id}")
                return True
            else:
                self.logger.error(f"Failed to place buy order for {symbol}: {response}")
                return False

        except Exception as e:
            self.logger.error(f"Error placing buy order for {symbol}: {e}")
            return False

    def get_positions(self) -> Dict:
        """
        Get current positions from broker

        Returns:
            Dictionary of positions by symbol
        """
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
        """
        Enter initial positions by selling PE and CE options
        """
        self.logger.info("=" * 60)
        self.logger.info("ENTERING POSITIONS")
        self.logger.info("=" * 60)

        # Get underlying instrument name from symbol_initials
        # Extract base symbol (e.g., NIFTY from NIFTY25JAN30)
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
            self.logger.error("Failed to get underlying LTP, cannot enter positions")
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
            self.logger.error("Failed to get option premiums, cannot enter positions")
            return

        # Check minimum price threshold
        if pe_premium < self.min_price_to_sell or ce_premium < self.min_price_to_sell:
            self.logger.warning(
                f"Option premiums below minimum threshold: "
                f"PE={pe_premium}, CE={ce_premium}, Min={self.min_price_to_sell}"
            )
            self.logger.warning("Waiting for better premiums...")
            return

        # Sell PE option
        self.logger.info(f"Selling {self.pe_symbol} @ {pe_premium} x {self.pe_quantity}")
        pe_success = self.sell_option(self.pe_symbol, self.pe_quantity)

        if not pe_success:
            self.logger.error("Failed to sell PE option")
            return

        time.sleep(1)  # Small delay between orders

        # Sell CE option
        self.logger.info(f"Selling {self.ce_symbol} @ {ce_premium} x {self.ce_quantity}")
        ce_success = self.sell_option(self.ce_symbol, self.ce_quantity)

        if not ce_success:
            self.logger.error("Failed to sell CE option, attempting to exit PE position")
            # Try to exit PE position
            self.buy_option(self.pe_symbol, self.pe_quantity)
            return

        # Store entry prices
        self.pe_entry_price = pe_premium
        self.ce_entry_price = ce_premium
        self.positions_entered = True

        self.logger.info("=" * 60)
        self.logger.info("POSITIONS ENTERED SUCCESSFULLY")
        self.logger.info(f"PE: {self.pe_symbol} @ {self.pe_entry_price} x {self.pe_quantity}")
        self.logger.info(f"CE: {self.ce_symbol} @ {self.ce_entry_price} x {self.ce_quantity}")
        self.logger.info(f"Total Premium Collected: {(self.pe_entry_price + self.ce_entry_price) * min(self.pe_quantity, self.ce_quantity)}")
        self.logger.info("=" * 60)

    def monitor_positions(self):
        """
        Monitor positions and manage exits based on P&L
        """
        positions = self.get_positions()

        if not positions:
            self.logger.warning("No positions found")
            return

        # Get current data for PE and CE
        pe_position = positions.get(self.pe_symbol)
        ce_position = positions.get(self.ce_symbol)

        if not pe_position or not ce_position:
            self.logger.warning("PE or CE position not found in position book")
            return

        # Calculate P&L
        pe_pnl = pe_position['pnl']
        ce_pnl = ce_position['pnl']
        total_pnl = pe_pnl + ce_pnl

        pe_current_price = pe_position['ltp']
        ce_current_price = ce_position['ltp']

        # Log current status
        self.logger.info("-" * 60)
        self.logger.info("POSITION MONITORING")
        self.logger.info(f"PE: {self.pe_symbol}")
        self.logger.info(f"  Entry: {self.pe_entry_price}, Current: {pe_current_price}, P&L: {pe_pnl}")
        self.logger.info(f"CE: {self.ce_symbol}")
        self.logger.info(f"  Entry: {self.ce_entry_price}, Current: {ce_current_price}, P&L: {ce_pnl}")
        self.logger.info(f"Total P&L: {total_pnl}")
        self.logger.info("-" * 60)

        # Check exit conditions
        should_exit = False
        exit_reason = ""

        # Stop loss check
        if total_pnl < -self.max_loss_per_lot:
            should_exit = True
            exit_reason = f"STOP LOSS HIT: Total P&L {total_pnl} < -{self.max_loss_per_lot}"

        # Target profit check
        elif total_pnl > self.target_profit_per_lot:
            should_exit = True
            exit_reason = f"TARGET PROFIT HIT: Total P&L {total_pnl} > {self.target_profit_per_lot}"

        # Individual option stop loss (option premium doubled from entry)
        pe_loss_threshold = self.pe_entry_price * 2
        ce_loss_threshold = self.ce_entry_price * 2

        if pe_current_price > pe_loss_threshold:
            should_exit = True
            exit_reason = f"PE STOP LOSS: Current {pe_current_price} > {pe_loss_threshold}"

        if ce_current_price > ce_loss_threshold:
            should_exit = True
            exit_reason = f"CE STOP LOSS: Current {ce_current_price} > {ce_loss_threshold}"

        if should_exit:
            self.logger.warning("=" * 60)
            self.logger.warning(f"EXIT SIGNAL: {exit_reason}")
            self.logger.warning("=" * 60)
            self.exit_positions()

    def exit_positions(self):
        """
        Exit all positions (square off)
        """
        self.logger.info("=" * 60)
        self.logger.info("EXITING ALL POSITIONS")
        self.logger.info("=" * 60)

        try:
            # Use closeposition API to square off all positions
            response = self.client.closeposition(strategy=self.strategy_name)

            if response.get('status') == 'success':
                self.logger.info("All positions closed successfully")
                self.logger.info(response.get('message', ''))
            else:
                self.logger.error(f"Failed to close positions via API: {response}")
                # Fallback: try to close individually
                self.logger.info("Attempting individual position closure...")
                if self.pe_symbol:
                    self.buy_option(self.pe_symbol, self.pe_quantity)
                if self.ce_symbol:
                    self.buy_option(self.ce_symbol, self.ce_quantity)

        except Exception as e:
            self.logger.error(f"Error closing positions: {e}")

        self.positions_entered = False
        self.logger.info("Strategy execution completed")

    def run(self):
        """
        Main strategy execution loop
        """
        self.logger.info("=" * 60)
        self.logger.info("SURVIVOR STRATEGY STARTED")
        self.logger.info("=" * 60)
        self.logger.info(f"Symbol Initials: {self.symbol_initials}")
        self.logger.info(f"PE Gap: {self.pe_gap}, CE Gap: {self.ce_gap}")
        self.logger.info(f"PE Quantity: {self.pe_quantity}, CE Quantity: {self.ce_quantity}")
        self.logger.info(f"Min Price to Sell: {self.min_price_to_sell}")
        self.logger.info(f"Max Loss per Lot: {self.max_loss_per_lot}")
        self.logger.info(f"Target Profit per Lot: {self.target_profit_per_lot}")
        self.logger.info("=" * 60)

        try:
            while True:
                # Check if market is open
                if not self.is_market_hours():
                    if self.positions_entered:
                        self.logger.warning("Market closed, exiting positions")
                        self.exit_positions()

                    now = datetime.now()
                    self.logger.info(
                        f"Market is closed. Current time: {now.strftime('%H:%M:%S')}. "
                        f"Waiting for market hours (9:15 AM - 3:30 PM)..."
                    )
                    time.sleep(60)  # Check every minute
                    continue

                # Enter positions if not already entered
                if not self.positions_entered:
                    self.enter_positions()
                    if self.positions_entered:
                        time.sleep(10)  # Wait a bit after entry
                    else:
                        time.sleep(30)  # Wait before retry
                    continue

                # Monitor positions
                self.monitor_positions()

                # Wait before next check
                time.sleep(15)  # Check every 15 seconds

        except KeyboardInterrupt:
            self.logger.info("Strategy interrupted by user")
            if self.positions_entered:
                self.logger.info("Closing positions before exit...")
                self.exit_positions()

        except Exception as e:
            self.logger.error(f"Unexpected error in strategy: {e}", exc_info=True)
            if self.positions_entered:
                self.logger.info("Attempting to close positions due to error...")
                self.exit_positions()

        finally:
            self.logger.info("Survivor Strategy stopped")


def main():
    """Main entry point with command-line argument parsing"""
    parser = argparse.ArgumentParser(
        description="Survivor Options Strategy for OpenAlgo",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Required arguments
    parser.add_argument(
        '--api-key',
        type=str,
        default=os.getenv('OPENALGO_API_KEY'),
        help='OpenAlgo API key (or set OPENALGO_API_KEY env var)'
    )

    parser.add_argument(
        '--symbol-initials',
        type=str,
        required=True,
        help='Option symbol prefix (e.g., NIFTY25JAN30, BANKNIFTY25FEB28)'
    )

    parser.add_argument(
        '--pe-gap',
        type=int,
        default=25,
        help='Strike gap for PE option from ATM'
    )

    parser.add_argument(
        '--ce-gap',
        type=int,
        default=25,
        help='Strike gap for CE option from ATM'
    )

    parser.add_argument(
        '--pe-quantity',
        type=int,
        default=50,
        help='Quantity of PE options to trade'
    )

    parser.add_argument(
        '--ce-quantity',
        type=int,
        default=50,
        help='Quantity of CE options to trade'
    )

    parser.add_argument(
        '--min-price-to-sell',
        type=float,
        default=15.0,
        help='Minimum premium required to enter positions'
    )

    # Optional arguments
    parser.add_argument(
        '--host',
        type=str,
        default=os.getenv('OPENALGO_HOST', 'http://127.0.0.1:5000'),
        help='OpenAlgo host URL'
    )

    parser.add_argument(
        '--ws-url',
        type=str,
        default=os.getenv('OPENALGO_WS_URL', 'ws://127.0.0.1:8765'),
        help='OpenAlgo WebSocket URL'
    )

    parser.add_argument(
        '--max-loss',
        type=float,
        default=100.0,
        help='Maximum loss per lot before exit'
    )

    parser.add_argument(
        '--target-profit',
        type=float,
        default=50.0,
        help='Target profit per lot'
    )

    args = parser.parse_args()

    # Validate API key
    if not args.api_key:
        print("Error: API key is required. Use --api-key or set OPENALGO_API_KEY environment variable")
        sys.exit(1)

    # Create and run strategy
    strategy = SurvivorStrategy(
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
