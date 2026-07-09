from __future__ import annotations

from typing import Any


LEVERAGED_ETFS = {
    "SOXS",
    "LABD",
    "DRIP",
    "YINN",
    "TQQQ",
    "SQQQ",
    "SOXL",
    "LABU",
    "BOIL",
    "KOLD",
    "UVXY",
    "SPXS",
    "SPXL",
    "FAS",
    "FAZ",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _normalize_ticker(value: Any) -> str:
    return str(value or "").strip().upper()


class PortfolioManager:
    def __init__(
        self,
        *,
        max_positions: int = 3,
        max_total_exposure: float = 1.0,
        max_total_risk: float = 0.05,
        leveraged_etf_max_single_position: float = 0.15,
        leveraged_etf_max_group_exposure: float = 0.50,
    ) -> None:
        self.max_positions = max_positions
        self.max_total_exposure = float(max_total_exposure)
        self.max_total_risk = float(max_total_risk)
        self.leveraged_etf_max_single_position = float(leveraged_etf_max_single_position)
        self.leveraged_etf_max_group_exposure = float(leveraged_etf_max_group_exposure)

    def check_portfolio_risk(self, proposed_order: dict, portfolio_state: dict) -> dict:
        order = dict(proposed_order or {})
        state = dict(portfolio_state or {})

        ticker = _normalize_ticker(order.get("ticker"))
        side = str(order.get("side") or "").strip().upper()
        reduce_only = bool(order.get("reduce_only", False))
        regime = str(order.get("regime") or "RANGE").strip().upper()
        quantity = int(_safe_float(order.get("quantity"), 0.0))
        price = _safe_float(order.get("price"), 0.0)
        target_capital = _safe_float(order.get("target_capital"), 0.0)
        account_equity = _safe_float(state.get("account_equity"), 0.0)
        cash = _safe_float(state.get("cash"), 0.0)
        positions = state.get("positions") or {}
        if not isinstance(positions, dict):
            positions = {}

        current_exposure, current_leveraged_exposure, current_positions = self._portfolio_metrics(positions, account_equity)
        order_value = self._order_value(target_capital, quantity, price)
        ticker_is_leveraged = ticker in LEVERAGED_ETFS

        if side == "SELL" or reduce_only:
            reason = "sell_allowed" if side == "SELL" else "reduce_only_allowed"
            projected_exposure = current_exposure
            projected_leveraged = current_leveraged_exposure
            if side == "SELL" and ticker in positions and account_equity > 0:
                projected_exposure = max(0.0, current_exposure - (self._position_value(positions.get(ticker)) / account_equity))
                if ticker_is_leveraged:
                    projected_leveraged = max(
                        0.0,
                        current_leveraged_exposure - (self._position_value(positions.get(ticker)) / account_equity),
                    )
            return {
                "allowed": True,
                "reason": reason,
                "current_exposure": round(current_exposure, 4),
                "projected_exposure": round(projected_exposure, 4),
                "current_leveraged_etf_exposure": round(current_leveraged_exposure, 4),
                "projected_leveraged_etf_exposure": round(projected_leveraged, 4),
            }

        if side != "BUY":
            return self._blocked("invalid_order", current_exposure, current_leveraged_exposure)
        if regime == "EVENT":
            return self._blocked("event_regime_blocked", current_exposure, current_leveraged_exposure)
        if account_equity <= 0:
            return self._blocked("invalid_order", current_exposure, current_leveraged_exposure)
        if price <= 0 or quantity <= 0:
            return self._blocked("invalid_order", current_exposure, current_leveraged_exposure)
        if order_value <= 0:
            return self._blocked("invalid_order", current_exposure, current_leveraged_exposure)

        projected_exposure = current_exposure + (order_value / account_equity)
        projected_leveraged = current_leveraged_exposure + (order_value / account_equity if ticker_is_leveraged else 0.0)
        projected_positions = current_positions + (0 if ticker in positions else 1)

        if ticker_is_leveraged and (order_value / account_equity) > self.leveraged_etf_max_single_position:
            return self._blocked(
                "single_position_exceeded",
                current_exposure,
                current_leveraged_exposure,
                projected_exposure=projected_exposure,
                projected_leveraged=projected_leveraged,
            )
        if projected_leveraged > self.leveraged_etf_max_group_exposure:
            return self._blocked(
                "leveraged_group_exposure_exceeded",
                current_exposure,
                current_leveraged_exposure,
                projected_exposure=projected_exposure,
                projected_leveraged=projected_leveraged,
            )
        if projected_exposure > self.max_total_exposure:
            return self._blocked(
                "total_exposure_exceeded",
                current_exposure,
                current_leveraged_exposure,
                projected_exposure=projected_exposure,
                projected_leveraged=projected_leveraged,
            )
        if projected_positions > self.max_positions:
            return self._blocked(
                "max_positions_exceeded",
                current_exposure,
                current_leveraged_exposure,
                projected_exposure=projected_exposure,
                projected_leveraged=projected_leveraged,
            )

        return {
            "allowed": True,
            "reason": "ok",
            "current_exposure": round(current_exposure, 4),
            "projected_exposure": round(projected_exposure, 4),
            "current_leveraged_etf_exposure": round(current_leveraged_exposure, 4),
            "projected_leveraged_etf_exposure": round(projected_leveraged, 4),
        }

    def _portfolio_metrics(self, positions: dict[str, Any], account_equity: float) -> tuple[float, float, int]:
        if account_equity <= 0:
            return 0.0, 0.0, 0
        total_mv = 0.0
        leveraged_mv = 0.0
        count = 0
        for ticker, raw in positions.items():
            item = dict(raw or {}) if isinstance(raw, dict) else {}
            mv = self._position_value(item)
            qty = int(_safe_float(item.get("quantity"), 0.0))
            if mv > 0 or qty > 0:
                count += 1
            total_mv += mv
            if _normalize_ticker(ticker) in LEVERAGED_ETFS:
                leveraged_mv += mv
        return total_mv / account_equity, leveraged_mv / account_equity, count

    def _position_value(self, position: dict[str, Any] | None) -> float:
        if not position:
            return 0.0
        mv = _safe_float(position.get("market_value"), 0.0)
        if mv > 0:
            return mv
        qty = _safe_float(position.get("quantity"), 0.0)
        avg_cost = _safe_float(position.get("avg_cost"), 0.0)
        if qty > 0 and avg_cost > 0:
            return qty * avg_cost
        return 0.0

    def _order_value(self, target_capital: float, quantity: int, price: float) -> float:
        if target_capital > 0:
            return target_capital
        if quantity > 0 and price > 0:
            return quantity * price
        return 0.0

    def _blocked(
        self,
        reason: str,
        current_exposure: float,
        current_leveraged_exposure: float,
        *,
        projected_exposure: float | None = None,
        projected_leveraged: float | None = None,
    ) -> dict:
        return {
            "allowed": False,
            "reason": reason,
            "current_exposure": round(current_exposure, 4),
            "projected_exposure": round(projected_exposure if projected_exposure is not None else current_exposure, 4),
            "current_leveraged_etf_exposure": round(current_leveraged_exposure, 4),
            "projected_leveraged_etf_exposure": round(
                projected_leveraged if projected_leveraged is not None else current_leveraged_exposure,
                4,
            ),
        }
