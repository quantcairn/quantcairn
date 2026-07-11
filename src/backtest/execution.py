from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from math import floor
from typing import Any

from .models import BacktestOrder, Bar


def _coerce_order(order: BacktestOrder | dict[str, Any]) -> dict[str, Any]:
    if is_dataclass(order):
        return asdict(order)
    return dict(order)


def _normalize_side(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_type(value: Any) -> str:
    return str(value or "MARKET").strip().upper().replace(" ", "_")


class BacktestExecutionModel:
    def __init__(
        self,
        *,
        commission_per_share: float = 0.005,
        minimum_commission: float = 0.0,
        platform_fee_per_trade: float = 0.0,
        slippage_bps: float = 5.0,
        spread_bps: float = 0.0,
        participation_limit: float = 1.0,
        minimum_lot: int = 1,
        price_tick: float = 0.01,
        allow_partial_fill: bool = True,
    ) -> None:
        self.commission_per_share = max(0.0, float(commission_per_share))
        self.minimum_commission = max(0.0, float(minimum_commission))
        self.platform_fee_per_trade = max(0.0, float(platform_fee_per_trade))
        self.slippage_bps = max(0.0, float(slippage_bps))
        self.spread_bps = max(0.0, float(spread_bps))
        self.participation_limit = min(1.0, max(0.0, float(participation_limit)))
        self.minimum_lot = max(1, int(minimum_lot))
        self.price_tick = max(0.0001, float(price_tick))
        self.allow_partial_fill = bool(allow_partial_fill)

    def _apply_tick(self, value: float) -> float:
        ticks = round(value / self.price_tick)
        return round(ticks * self.price_tick, 6)

    def _market_fill_price(self, side: str, bar: Bar) -> float:
        slip = self.slippage_bps / 10000.0
        base = float(bar.open)
        if side == "BUY":
            price = base * (1.0 + slip)
        else:
            price = base * (1.0 - slip)
        return self._apply_tick(max(self.price_tick, price))

    def _limit_fill_price(self, side: str, limit_price: float, bar: Bar) -> float | None:
        if side == "BUY":
            if bar.open <= limit_price:
                return self._apply_tick(bar.open * (1.0 + self.slippage_bps / 10000.0))
            if bar.low <= limit_price:
                return self._apply_tick(limit_price * (1.0 + self.slippage_bps / 10000.0))
            return None
        if bar.open >= limit_price:
            return self._apply_tick(bar.open * (1.0 - self.slippage_bps / 10000.0))
        if bar.high >= limit_price:
            return self._apply_tick(limit_price * (1.0 - self.slippage_bps / 10000.0))
        return None

    def _stop_triggered(self, side: str, stop_price: float, bar: Bar) -> bool:
        if side == "BUY":
            return bar.high >= stop_price
        return bar.low <= stop_price

    def simulate_fill(
        self,
        order: BacktestOrder | dict[str, Any],
        bar: Bar,
        *,
        available_cash: float,
        current_position: int,
    ) -> dict[str, Any]:
        payload = _coerce_order(order)
        side = _normalize_side(payload.get("side"))
        order_type = _normalize_type(payload.get("order_type"))
        requested_quantity = int(payload.get("quantity") or payload.get("requested_quantity") or 0)
        requested_quantity = max(0, requested_quantity)
        limit_price = payload.get("limit_price")
        stop_price = payload.get("stop_price")
        order_id = str(payload.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("order_id is required")

        original_requested_quantity = requested_quantity
        result = dict(payload)
        result.setdefault("submitted_price", payload.get("submitted_price"))
        result.setdefault("filled_price", None)
        result.setdefault("filled_quantity", 0)
        result.setdefault("commission", 0.0)
        result.setdefault("fees", 0.0)
        result.setdefault("slippage", 0.0)
        result.setdefault("reject_reason", "")

        if requested_quantity <= 0 or side not in {"BUY", "SELL"}:
            result.update({"status": "REJECTED", "reject_reason": "invalid_order"})
            return result

        if side == "SELL" and current_position <= 0:
            result.update({"status": "REJECTED", "reject_reason": "no_position"})
            return result

        if side == "SELL":
            requested_quantity = min(requested_quantity, current_position)

        fill_price = None
        if order_type == "MARKET":
            fill_price = self._market_fill_price(side, bar)
        elif order_type == "LIMIT":
            if limit_price in (None, ""):
                result.update({"status": "REJECTED", "reject_reason": "missing_limit_price"})
                return result
            fill_price = self._limit_fill_price(side, float(limit_price), bar)
        elif order_type == "STOP":
            if stop_price in (None, ""):
                result.update({"status": "REJECTED", "reject_reason": "missing_stop_price"})
                return result
            if self._stop_triggered(side, float(stop_price), bar):
                fill_price = self._market_fill_price(side, bar)
        elif order_type == "STOP_LIMIT":
            if stop_price in (None, "") or limit_price in (None, ""):
                result.update({"status": "REJECTED", "reject_reason": "missing_stop_or_limit_price"})
                return result
            if self._stop_triggered(side, float(stop_price), bar):
                fill_price = self._limit_fill_price(side, float(limit_price), bar)
        else:
            result.update({"status": "REJECTED", "reject_reason": "unsupported_order_type"})
            return result

        if fill_price is None:
            result.update({"status": "PENDING", "reject_reason": "not_triggered"})
            return result

        max_volume_fill = requested_quantity
        if bar.volume <= 0:
            max_volume_fill = 0
        elif self.participation_limit < 1.0:
            max_volume_fill = max(self.minimum_lot, int(floor(bar.volume * self.participation_limit)))
            max_volume_fill = min(max_volume_fill, requested_quantity)
        if side == "BUY":
            price_with_cost = fill_price * requested_quantity + (requested_quantity * self.commission_per_share) + self.platform_fee_per_trade
            if available_cash <= 0 or price_with_cost > available_cash:
                affordable = int((available_cash - self.platform_fee_per_trade) / (fill_price + self.commission_per_share)) if available_cash > 0 else 0
                affordable = max(0, affordable)
                if affordable <= 0:
                    result.update({"status": "REJECTED", "reject_reason": "insufficient_cash"})
                    return result
                if not self.allow_partial_fill:
                    result.update({"status": "REJECTED", "reject_reason": "insufficient_cash"})
                    return result
                requested_quantity = min(requested_quantity, affordable)
        if side == "SELL":
            requested_quantity = min(requested_quantity, current_position)
        filled_quantity = min(requested_quantity, max_volume_fill)
        if filled_quantity <= 0:
            result.update({"status": "REJECTED", "reject_reason": "quantity_zero"})
            return result

        commission = max(self.minimum_commission, round(filled_quantity * self.commission_per_share, 6))
        fees = round(self.platform_fee_per_trade, 6)
        slippage_cost = abs(fill_price - float(bar.open)) * filled_quantity
        status = "FILLED" if filled_quantity == original_requested_quantity else "PARTIALLY_FILLED"
        if status == "PARTIALLY_FILLED" and not self.allow_partial_fill:
            status = "REJECTED"
            result.update({"status": status, "reject_reason": "partial_fill_not_allowed"})
            return result

        result.update(
            {
                "status": status,
                "filled_quantity": int(filled_quantity),
                "filled_price": round(fill_price, 6),
                "commission": round(commission, 6),
                "fees": round(fees, 6),
                "slippage": round(slippage_cost, 6),
                "reject_reason": "",
                "filled_at": bar.timestamp.isoformat(),
                "status_timestamp": bar.timestamp.isoformat(),
            }
        )
        return result
