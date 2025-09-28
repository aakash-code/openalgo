import logging
from typing import Any, Dict, List
import dhanhq
from ..core.broker_interface import BrokerInterface

logging.basicConfig(level=logging.INFO)

class DhanBroker(BrokerInterface):
    """
    Implementation of the BrokerInterface for DhanHQ.
    """

    def __init__(self, client_id: str, access_token: str):
        """
        Initializes the DhanBroker and fetches the instrument list for mapping.
        """
        self.client_id = client_id
        self.access_token = access_token
        self.dhan = dhanhq.dhanhq(self.client_id, self.access_token)
        self._instrument_map = self._fetch_and_cache_instruments()
        logging.info(f"Dhan session initialized and {len(self._instrument_map)} instruments cached.")

    def _fetch_and_cache_instruments(self) -> Dict[str, str]:
        """
        Fetches the Dhan instrument list CSV and caches it for mapping.
        """
        import requests
        import csv
        from io import StringIO

        instrument_url = "https://images.dhan.co/api-data/api-scrip-master.csv"
        instrument_map = {}
        try:
            logging.info("Downloading Dhan instrument master file...")
            response = requests.get(instrument_url, timeout=10)
            response.raise_for_status()

            csv_file = StringIO(response.text)
            reader = csv.DictReader(csv_file)

            for row in reader:
                symbol = row.get('SEM_TRADING_SYMBOL')
                security_id = row.get('SEM_SMST_SECURITY_ID')
                if symbol and security_id:
                    instrument_map[symbol] = security_id

            if not instrument_map:
                logging.error("Instrument map is empty after parsing CSV. Check Dhan's CSV format.")

            return instrument_map

        except requests.exceptions.RequestException as e:
            logging.error(f"Could not download Dhan instrument file: {e}")
            return {}
        except Exception as e:
            logging.error(f"Failed to parse Dhan instrument file: {e}")
            return {}

    def login(self, auth_details: Dict[str, Any]) -> None:
        logging.info("DhanBroker does not require a separate login step.")
        pass

    def logout(self) -> None:
        logging.info("DhanBroker does not require a logout step.")
        pass

    def place_order(self, order_details: Dict[str, Any]) -> Dict[str, Any]:
        """
        Places an order with Dhan after looking up the security_id.
        """
        symbol = order_details["symbol"]
        security_id = self._instrument_map.get(symbol)

        if not security_id:
            raise ValueError(f"Could not find security_id for symbol '{symbol}'. "
                             "Instrument might not be cached or does not exist.")

        try:
            # The Dhan API expects string literals for these parameters.
            # Our internal format for transaction_type ('BUY'/'SELL') already matches.
            transaction_type = order_details["transaction_type"]
            order_type = "MARKET"  # Assuming market order for simplicity
            product_type = "CNC"     # Assuming CNC for simplicity
            exchange_segment = "NSE_EQ" # Assuming NSE Equity

            response = self.dhan.place_order(
                security_id=security_id,
                exchange_segment=exchange_segment,
                transaction_type=transaction_type,
                quantity=order_details["quantity"],
                order_type=order_type,
                product_type=product_type,
                price=0
            )
            order_id = response.get('data', {}).get('orderId', 'N/A')
            logging.info(f"Order placed successfully with Dhan. Order ID: {order_id}")
            return {"order_id": order_id}
        except Exception as e:
            logging.error(f"Failed to place order with Dhan for symbol {symbol}: {e}")
            raise

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieves and standardizes open positions from Dhan.
        """
        try:
            response = self.dhan.get_positions()
            # On success, the API returns a list of positions.
            # On failure or if empty, it might return a dict, so we handle both.
            raw_positions = response if isinstance(response, list) else response.get('data', [])

            if not raw_positions:
                return []

            standardized_positions = []
            for pos in raw_positions:
                standardized_positions.append({
                    'tradingsymbol': pos['tradingSymbol'],
                    'quantity': pos['netQty'],
                    'product': pos['productType']
                })
            return standardized_positions
        except Exception as e:
            logging.error(f"Failed to get positions from Dhan: {e}")
            return []

    def get_holdings(self) -> List[Dict[str, Any]]:
        """
        Retrieves user's holdings from Dhan.
        """
        try:
            holdings = self.dhan.get_holdings()
            return holdings.get('data', [])
        except Exception as e:
            logging.error(f"Failed to get holdings from Dhan: {e}")
            return []