import unittest
from unittest.mock import patch, Mock
from src.brokers.dhan_broker import DhanBroker

# Mock CSV data that simulates the download from Dhan's server
MOCK_CSV_DATA = """SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,SEM_CUSTOM_SYMBOL
RELIANCE,1234,RELIANCE-EQ
TCS,5678,TCS-EQ
"""

class TestDhanBroker(unittest.TestCase):

    @patch('requests.get')
    @patch('dhanhq.dhanhq')
    def setUp(self, mock_dhanhq, mock_requests_get):
        """Set up the test case with mock objects."""
        # Mock the requests call to download the instrument CSV
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = MOCK_CSV_DATA
        mock_requests_get.return_value = mock_response

        # Mock the dhanhq library instance
        self.mock_dhan_instance = Mock()
        mock_dhanhq.return_value = self.mock_dhan_instance

        # Instantiate the broker, which will trigger the mocked download
        self.broker = DhanBroker(client_id="test_client", access_token="test_token")

    def test_instrument_cache_is_created_on_init(self):
        """
        Verify that the instrument map is correctly created from the mock CSV data.
        """
        self.assertIsNotNone(self.broker._instrument_map)
        self.assertEqual(len(self.broker._instrument_map), 2)
        self.assertEqual(self.broker._instrument_map['RELIANCE'], '1234')
        self.assertEqual(self.broker._instrument_map['TCS'], '5678')
        print("Test passed: DhanBroker correctly creates instrument map on initialization.")

    def test_get_positions_standardizes_data(self):
        """
        Verify that get_positions correctly fetches and standardizes the position data.
        """
        # Mock the response from the dhanhq library's get_positions method.
        # The API returns a list directly on success.
        mock_dhan_positions = [{
            'tradingSymbol': 'TCS',
            'netQty': 10,
            'productType': 'CNC'
        }]
        self.mock_dhan_instance.get_positions.return_value = mock_dhan_positions

        # Call the method and check the standardized output
        positions = self.broker.get_positions()
        self.assertEqual(len(positions), 1)

        expected_position = {
            'tradingsymbol': 'TCS',
            'quantity': 10,
            'product': 'CNC'
        }
        self.assertEqual(positions[0], expected_position)
        print("Test passed: DhanBroker correctly standardizes position data.")

    def test_place_order_uses_correct_security_id(self):
        """
        Verify that place_order correctly looks up the security_id from the
        instrument map and calls the underlying library method.
        """
        # Define the trade signal that our core logic would generate
        order_details = {
            "symbol": "TCS",
            "quantity": 5,
            "transaction_type": "BUY",
            "order_type": "MARKET",
            "product_type": "CNC"
        }

        # Mock the return from the place_order call
        self.mock_dhan_instance.place_order.return_value = {'data': {'orderId': '12345'}}

        # Call the method
        self.broker.place_order(order_details)

        # Verify that the mocked place_order method was called once
        self.mock_dhan_instance.place_order.assert_called_once()

        # Get the arguments it was called with
        call_args, call_kwargs = self.mock_dhan_instance.place_order.call_args

        # Check that the security_id was correctly looked up ('5678' for 'TCS')
        self.assertEqual(call_kwargs.get('security_id'), '5678')
        self.assertEqual(call_kwargs.get('quantity'), 5)
        print("Test passed: DhanBroker correctly uses security_id for placing orders.")

if __name__ == '__main__':
    unittest.main()