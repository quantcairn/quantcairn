from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return default


class PortfolioAllocator:
    def allocate(self, signals: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        clean: list[dict[str, Any]] = []
        for signal in signals:
            ticker = str(signal.get("ticker", "")).strip().upper()
            score = _safe_float(signal.get("score"), 0.0)
            volatility = max(_safe_float(signal.get("volatility"), 0.0), 1e-9)
            if not ticker or score <= 0:
                continue
            clean.append(
                {
                    "ticker": ticker,
                    "score": score,
                    "volatility": volatility,
                    "risk_adjusted_weight": (score / volatility),
                }
            )

        if not clean:
            return {}

        total_score = sum(item["score"] for item in clean)
        if total_score <= 0:
            return {}

        raw_weights = {}
        adjusted_weights = {}
        for item in clean:
            ticker = item["ticker"]
            raw_weight = item["score"] / total_score
            adjusted = raw_weight * (1.0 / item["volatility"])
            raw_weights[ticker] = raw_weight
            adjusted_weights[ticker] = adjusted

        adjusted_total = sum(adjusted_weights.values())
        if adjusted_total <= 0:
            return {}

        allocation: dict[str, dict[str, float]] = {}
        for ticker, adjusted in adjusted_weights.items():
            allocation[ticker] = {
                "position_size": adjusted / adjusted_total,
                "leverage": 1.0,
                "weight_raw": raw_weights[ticker],
                "risk_adjusted_weight": adjusted,
            }
        return allocation
