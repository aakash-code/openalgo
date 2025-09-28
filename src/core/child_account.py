import logging
from typing import List
from ..core.broker_interface import BrokerInterface
from ..core.trade_signal import TradeSignal

class ChildAccount:
    """
    Manages a child account and executes trades based on signals.
    """

    def __init__(self, broker: BrokerInterface, account_id: str):
        """
        Initializes the ChildAccount with a broker instance.

        Args:
            broker (BrokerInterface): An object that adheres to the BrokerInterface.
            account_id (str): A unique identifier for this child account.
        """
        self.broker = broker
        self.account_id = account_id
        logging.info(f"ChildAccount {self.account_id} initialized.")

    def execute_trades(self, signals: List[TradeSignal]) -> None:
        """
        Executes a list of trade signals on this child account.

        Args:
            signals (List[TradeSignal]): A list of trade signals to execute.
        """
        if not signals:
            return

        logging.info(f"Account {self.account_id}: Received {len(signals)} trade(s) to execute.")
        for signal in signals:
            try:
                logging.info(f"Account {self.account_id}: Executing {signal.transaction_type} "
                             f"order for {signal.quantity} of {signal.symbol}.")

                order_details = {
                    "symbol": signal.symbol,
                    "quantity": signal.quantity,
                    "transaction_type": signal.transaction_type,
                    "order_type": signal.order_type,
                    "product_type": signal.product_type,
                }

                # Execute the trade via the broker interface
                self.broker.place_order(order_details)

            except Exception as e:
                logging.error(f"Account {self.account_id}: Failed to execute trade "
                              f"for {signal.symbol}. Error: {e}")
                # Continue to the next trade even if one fails
                continue