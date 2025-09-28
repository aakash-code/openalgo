from abc import ABC, abstractmethod
from typing import Any, Dict, List

class BrokerInterface(ABC):
    """
    An abstract base class for all broker implementations.
    """

    @abstractmethod
    def login(self, auth_details: Dict[str, Any]) -> None:
        """
        Logs into the broker's API.
        """
        pass

    @abstractmethod
    def logout(self) -> None:
        """
        Logs out from the broker's API.
        """
        pass

    @abstractmethod
    def place_order(self, order_details: Dict[str, Any]) -> Dict[str, Any]:
        """
        Places an order with the broker.
        """
        pass

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieves the current open positions.
        """
        pass

    @abstractmethod
    def get_holdings(self) -> List[Dict[str, Any]]:
        """
        Retrieves the user's holdings.
        """
        pass