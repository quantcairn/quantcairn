from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


def _clamp_ratio(value: float) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        ratio = 0.0
    return max(0.0, min(1.0, ratio))


@dataclass
class InventoryAwareSizer:
    """
    Cash-only inventory aware sizer.

    Designed to stay fail-closed and avoid margin/buying-power based sizing.
    """

    def adjust_quantity(
        self,
        *,
        base_quantity: int,
        current_position_value: float,
        allowed_position_value: float,
        available_cash: float,
        current_price: float,
        leveraged_etf: bool = False,
        leveraged_etf_limit: float = 0.1,
        cash_reserve_ratio: float = 0.2,
    ) -> dict[str, Any]:
        base_quantity = int(base_quantity or 0)
        current_position_value = float(current_position_value or 0.0)
        allowed_position_value = float(allowed_position_value or 0.0)
        available_cash = float(available_cash or 0.0)
        current_price = float(current_price or 0.0)
        leveraged_etf_limit = _clamp_ratio(leveraged_etf_limit)
        cash_reserve_ratio = _clamp_ratio(cash_reserve_ratio)

        if base_quantity <= 0:
            return {
                "inventory_ratio": 0.0,
                "adjusted_quantity": 0,
                "adjustment_factor": 0.0,
                "allowed": False,
                "reject_reason": "invalid_base_quantity",
            }

        if current_price <= 0:
            return {
                "inventory_ratio": 0.0 if allowed_position_value <= 0 else 1.0,
                "adjusted_quantity": 0,
                "adjustment_factor": 0.0,
                "allowed": False,
                "reject_reason": "invalid_price",
            }

        if available_cash <= 0:
            return {
                "inventory_ratio": 0.0 if allowed_position_value <= 0 else 1.0,
                "adjusted_quantity": 0,
                "adjustment_factor": 0.0,
                "allowed": False,
                "reject_reason": "insufficient_cash",
            }

        effective_allowed_position_value = max(0.0, allowed_position_value)
        if leveraged_etf:
            leveraged_cap = available_cash * leveraged_etf_limit
            effective_allowed_position_value = min(effective_allowed_position_value, max(0.0, leveraged_cap))

        if effective_allowed_position_value <= 0:
            return {
                "inventory_ratio": 1.0,
                "adjusted_quantity": 0,
                "adjustment_factor": 0.0,
                "allowed": False,
                "reject_reason": "inventory_limit",
            }

        inventory_ratio = current_position_value / effective_allowed_position_value if effective_allowed_position_value > 0 else 1.0
        if inventory_ratio >= 0.8:
            return {
                "inventory_ratio": round(inventory_ratio, 6),
                "adjusted_quantity": 0,
                "adjustment_factor": 0.0,
                "allowed": False,
                "reject_reason": "inventory_limit",
            }

        if inventory_ratio >= 0.6:
            inventory_factor = 0.4
        elif inventory_ratio >= 0.3:
            inventory_factor = 0.7
        else:
            inventory_factor = 1.0

        tradable_cash = max(0.0, available_cash * (1.0 - cash_reserve_ratio))
        cash_cap_quantity = int(math.floor(tradable_cash / current_price)) if tradable_cash > 0 else 0

        remaining_value_room = max(0.0, effective_allowed_position_value - current_position_value)
        room_quantity = int(math.floor(remaining_value_room / current_price)) if remaining_value_room > 0 else 0

        adjusted_quantity = int(math.floor(base_quantity * inventory_factor))
        adjusted_quantity = min(adjusted_quantity, cash_cap_quantity, room_quantity)

        if adjusted_quantity <= 0:
            return {
                "inventory_ratio": round(inventory_ratio, 6),
                "adjusted_quantity": 0,
                "adjustment_factor": 0.0,
                "allowed": False,
                "reject_reason": "quantity_zero",
            }

        adjustment_factor = adjusted_quantity / base_quantity if base_quantity > 0 else 0.0
        return {
            "inventory_ratio": round(inventory_ratio, 6),
            "adjusted_quantity": adjusted_quantity,
            "adjustment_factor": round(adjustment_factor, 6),
            "allowed": True,
            "reject_reason": "",
            "inventory_factor": inventory_factor,
            "cash_cap_quantity": cash_cap_quantity,
            "room_quantity": room_quantity,
            "effective_allowed_position_value": round(effective_allowed_position_value, 6),
        }

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        return self.adjust_quantity(**kwargs)
