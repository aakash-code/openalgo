import logging
from typing import Any, Dict, List
from kiteconnect import KiteConnect
from ..core.broker_interface import BrokerInterface

logging.basicConfig(level=logging.INFO)

class ZerodhaBroker(BrokerInterface):
    """
    Implementation of the BrokerInterface for Zerodha Kite Connect.
    """

    def __init__(self, api_key: str, api_secret: str, access_token: str = None):
        """
        Initializes the ZerodhaBroker.

        Args:
            api_key (str): The API key for Kite Connect.
            api_secret (str): The API secret for Kite Connect.
            access_token (str, optional): A pre-existing access token. Defaults to None.
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.kite = KiteConnect(api_key=self.api_key)

        if access_token:
            self.kite.set_access_token(access_token)
            logging.info("Zerodha session initialized with existing access token.")

    def login(self, auth_details: Dict[str, Any]) -> None:
        """
        Logs into Zerodha. For Kite Connect, this typically means generating
        and setting the access token. This implementation assumes a request_token
        is passed in auth_details to generate a new session.

        Args:
            auth_details (Dict[str, Any]): A dictionary containing the 'request_token'.
        """
        request_token = auth_details.get("request_token")
        if not request_token:
            raise ValueError("A request_token is required to generate a new session.")

        try:
            data = self.kite.generate_session(request_token, api_secret=self.api_secret)
            self.kite.set_access_token(data["access_token"])
            logging.info("Successfully generated new Zerodha session.")
            # It's the responsibility of the calling code to persist this new access token.
            logging.info(f"New access token: {data['access_token']}")
        except Exception as e:
            logging.error(f"Authentication failed: {e}")
            raise

    def logout(self) -> None:
        """
        Invalidates the current access token.
        """
        try:
            # The token is invalidated on the server side by this call.
            self.kite.invalidate_access_token()
            logging.info("Zerodha session invalidated.")
        except Exception as e:
            logging.error(f"Logout failed: {e}")
            raise

    def place_order(self, order_details: Dict[str, Any]) -> Dict[str, Any]:
        """
        Places an order with Zerodha.

        Args:
            order_details (Dict[str, Any]): A dictionary containing order parameters
            like 'symbol', 'quantity', 'transaction_type', 'order_type', etc.
        """
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,  # Assuming NSE, can be parameterized
                tradingsymbol=order_details["symbol"],
                transaction_type=order_details["transaction_type"],
                quantity=order_details["quantity"],
                product=order_details["product_type"],
                order_type=order_details["order_type"],
            )
            logging.info(f"Order placed successfully. Order ID: {order_id}")
            return {"order_id": order_id}
        except Exception as e:
            logging.error(f"Failed to place order: {e}")
            raise

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieves the current open net positions.
        """
        try:
            positions = self.kite.positions()
            return positions.get('net', [])
        except Exception as e:
            logging.error(f"Failed to get positions: {e}")
            raise

    def get_holdings(self) -> List[Dict[str, Any]]:
        """
        Retrieves the user's holdings.
        """
        try:
            holdings = self.kite.holdings()
            return holdings
        except Exception as e:
            logging.error(f"Failed to get holdings: {e}")
            raise