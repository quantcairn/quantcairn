from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.market_regime.detector import compute_market_features


def _series(df: Any, *candidates: str) -> list[float]:
    columns = getattr(df, "columns", None)
    if columns is not None:
        lookup = {str(col).lower(): col for col in columns}
        for candidate in candidates:
            key = candidate.lower()
            if key in lookup:
                raw = df[lookup[key]]
                if hasattr(raw, "tolist"):
                    values = raw.tolist()
                else:
                    values = list(raw)
                return [float(value) for value in values]
    return []


@dataclass(frozen=True)
class RangeStrategy:
    name: str = "RangeStrategy"

    def calculate_levels(self, df: Any) -> dict[str, float]:
        lows = _series(df, "low", "low_price", "l")
        highs = _series(df, "high", "high_price", "h")
        closes = _series(df, "close", "close_price", "c")
        if len(lows) < 20 or len(highs) < 20 or len(closes) < 1:
            raise ValueError("RangeStrategy requires at least 20 rows")
        support = min(lows[-20:])
        resistance = max(highs[-20:])
        mid_range = (support + resistance) / 2.0
        features = compute_market_features(df)
        return {
            "support": support,
            "resistance": resistance,
            "mid_range": mid_range,
            "atr": features.atr,
            "rsi": features.rsi,
            "price": closes[-1],
        }

    def generate_signal(self, df: Any) -> dict[str, Any]:
        levels = self.calculate_levels(df)
        price = levels["price"]
        support = levels["support"]
        resistance = levels["resistance"]
        mid_range = levels["mid_range"]
        atr = levels["atr"]
        rsi = levels["rsi"]

        near_support = support > 0 and abs(price - support) / support <= 0.02
        near_resistance = resistance > 0 and abs(resistance - price) / resistance <= 0.02

        action = "hold"
        side = None
        entry_price = None
        if near_support and rsi < 35:
            action = "buy"
            side = "long"
            entry_price = price
        elif near_resistance and rsi > 65:
            action = "sell"
            side = "short"
            entry_price = price

        stop_distance = 1.2 * atr
        stop_loss_price = None
        if side == "long":
            stop_loss_price = price - stop_distance
        elif side == "short":
            stop_loss_price = price + stop_distance

        return {
            "strategy": self.name,
            "action": action,
            "side": side,
            "entry_price": entry_price,
            "target_price": mid_range,
            "stop_loss": stop_distance,
            "stop_loss_price": stop_loss_price,
            "support": support,
            "resistance": resistance,
            "mid_range": mid_range,
            "price": price,
            "rsi": rsi,
            "atr": atr,
            "regime": "RANGE",
        }
