from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Sequence


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(float(raw))
            except Exception:
                return None
    return None


def _extract_series(series: Sequence[Any] | Iterable[Any], cutoff: Any = None) -> tuple[list[float], datetime | None]:
    cutoff_dt = _coerce_datetime(cutoff)
    values: list[float] = []
    timestamps: list[datetime] = []
    for item in list(series or []):
        item_ts = None
        value = item
        if isinstance(item, dict):
            item_ts = _coerce_datetime(item.get("timestamp") or item.get("time") or item.get("datetime"))
            value = item.get("value", item.get("close", item.get("high", item.get("low"))))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            item_ts = _coerce_datetime(item[0])
            value = item[1]

        if cutoff_dt is not None and item_ts is not None and item_ts > cutoff_dt:
            continue

        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue

        values.append(numeric)
        if item_ts is not None:
            timestamps.append(item_ts)

    latest_ts = max(timestamps) if timestamps else cutoff_dt
    return values, latest_ts


def _ema(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = float(values[0])
    for value in values[1:]:
        ema = (float(value) * alpha) + (ema * (1.0 - alpha))
    return ema


def _true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr_values: list[float] = []
    prev_close = None
    for high, low, close in zip(highs, lows, closes):
        high_f = float(high)
        low_f = float(low)
        close_f = float(close)
        if prev_close is None:
            tr = high_f - low_f
        else:
            tr = max(high_f - low_f, abs(high_f - prev_close), abs(low_f - prev_close))
        tr_values.append(max(0.0, tr))
        prev_close = close_f
    return tr_values


def _atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> float | None:
    if period <= 0:
        return None
    if len(closes) < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return None
    tr_values = _true_ranges(highs[-(period + 1):], lows[-(period + 1):], closes[-(period + 1):])
    if len(tr_values) < period:
        return None
    window = tr_values[-period:]
    return sum(window) / len(window) if window else None


def _range_quality(range_width_pct: float, minimum_range_pct: float, maximum_range_pct: float) -> str:
    if range_width_pct <= 0:
        return "invalid"
    if range_width_pct <= minimum_range_pct * 1.5:
        return "tight"
    midpoint = (minimum_range_pct + maximum_range_pct) / 2.0
    if range_width_pct <= midpoint:
        return "balanced"
    return "wide"


@dataclass
class DynamicRangeCalculator:
    calculation_version: str = "dynamic_range_v1"

    def calculate(
        self,
        *,
        timestamp: Any,
        current_price: float,
        highs: Sequence[Any],
        lows: Sequence[Any],
        closes: Sequence[Any],
        atr_period: int = 14,
        ema_period: int = 20,
        rolling_lookback: int = 20,
        lookback_period: int | None = None,
        atr_multiplier: float = 1.5,
        minimum_range_pct: float = 1.0,
        maximum_range_pct: float = 12.0,
        support_buffer: float = 0.2,
        resistance_buffer: float = 0.2,
        warmup_bars: int | None = None,
    ) -> dict[str, Any]:
        cutoff = _coerce_datetime(timestamp)
        highs_v, latest_high_ts = _extract_series(highs, cutoff=cutoff)
        lows_v, latest_low_ts = _extract_series(lows, cutoff=cutoff)
        closes_v, latest_close_ts = _extract_series(closes, cutoff=cutoff)

        latest_ts = max(
            [ts for ts in [latest_high_ts, latest_low_ts, latest_close_ts, cutoff] if ts is not None],
            default=None,
        )

        lookback = int(lookback_period or rolling_lookback or 20)
        warmup_needed = int(warmup_bars or max(ema_period, atr_period, lookback))
        if len(closes_v) < warmup_needed or len(highs_v) < warmup_needed or len(lows_v) < warmup_needed:
            return {
                "center": None,
                "support": None,
                "resistance": None,
                "grid_width": 0.0,
                "range_width_pct": 0.0,
                "range_quality": "invalid",
                "valid": False,
                "invalid_reason": "insufficient_data",
                "calculation_version": self.calculation_version,
                "data_timestamp": latest_ts.isoformat() if latest_ts else None,
            }

        current_price = float(current_price or 0.0)
        if current_price <= 0:
            return {
                "center": None,
                "support": None,
                "resistance": None,
                "grid_width": 0.0,
                "range_width_pct": 0.0,
                "range_quality": "invalid",
                "valid": False,
                "invalid_reason": "invalid_current_price",
                "calculation_version": self.calculation_version,
                "data_timestamp": latest_ts.isoformat() if latest_ts else None,
            }

        highs_window = highs_v[-lookback:]
        lows_window = lows_v[-lookback:]
        closes_window = closes_v[-lookback:]
        ema_value = _ema(closes_window, min(ema_period, len(closes_window)))
        atr_value = _atr(highs_v, lows_v, closes_v, min(atr_period, len(closes_v) - 1))

        if ema_value is None or atr_value is None:
            return {
                "center": None,
                "support": None,
                "resistance": None,
                "grid_width": 0.0,
                "range_width_pct": 0.0,
                "range_quality": "invalid",
                "valid": False,
                "invalid_reason": "insufficient_indicator_data",
                "calculation_version": self.calculation_version,
                "data_timestamp": latest_ts.isoformat() if latest_ts else None,
            }

        recent_high = max(highs_window)
        recent_low = min(lows_window)
        rolling_width = max(0.0, recent_high - recent_low)
        atr_width = max(0.0, float(atr_value) * max(0.0, float(atr_multiplier)))
        grid_width = max(rolling_width, atr_width * 2.0)
        if grid_width <= 0:
            return {
                "center": round(float(ema_value), 6),
                "support": None,
                "resistance": None,
                "grid_width": 0.0,
                "range_width_pct": 0.0,
                "range_quality": "invalid",
                "valid": False,
                "invalid_reason": "non_positive_grid_width",
                "calculation_version": self.calculation_version,
                "data_timestamp": latest_ts.isoformat() if latest_ts else None,
            }

        support = float(ema_value) - grid_width / 2.0 + atr_value * max(0.0, float(support_buffer))
        resistance = float(ema_value) + grid_width / 2.0 - atr_value * max(0.0, float(resistance_buffer))
        support = round(max(0.01, support), 6)
        resistance = round(max(0.01, resistance), 6)
        range_width = max(0.0, resistance - support)
        range_width_pct = (range_width / float(ema_value) * 100.0) if ema_value > 0 else 0.0

        invalid_reason = None
        valid = True
        if support >= resistance:
            valid = False
            invalid_reason = "range_too_narrow"
        elif range_width_pct < float(minimum_range_pct):
            valid = False
            invalid_reason = "range_too_narrow"
        elif range_width_pct > float(maximum_range_pct):
            valid = False
            invalid_reason = "range_too_wide"

        result = {
            "center": round(float(ema_value), 6),
            "support": support,
            "resistance": resistance,
            "grid_width": round(range_width, 6),
            "range_width_pct": round(range_width_pct, 6),
            "range_quality": _range_quality(range_width_pct, float(minimum_range_pct), float(maximum_range_pct)) if valid else "invalid",
            "valid": valid,
            "invalid_reason": invalid_reason,
            "calculation_version": self.calculation_version,
            "data_timestamp": latest_ts.isoformat() if latest_ts else None,
        }
        if valid and support <= current_price <= resistance:
            result["entry_ready"] = True
        else:
            result["entry_ready"] = False
        return result
