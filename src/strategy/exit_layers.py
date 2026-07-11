from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


def _normalize_layer_status(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


@dataclass
class ExitLayerManager:
    def plan_exits(
        self,
        *,
        filled_entry_layers: Iterable[Any],
        current_price: float,
        grid_width: float,
        current_broker_position: int,
        pending_sell_exists: bool = False,
    ) -> dict[str, Any]:
        current_price = float(current_price or 0.0)
        grid_width = float(grid_width or 0.0)
        current_broker_position = int(current_broker_position or 0)

        if pending_sell_exists:
            return {
                "allowed": False,
                "reject_reason": "pending_sell_exists",
                "orders": [],
                "warnings": [],
            }
        if current_broker_position <= 0:
            return {
                "allowed": False,
                "reject_reason": "no_position",
                "orders": [],
                "warnings": [],
            }
        if current_price <= 0 or grid_width <= 0:
            return {
                "allowed": False,
                "reject_reason": "invalid_market_state",
                "orders": [],
                "warnings": [],
            }

        remaining_position = current_broker_position
        orders: list[dict[str, Any]] = []
        warnings: list[str] = []
        for layer in sorted(filled_entry_layers or [], key=lambda item: int(item.get("layer_id", item.get("id", 0))) if isinstance(item, dict) else 0):
            if not isinstance(layer, dict):
                continue
            layer_status = _normalize_layer_status(layer.get("status"))
            exit_status = _normalize_layer_status(layer.get("exit_status"))
            if layer_status not in {"filled", "partially_filled"}:
                continue
            if exit_status in {"exited", "closed", "filled"}:
                continue
            try:
                layer_id = int(layer.get("layer_id", layer.get("id")))
            except (TypeError, ValueError):
                continue
            already_sold = int(layer.get("exited_quantity", 0) or 0)
            layer_qty = int(layer.get("filled_quantity", layer.get("target_quantity", 0)) or 0) - already_sold
            if layer_qty <= 0:
                continue
            sell_quantity = min(layer_qty, remaining_position)
            if sell_quantity <= 0:
                continue
            entry_price = float(layer.get("average_fill_price", layer.get("entry_price", current_price)) or current_price)
            target_price = float(layer.get("exit_target", entry_price + grid_width) or (entry_price + grid_width))
            orders.append(
                {
                    "layer_id": layer_id,
                    "sell_quantity": int(sell_quantity),
                    "target_price": round(target_price, 6),
                    "reason": "layer_take_profit",
                    "status": "planned",
                }
            )
            remaining_position -= sell_quantity
            if remaining_position <= 0:
                break

        if not orders:
            return {
                "allowed": False,
                "reject_reason": "no_exit_layers",
                "orders": [],
                "warnings": warnings,
            }

        return {
            "allowed": True,
            "reject_reason": "",
            "orders": orders,
            "warnings": warnings,
        }
