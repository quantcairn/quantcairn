from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.market_regime.detector import detect_market_regime
from src.strategy.range_strategy import RangeStrategy
from src.strategy.trend_strategy import TrendStrategy


@dataclass(frozen=True)
class NoTradeStrategy:
    name: str = "NoTradeStrategy"

    def generate_signal(self, df: Any) -> dict[str, Any]:
        return {
            "strategy": self.name,
            "action": "hold",
            "side": None,
            "reason": "event regime - no trade",
            "regime": "EVENT",
        }


def select_strategy(df: Any) -> dict[str, Any]:
    regime = detect_market_regime(df)
    if regime == "RANGE":
        strategy = RangeStrategy()
    elif regime == "TREND":
        strategy = TrendStrategy()
    else:
        strategy = NoTradeStrategy()
    return {
        "regime": regime,
        "strategy": strategy,
        "strategy_object": strategy,
    }
