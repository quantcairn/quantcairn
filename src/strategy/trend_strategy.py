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
class TrendStrategy:
    name: str = "TrendStrategy"

    def generate_signal(self, df: Any) -> dict[str, Any]:
        closes = _series(df, "close", "close_price", "c")
        highs = _series(df, "high", "high_price", "h")
        volumes = _series(df, "volume", "vol", "v")
        if len(closes) < 50:
            raise ValueError("TrendStrategy requires at least 50 rows")

        features = compute_market_features(df)
        price = closes[-1]
        sma20 = features.sma20
        sma50 = features.sma50
        avg_volume = sum(volumes[-20:]) / min(len(volumes), 20) if volumes else 0.0
        if len(highs) > 20:
            recent_window = highs[-21:-1]
        else:
            recent_window = highs[:-1]
        recent_high = max(recent_window) if recent_window else max(highs)

        breakout = price >= recent_high
        pullback = sma20 > 0 and abs(price - sma20) / sma20 <= 0.02
        volume_ok = not volumes or volumes[-1] > avg_volume
        long_setup = price > sma50 and sma20 > sma50 and volume_ok and (breakout or pullback)

        atr = features.atr
        trailing_stop = price - (3.0 * atr)

        return {
            "strategy": self.name,
            "action": "buy" if long_setup else "hold",
            "side": "long" if long_setup else None,
            "entry_type": "breakout" if breakout else "pullback" if pullback else None,
            "price": price,
            "sma20": sma20,
            "sma50": sma50,
            "avg_volume": avg_volume,
            "volume_ok": volume_ok,
            "atr_trailing_stop": trailing_stop,
            "atr": atr,
            "regime": "TREND",
        }
