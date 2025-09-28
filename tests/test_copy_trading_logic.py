import unittest
from unittest.mock import Mock, patch
from src.core.master_account import MasterAccount
from src.core.child_account import ChildAccount
from src.core.trade_signal import TradeSignal

class TestCopyTradingLogic(unittest.TestCase):

    def setUp(self):
        """Set up mock broker objects for master and child accounts."""
        # Mock for the Master Broker
        self.mock_master_broker = Mock()
        # Mock for the Child Broker
        self.mock_child_broker = Mock()

        # Set a default return value for get_positions before instantiating MasterAccount
        self.mock_master_broker.get_positions.return_value = []

        # Instantiate the core logic classes with the mock brokers
        self.master_account = MasterAccount(broker=self.mock_master_broker)
        self.child_account = ChildAccount(broker=self.mock_child_broker, account_id="CHILD_1")

    def test_new_position_creates_buy_signal_and_executes_trade(self):
        """
        Verify that a new position in the master account triggers a trade on the child account.
        """
        # --- Stage 1: Master account starts with no positions (already set in setUp) ---

        # Initial check should yield no signals
        initial_signals = self.master_account.check_for_new_trades()
        self.assertEqual(len(initial_signals), 0, "Should be no trades initially.")

        # --- Stage 2: A new position appears in the master account ---
        new_position = {
            'tradingsymbol': 'RELIANCE',
            'quantity': 10,
            'product': 'CNC'
        }
        self.mock_master_broker.get_positions.return_value = [new_position]

        # --- Stage 3: Check for new trades and verify the signal ---
        new_signals = self.master_account.check_for_new_trades()

        self.assertEqual(len(new_signals), 1, "Should detect one new trade.")

        expected_signal = TradeSignal(
            symbol='RELIANCE',
            quantity=10,
            transaction_type='BUY',
            order_type='MARKET',
            product_type='CNC'
        )
        self.assertEqual(new_signals[0], expected_signal, "The generated signal is incorrect.")

        # --- Stage 4: Execute the trade on the child account ---
        self.child_account.execute_trades(new_signals)

        # --- Stage 5: Verify that the child broker's place_order was called correctly ---
        self.mock_child_broker.place_order.assert_called_once()

        expected_order_details = {
            "symbol": "RELIANCE",
            "quantity": 10,
            "transaction_type": "BUY",
            "order_type": "MARKET",
            "product_type": "CNC",
        }
        self.mock_child_broker.place_order.assert_called_with(expected_order_details)
        print("Test passed: New position correctly triggered a trade on the child account.")

    def test_increased_position_triggers_buy_trade(self):
        """
        Verify that increasing the quantity of an existing position triggers a new BUY trade.
        """
        # --- Stage 1: Master account starts with an existing position ---
        initial_position = [{'tradingsymbol': 'TCS', 'quantity': 5, 'product': 'MIS'}]
        self.mock_master_broker.get_positions.return_value = initial_position
        self.master_account.check_for_new_trades() # This sets the initial state

        # --- Stage 2: The position quantity increases ---
        updated_position = [{'tradingsymbol': 'TCS', 'quantity': 15, 'product': 'MIS'}]
        self.mock_master_broker.get_positions.return_value = updated_position

        # --- Stage 3: Check for trades and verify the signal ---
        new_signals = self.master_account.check_for_new_trades()
        self.assertEqual(len(new_signals), 1)

        expected_signal = TradeSignal('TCS', 10, 'BUY', 'MARKET', 'MIS')
        self.assertEqual(new_signals[0], expected_signal)

        # --- Stage 4: Execute and verify on child account ---
        self.child_account.execute_trades(new_signals)
        self.mock_child_broker.place_order.assert_called_once_with({
            "symbol": "TCS", "quantity": 10, "transaction_type": "BUY",
            "order_type": "MARKET", "product_type": "MIS"
        })
        print("Test passed: Increased position correctly triggered a new BUY trade.")

if __name__ == '__main__':
    unittest.main()