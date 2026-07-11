from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Iterable, Sequence


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_series(values: Sequence[Any] | Iterable[Any]) -> list[float]:
    series: list[float] = []
    for value in list(values or []):
        try:
            series.append(float(value))
        except (TypeError, ValueError):
            continue
    return series


def _slope_pct(values: Sequence[float]) -> float:
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return ((values[-1] - values[0]) / values[0]) * 100.0


def _range_pct(values: Sequence[float]) -> float:
    if not values or values[0] == 0:
        return 0.0
    return ((max(values) - min(values)) / values[0]) * 100.0


def _volatility_pct(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    returns: list[float] = []
    for prev, current in zip(values, values[1:]):
        if prev == 0:
            continue
        returns.append(((current - prev) / prev) * 100.0)
    if len(returns) < 2:
        return 0.0
    return pstdev(returns)


def _benchmark_slope_pct(values: Sequence[float]) -> float:
    return _slope_pct(values)


@dataclass
class TrendGuard:
    calculation_version: str = "trend_guard_v1"

    def evaluate(
        self,
        *,
        timestamp: Any,
        current_price: float,
        closes: Sequence[Any],
        highs: Sequence[Any] | None = None,
        lows: Sequence[Any] | None = None,
        volumes: Sequence[Any] | None = None,
        benchmark_closes: Sequence[Any] | None = None,
        symbol: str | None = None,
        lookback: int = 20,
        strong_trend_slope_pct: float = 2.5,
        high_volatility_pct: float = 5.0,
        invalid_range_pct: float = 1.0,
        cooldown_minutes: int = 30,
    ) -> dict[str, Any]:
        closes_v = _to_series(closes)[-max(2, int(lookback or 20)) :]
        benchmark_v = _to_series(benchmark_closes or [])
        current_price = _coerce_float(current_price, 0.0)
        ts = timestamp if isinstance(timestamp, datetime) else None

        if len(closes_v) < 5 or current_price <= 0:
            return {
                "regime": "UNKNOWN",
                "trend_score": 0.0,
                "buy_allowed": False,
                "sell_allowed": True,
                "symbol_reduce_only": False,
                "cooldown_until": None,
                "trigger_reasons": ["insufficient_data" if len(closes_v) < 5 else "invalid_price"],
                "data_timestamp": ts.isoformat() if ts else None,
                "calculation_version": self.calculation_version,
            }

        slope = _slope_pct(closes_v)
        price_range = _range_pct(closes_v)
        volatility = _volatility_pct(closes_v)
        prev_window = closes_v[:-1] if len(closes_v) > 1 else closes_v
        breakout_up = closes_v[-1] >= max(prev_window) * 0.995 if prev_window else False
        breakout_down = closes_v[-1] <= min(prev_window) * 1.005 if prev_window else False

        benchmark_slope = _benchmark_slope_pct(benchmark_v[-max(2, int(lookback or 20)) :]) if benchmark_v else 0.0
        symbol_norm = str(symbol or "").strip().upper().split(".")[0]
        benchmark_up_block = symbol_norm == "SOXS" and benchmark_slope > strong_trend_slope_pct / 2.0
        benchmark_down_block = symbol_norm == "SOXS" and benchmark_slope < -strong_trend_slope_pct / 2.0

        regime = "RANGE"
        trigger_reasons: list[str] = []
        if price_range < invalid_range_pct:
            regime = "INVALID_RANGE"
            trigger_reasons.append("range_too_narrow")
        elif volatility >= high_volatility_pct:
            regime = "HIGH_VOLATILITY"
            trigger_reasons.append("volatility_too_high")
        elif slope >= strong_trend_slope_pct and breakout_up:
            regime = "STRONG_UPTREND"
            trigger_reasons.append("strong_uptrend")
        elif slope <= -strong_trend_slope_pct and breakout_down:
            regime = "STRONG_DOWNTREND"
            trigger_reasons.append("strong_downttrend")

        if benchmark_up_block:
            trigger_reasons.append("benchmark_strong_uptrend_blocks_soxs_buy")
        if benchmark_down_block:
            trigger_reasons.append("benchmark_strong_downtrend_blocks_soxs_buy")

        buy_allowed = regime == "RANGE" and not benchmark_up_block and not benchmark_down_block
        sell_allowed = True
        symbol_reduce_only = benchmark_up_block
        trend_score = max(0.0, min(100.0, 100.0 - abs(slope) * 10.0 - volatility * 3.0))
        cooldown_until = None
        if regime in {"STRONG_UPTREND", "STRONG_DOWNTREND", "HIGH_VOLATILITY", "INVALID_RANGE"} or benchmark_up_block:
            if ts is not None:
                cooldown_until = (ts + timedelta(minutes=int(cooldown_minutes))).isoformat()

        if regime == "INVALID_RANGE":
            buy_allowed = False
        if regime == "HIGH_VOLATILITY":
            buy_allowed = False
        if regime == "STRONG_UPTREND":
            buy_allowed = False
        if regime == "STRONG_DOWNTREND":
            buy_allowed = False

        return {
            "regime": regime,
            "trend_score": round(trend_score, 6),
            "buy_allowed": bool(buy_allowed),
            "sell_allowed": bool(sell_allowed),
            "symbol_reduce_only": bool(symbol_reduce_only),
            "cooldown_until": cooldown_until,
            "trigger_reasons": trigger_reasons,
            "price_slope_pct": round(slope, 6),
            "price_range_pct": round(price_range, 6),
            "volatility_pct": round(volatility, 6),
            "benchmark_slope_pct": round(benchmark_slope, 6),
            "data_timestamp": ts.isoformat() if ts else None,
            "calculation_version": self.calculation_version,
        }
