from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable


DEFAULT_LAYER_WEIGHTS = [0.25, 0.25, 0.20, 0.15, 0.15]


def _normalize_existing_ids(existing_layers: Iterable[Any] | None) -> set[int]:
    existing_ids: set[int] = set()
    for layer in existing_layers or []:
        if isinstance(layer, dict):
            raw_id = layer.get("layer_id", layer.get("id"))
            try:
                layer_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            existing_ids.add(layer_id)
        else:
            try:
                existing_ids.add(int(layer))
            except (TypeError, ValueError):
                continue
    return existing_ids


def _weighted_quantities(total: int, layer_count: int, weights: list[float]) -> list[int]:
    if total <= 0 or layer_count <= 0:
        return []
    raw = weights[:layer_count]
    total_weight = sum(raw) or 1.0
    normalized = [weight / total_weight for weight in raw]
    quantities = [int(math.floor(total * weight)) for weight in normalized]
    remainder = total - sum(quantities)
    for index in range(layer_count):
        if remainder <= 0:
            break
        quantities[index] += 1
        remainder -= 1
    for index in range(1, layer_count):
        if quantities[index] > quantities[index - 1]:
            quantities[index] = quantities[index - 1]
    while sum(quantities) > total:
        for index in reversed(range(layer_count)):
            if sum(quantities) <= total:
                break
            if quantities[index] > 0:
                quantities[index] -= 1
    return quantities


@dataclass
class EntryLayerPlanner:
    default_weights: list[float] = None

    def __post_init__(self) -> None:
        if self.default_weights is None:
            self.default_weights = list(DEFAULT_LAYER_WEIGHTS)

    def plan_layers(
        self,
        *,
        support: float,
        grid_width: float,
        total_target_quantity: int,
        max_layers: int = 5,
        existing_layers: Iterable[Any] | None = None,
        pending_buy_exists: bool = False,
        inventory_ratio: float = 0.0,
        trend_buy_allowed: bool = True,
    ) -> dict[str, Any]:
        support = float(support or 0.0)
        grid_width = float(grid_width or 0.0)
        total_target_quantity = int(total_target_quantity or 0)
        max_layers = max(1, min(5, int(max_layers or 5)))
        inventory_ratio = float(inventory_ratio or 0.0)

        if support <= 0 or grid_width <= 0:
            return {
                "allowed": False,
                "reject_reason": "invalid_range",
                "layers": [],
                "warnings": [],
            }
        if total_target_quantity <= 0:
            return {
                "allowed": False,
                "reject_reason": "invalid_quantity",
                "layers": [],
                "warnings": [],
            }
        if pending_buy_exists:
            return {
                "allowed": False,
                "reject_reason": "pending_buy_exists",
                "layers": [],
                "warnings": [],
            }
        if not trend_buy_allowed:
            return {
                "allowed": False,
                "reject_reason": "trend_buy_blocked",
                "layers": [],
                "warnings": [],
            }
        if inventory_ratio >= 0.8:
            return {
                "allowed": False,
                "reject_reason": "inventory_limit",
                "layers": [],
                "warnings": [],
            }

        existing_ids = _normalize_existing_ids(existing_layers)
        weights = self.default_weights[:max_layers]
        quantities = _weighted_quantities(total_target_quantity, max_layers, weights)
        layers: list[dict[str, Any]] = []
        for index in range(max_layers):
            layer_id = index + 1
            target_quantity = int(quantities[index] if index < len(quantities) else 0)
            if target_quantity <= 0:
                continue
            if layer_id in existing_ids:
                continue
            trigger_offset = grid_width * 0.05 * max(0, (max_layers - index - 1))
            trigger_price = round(max(0.01, support + trigger_offset), 6)
            if trigger_price <= 0:
                continue
            layers.append(
                {
                    "layer_id": layer_id,
                    "trigger_price": trigger_price,
                    "target_quantity": target_quantity,
                    "status": "planned",
                    "reason": "layered_entry_plan",
                    "weight": weights[index] if index < len(weights) else 0.0,
                }
            )

        return {
            "allowed": bool(layers),
            "reject_reason": "" if layers else "no_layers_generated",
            "layers": layers,
            "warnings": [] if layers else ["no_layers_generated"],
        }
