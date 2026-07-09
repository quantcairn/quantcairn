from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


class RiskAllocator:
    def allocate_positions(self, signals: list, total_capital: float, reserve_cash: float = 100.0) -> dict:
        total_capital = float(total_capital or 0.0)
        reserve_cash = max(0.0, float(reserve_cash or 0.0))
        available_capital = max(0.0, total_capital - reserve_cash)
        if available_capital <= 0 or not signals:
            return {}

        candidates: list[dict[str, Any]] = []
        raw_weights: list[float] = []
        for raw in signals or []:
            item = dict(raw)
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            final_score = _safe_float(item.get("final_score"), _safe_float(item.get("score"), 0.0)) or 0.0
            atr_pct = _safe_float(item.get("atr_pct"), 0.05)
            if atr_pct is None or atr_pct <= 0:
                atr_pct = 0.05
            price = _safe_float(item.get("price"), _safe_float(item.get("current_price"), 0.0))
            if price is None or price <= 0:
                low = _safe_float(item.get("range_low"))
                high = _safe_float(item.get("range_high"))
                if low is not None and high is not None and high > low > 0:
                    price = (low + high) / 2.0
            price = float(price or 0.0)
            regime = str(item.get("regime") or "RANGE").strip().upper()

            if regime == "EVENT":
                raw_weight = 0.0
            else:
                raw_weight = max(0.0, final_score) / max(atr_pct, 1e-6)

            item["ticker"] = ticker
            item["final_score"] = final_score
            item["atr_pct"] = atr_pct
            item["price"] = price
            item["regime"] = regime
            candidates.append(item)
            raw_weights.append(raw_weight)

        total_raw = sum(raw_weights)
        per_ticker_cap = available_capital * 0.15
        allocations: dict[str, dict[str, Any]] = {}
        for item, raw_weight in zip(candidates, raw_weights):
            ticker = item["ticker"]
            price = float(item.get("price") or 0.0)
            atr_pct = float(item.get("atr_pct") or 0.05)
            regime = str(item.get("regime") or "RANGE").strip().upper()

            if regime == "EVENT":
                capital = 0.0
                shares = 0
                actual_weight = 0.0
                risk_pct = 0.0
            elif price <= 0:
                capital = 0.0
                shares = 0
                actual_weight = 0.0
                risk_pct = 0.0
            else:
                normalized_weight = (raw_weight / total_raw) if total_raw > 0 else 0.0
                proposed_capital = available_capital * normalized_weight
                capital = min(proposed_capital, per_ticker_cap)
                if capital > available_capital:
                    capital = available_capital
                shares = _safe_int(capital // price, 0)
                if shares <= 0:
                    capital = 0.0
                actual_weight = capital / available_capital if available_capital > 0 else 0.0
                risk_pct = 1.0 if capital > 0 else 0.0

            if regime == "EVENT":
                reason = "event_regime"
            elif price <= 0:
                reason = "invalid_price"
            elif total_raw <= 0:
                reason = "no_allocatable_signals"
            elif capital > 0:
                reason = "risk_adjusted_allocation"
            else:
                reason = "no_allocation"

            allocations[ticker] = {
                "capital": round(float(capital), 2),
                "shares": int(shares),
                "weight": round(float(actual_weight), 4),
                "risk_pct": round(float(risk_pct), 2),
                "atr_pct": round(float(atr_pct), 4),
                "reason": reason,
            }

        return allocations
