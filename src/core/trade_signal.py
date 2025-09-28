from dataclasses import dataclass

@dataclass
class TradeSignal:
    """
    A standardized representation of a trade signal.
    """
    symbol: str
    quantity: int
    transaction_type: str  # 'BUY' or 'SELL'
    order_type: str        # 'MARKET', 'LIMIT', etc.
    product_type: str      # 'CNC', 'MIS', etc.