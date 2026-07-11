from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@dataclass
class TradeCostEstimator:
    calculation_version: str = "trade_cost_v1"

    def estimate(
        self,
        *,
        entry_price: float,
        exit_price: float,
        quantity: int,
        commission_per_share: float = 0.005,
        platform_fee_per_trade: float = 0.0,
        spread_pct: float = 0.0,
        slippage_pct: float = 0.0,
        available_cash: float | None = None,
        minimum_net_profit: float = 0.0,
        max_spread_profit_ratio: float = 0.5,
    ) -> dict[str, Any]:
        entry_price = _coerce_float(entry_price, 0.0)
        exit_price = _coerce_float(exit_price, 0.0)
        quantity = int(quantity or 0)
        commission_per_share = max(0.0, _coerce_float(commission_per_share, 0.0))
        platform_fee_per_trade = max(0.0, _coerce_float(platform_fee_per_trade, 0.0))
        spread_pct = max(0.0, _coerce_float(spread_pct, 0.0))
        slippage_pct = max(0.0, _coerce_float(slippage_pct, 0.0))
        minimum_net_profit = max(0.0, _coerce_float(minimum_net_profit, 0.0))
        max_spread_profit_ratio = max(0.0, _coerce_float(max_spread_profit_ratio, 0.5))
        available_cash = _coerce_float(available_cash, 0.0) if available_cash is not None else None

        commission = round(quantity * commission_per_share, 6)
        platform_fee = round(platform_fee_per_trade, 6)
        spread_cost = round(entry_price * spread_pct * quantity, 6)
        slippage_cost = round(entry_price * slippage_pct * quantity, 6)
        expected_gross_profit = round((exit_price - entry_price) * quantity, 6)
        expected_net_profit = round(
            expected_gross_profit - commission - platform_fee - spread_cost - slippage_cost,
            6,
        )

        if quantity <= 0 or entry_price <= 0 or exit_price <= 0:
            return {
                "allowed": False,
                "reject_reason": "invalid_inputs",
                "commission": commission,
                "platform_fee": platform_fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "expected_gross_profit": expected_gross_profit,
                "expected_net_profit": expected_net_profit,
                "spread_profit_ratio": None,
                "buying_power_required": round(entry_price * quantity, 6),
            }

        if available_cash is not None and available_cash < (entry_price * quantity + commission + platform_fee):
            return {
                "allowed": False,
                "reject_reason": "insufficient_cash",
                "commission": commission,
                "platform_fee": platform_fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "expected_gross_profit": expected_gross_profit,
                "expected_net_profit": expected_net_profit,
                "spread_profit_ratio": None if expected_gross_profit <= 0 else round(spread_cost / expected_gross_profit, 6),
                "buying_power_required": round(entry_price * quantity, 6),
            }

        spread_profit_ratio = None
        if expected_gross_profit > 0:
            spread_profit_ratio = spread_cost / expected_gross_profit

        if spread_profit_ratio is not None and spread_profit_ratio > max_spread_profit_ratio:
            return {
                "allowed": False,
                "reject_reason": "spread_too_wide",
                "commission": commission,
                "platform_fee": platform_fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "expected_gross_profit": expected_gross_profit,
                "expected_net_profit": expected_net_profit,
                "spread_profit_ratio": round(spread_profit_ratio, 6),
                "buying_power_required": round(entry_price * quantity, 6),
            }

        if expected_net_profit <= commission + platform_fee + spread_cost + slippage_cost:
            return {
                "allowed": False,
                "reject_reason": "cost_exceeds_edge",
                "commission": commission,
                "platform_fee": platform_fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "expected_gross_profit": expected_gross_profit,
                "expected_net_profit": expected_net_profit,
                "spread_profit_ratio": round(spread_profit_ratio, 6) if spread_profit_ratio is not None else None,
                "buying_power_required": round(entry_price * quantity, 6),
            }

        if expected_gross_profit <= 0:
            return {
                "allowed": False,
                "reject_reason": "expected_profit_too_low",
                "commission": commission,
                "platform_fee": platform_fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "expected_gross_profit": expected_gross_profit,
                "expected_net_profit": expected_net_profit,
                "spread_profit_ratio": spread_profit_ratio,
                "buying_power_required": round(entry_price * quantity, 6),
            }

        if expected_net_profit <= minimum_net_profit:
            return {
                "allowed": False,
                "reject_reason": "expected_profit_too_low",
                "commission": commission,
                "platform_fee": platform_fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "expected_gross_profit": expected_gross_profit,
                "expected_net_profit": expected_net_profit,
                "spread_profit_ratio": spread_profit_ratio,
                "buying_power_required": round(entry_price * quantity, 6),
            }

        return {
            "allowed": True,
            "reject_reason": "",
            "commission": commission,
            "platform_fee": platform_fee,
            "spread_cost": spread_cost,
            "slippage_cost": slippage_cost,
            "expected_gross_profit": expected_gross_profit,
            "expected_net_profit": expected_net_profit,
            "spread_profit_ratio": round(spread_profit_ratio, 6) if spread_profit_ratio is not None else None,
            "buying_power_required": round(entry_price * quantity, 6),
        }
