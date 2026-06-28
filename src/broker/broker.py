from typing import Any, Dict, Protocol


class BrokerInterface(Protocol):
    def place_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Place an order. Return a dict with at least order_id and status."""

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order by id."""

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Return order status."""

    def get_positions(self) -> Dict[str, Any]:
        """Return current positions."""
