import logging
from typing import List
from ..core.broker_interface import BrokerInterface
from ..core.trade_signal import TradeSignal

class MasterAccount:
    """
    Monitors a master account for new trades and generates trade signals.
    """

    def __init__(self, broker: BrokerInterface):
        """
        Initializes the MasterAccount with a broker instance.

        Args:
            broker (BrokerInterface): An object that adheres to the BrokerInterface.
        """
        self.broker = broker
        self._last_positions = self._fetch_current_positions()
        logging.info(f"MasterAccount initialized with {len(self._last_positions)} existing positions.")

    def _fetch_current_positions(self) -> dict:
        """
        Fetches the current positions from the broker and returns them as a dictionary
        keyed by a unique identifier (e.g., symbol).
        """
        try:
            positions = self.broker.get_positions()
            # Using 'tradingsymbol' as the key for uniqueness
            return {pos['tradingsymbol']: pos for pos in positions}
        except Exception as e:
            logging.error(f"Failed to fetch master account positions: {e}")
            return {}

    def check_for_new_trades(self) -> List[TradeSignal]:
        """
        Checks for new trades by comparing current positions against the last known state.

        Returns:
            List[TradeSignal]: A list of new trade signals to be executed.
        """
        current_positions = self._fetch_current_positions()
        new_signals = []

        # Simple diff logic: check for new symbols or changed quantities
        for symbol, position in current_positions.items():
            last_pos = self._last_positions.get(symbol)

            if not last_pos:
                # New position found
                logging.info(f"New position detected for {symbol}: Quantity {position['quantity']}")
                signal = self._create_signal_from_position(position)
                if signal:
                    new_signals.append(signal)
            elif position['quantity'] != last_pos['quantity']:
                # Position quantity has changed
                logging.info(f"Position change detected for {symbol}: "
                             f"Old Qty: {last_pos['quantity']}, New Qty: {position['quantity']}")

                # Create a signal for the difference in quantity
                quantity_diff = position['quantity'] - last_pos['quantity']
                trade_type = 'BUY' if quantity_diff > 0 else 'SELL'

                signal = TradeSignal(
                    symbol=position['tradingsymbol'],
                    quantity=abs(quantity_diff),
                    transaction_type=trade_type,
                    order_type='MARKET',  # Assuming market order for simplicity
                    product_type=position['product']
                )
                new_signals.append(signal)

        # Update the state for the next check
        self._last_positions = current_positions

        return new_signals

    def _create_signal_from_position(self, position: dict) -> TradeSignal:
        """
        Creates a TradeSignal from a position dictionary.
        """
        quantity = position.get('quantity')
        if quantity == 0:
            return None

        return TradeSignal(
            symbol=position['tradingsymbol'],
            quantity=abs(quantity),
            transaction_type='BUY' if quantity > 0 else 'SELL',
            order_type='MARKET',  # Defaulting to MARKET for simplicity
            product_type=position['product']
        )