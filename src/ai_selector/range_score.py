from __future__ import annotations

import math
from statistics import pstdev
from typing import Any, Iterable


def _clamp(value: float, default: float = 50.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if math.isnan(number) or math.isinf(number):
        number = float(default)
    return max(0.0, min(100.0, number))


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _extract_sequence(market_data: dict[str, Any], *keys: str) -> list[float]:
    for key in keys:
        value = market_data.get(key)
        if isinstance(value, (list, tuple)):
            items = [_safe_float(item) for item in value]
            return [float(item) for item in items if item is not None]
    return []


def _extract_price(market_data: dict[str, Any]) -> float | None:
    for key in ("current_price", "price", "last_price", "close", "last_close", "mid_price"):
        price = _safe_float(market_data.get(key))
        if price and price > 0:
            return price
    closes = _extract_sequence(market_data, "close_history", "closes", "prices")
    if closes:
        return closes[-1]
    return None


def _extract_volume(market_data: dict[str, Any]) -> float | None:
    for key in ("avg_10d_volume", "average_10d_volume", "volume_10d", "avg_volume_10d", "volume"):
        volume = _safe_float(market_data.get(key))
        if volume and volume > 0:
            return volume
    return None


def _extract_spread_pct(market_data: dict[str, Any], price: float | None) -> float | None:
    spread_pct = _safe_float(market_data.get("spread_pct"))
    if spread_pct is not None and spread_pct >= 0:
        return spread_pct
    bid = _safe_float(market_data.get("bid"))
    ask = _safe_float(market_data.get("ask"))
    if price and price > 0 and bid and ask and ask >= bid:
        return ((ask - bid) / price) * 100.0
    return None


def _recent_change_pct(market_data: dict[str, Any]) -> float | None:
    change = _safe_float(market_data.get("three_day_change_pct"))
    if change is not None:
        return change
    closes = _extract_sequence(market_data, "close_history", "closes", "prices")
    if len(closes) >= 4 and closes[-4] > 0:
        return ((closes[-1] - closes[-4]) / closes[-4]) * 100.0
    return None


def _extract_support_resistance(market_data: dict[str, Any]) -> tuple[float | None, float | None]:
    support = _safe_float(
        market_data.get("support_price"),
        _safe_float(market_data.get("range_low"), _safe_float(market_data.get("recent_low"))),
    )
    resistance = _safe_float(
        market_data.get("resistance_price"),
        _safe_float(market_data.get("range_high"), _safe_float(market_data.get("recent_high"))),
    )
    if support is not None and support <= 0:
        support = None
    if resistance is not None and resistance <= 0:
        resistance = None
    return support, resistance


def _entry_quality_from_position(position: float) -> tuple[float, str, bool]:
    if position <= 20.0:
        return 95.0, "excellent", True
    if position <= 40.0:
        return 80.0, "good", True
    if position <= 60.0:
        return 55.0, "neutral", False
    if position <= 80.0:
        return 30.0, "poor", False
    return 10.0, "very_poor", False


class RangeFitnessScorer:
    def calculate(self, symbol, market_data) -> dict:
        ticker = str(symbol or "").strip().upper()
        data = dict(market_data or {})

        price = _extract_price(data)
        volume = _extract_volume(data)
        spread_pct = _extract_spread_pct(data, price)
        three_day_change_pct = _recent_change_pct(data)
        closes = _extract_sequence(data, "close_history", "closes", "prices")
        returns = _extract_sequence(data, "returns")
        entry = self._entry_proximity(price, data)

        volatility_score = self._volatility_score(closes, returns, data)
        mean_reversion_score = self._mean_reversion_score(price, closes, data)
        liquidity_score = self._liquidity_score(price, volume, data)
        spread_score = self._spread_score(spread_pct)
        stability_score = self._stability_score(price, closes, returns, three_day_change_pct)

        range_score = (
            0.25 * volatility_score
            + 0.25 * mean_reversion_score
            + 0.20 * liquidity_score
            + 0.15 * spread_score
            + 0.15 * stability_score
        )

        return {
            "ticker": ticker,
            "range_score": round(_clamp(range_score), 2),
            "volatility_score": round(_clamp(volatility_score), 2),
            "mean_reversion_score": round(_clamp(mean_reversion_score), 2),
            "liquidity_score": round(_clamp(liquidity_score), 2),
            "spread_score": round(_clamp(spread_score), 2),
            "stability_score": round(_clamp(stability_score), 2),
            "entry": entry,
        }

    def _entry_proximity(self, price: float | None, market_data: dict[str, Any]) -> dict[str, Any]:
        support, resistance = _extract_support_resistance(market_data)
        if price is None or price <= 0 or support is None or resistance is None or resistance <= support:
            return {
                "entry_proximity_score": 50.0,
                "good_for_entry_now": False,
                "entry_quality": "unknown",
                "entry_reason": "missing_support_resistance_or_price",
                "range_position": None,
                "dist_to_support": None,
                "dist_to_resistance": None,
            }

        range_width = resistance - support
        range_position = 100.0 * max(0.0, min(1.0, (price - support) / range_width))
        dist_to_support = ((price - support) / support) * 100.0 if support > 0 else None
        dist_to_resistance = ((resistance - price) / price) * 100.0 if price > 0 else None
        entry_proximity_score, entry_quality, good_for_entry_now = _entry_quality_from_position(range_position)
        entry_reason = "price_balanced_in_range"

        if dist_to_resistance is not None and dist_to_resistance <= 1.0:
            entry_proximity_score = 10.0
            entry_quality = "very_poor"
            good_for_entry_now = False
            entry_reason = "price_near_resistance_not_suitable_for_new_entry"
        elif dist_to_support is not None and dist_to_support >= 8.0:
            if entry_quality in {"excellent", "good", "neutral"}:
                entry_proximity_score = min(entry_proximity_score, 30.0)
                entry_quality = "poor"
            good_for_entry_now = False
            entry_reason = "price_far_from_support_not_suitable_for_new_entry"
        elif entry_quality in {"excellent", "good"}:
            entry_reason = "price_near_support_good_for_new_entry"
        elif entry_quality == "neutral":
            entry_reason = "mid_range_observation_only"
        elif entry_quality == "poor":
            entry_reason = "price_far_from_support_observation_only"
        elif entry_quality == "very_poor":
            entry_reason = "high_range_position_not_suitable_for_new_entry"

        return {
            "entry_proximity_score": round(_clamp(entry_proximity_score), 2),
            "good_for_entry_now": bool(good_for_entry_now),
            "entry_quality": entry_quality,
            "entry_reason": entry_reason,
            "range_position": round(range_position, 2),
            "dist_to_support": round(dist_to_support, 2) if dist_to_support is not None else None,
            "dist_to_resistance": round(dist_to_resistance, 2) if dist_to_resistance is not None else None,
        }

    def _volatility_score(self, closes: list[float], returns: list[float], market_data: dict[str, Any]) -> float:
        vol_pct = _safe_float(market_data.get("volatility_pct"))
        if vol_pct is None:
            if returns:
                vol_pct = pstdev(returns) * 100.0 if len(returns) >= 2 else 0.0
            elif len(closes) >= 4 and closes[-1] > 0:
                pct_returns = [
                    ((closes[i] - closes[i - 1]) / closes[i - 1]) * 100.0
                    for i in range(1, len(closes))
                    if closes[i - 1] > 0
                ]
                vol_pct = pstdev(pct_returns) if len(pct_returns) >= 2 else 0.0
            else:
                vol_pct = 0.0
        vol_pct = max(0.0, float(vol_pct or 0.0))
        target = 5.0
        score = 100.0 - min(100.0, abs(vol_pct - target) * 9.0)
        return score

    def _mean_reversion_score(self, price: float | None, closes: list[float], market_data: dict[str, Any]) -> float:
        if price is None or price <= 0:
            return 50.0
        low = _safe_float(market_data.get("recent_low"))
        high = _safe_float(market_data.get("recent_high"))
        if low is None and closes:
            low = min(closes[-20:] or closes)
        if high is None and closes:
            high = max(closes[-20:] or closes)
        if low is None or high is None or high <= low:
            return 50.0
        position = (price - low) / (high - low)
        score = 100.0 - min(100.0, abs(position - 0.5) * 200.0)
        return score

    def _liquidity_score(self, price: float | None, volume: float | None, market_data: dict[str, Any]) -> float:
        if price is None or price <= 0 or volume is None or volume <= 0:
            return 50.0
        dollar_volume = price * volume
        if dollar_volume >= 100_000_000:
            return 100.0
        if dollar_volume <= 1_000_000:
            return 30.0
        if dollar_volume <= 10_000_000:
            return 55.0 + (dollar_volume - 1_000_000) / 9_000_000 * 20.0
        if dollar_volume <= 50_000_000:
            return 75.0 + (dollar_volume - 10_000_000) / 40_000_000 * 15.0
        return 90.0 + (dollar_volume - 50_000_000) / 50_000_000 * 10.0

    def _spread_score(self, spread_pct: float | None) -> float:
        if spread_pct is None:
            return 50.0
        if spread_pct <= 0:
            return 100.0
        if spread_pct >= 2.0:
            return 0.0
        return 100.0 - min(100.0, spread_pct * 50.0)

    def _stability_score(
        self,
        price: float | None,
        closes: list[float],
        returns: list[float],
        three_day_change_pct: float | None,
    ) -> float:
        if price is None or price <= 0:
            return 50.0
        change_penalty = abs(float(three_day_change_pct or 0.0)) * 3.5
        vol_penalty = 0.0
        if returns:
            vol_penalty = min(50.0, pstdev(returns) * 120.0 if len(returns) >= 2 else 0.0)
        elif len(closes) >= 5:
            pct_returns = [
                ((closes[i] - closes[i - 1]) / closes[i - 1]) * 100.0
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]
            if len(pct_returns) >= 2:
                vol_penalty = min(50.0, pstdev(pct_returns) * 4.0)
        if closes:
            recent_high = max(closes[-10:] or closes)
            recent_low = min(closes[-10:] or closes)
            range_width = max(recent_high - recent_low, 1e-9)
            location_penalty = abs(((price - recent_low) / range_width) - 0.5) * 40.0
        else:
            location_penalty = 20.0
        score = 100.0 - min(100.0, change_penalty + vol_penalty + location_penalty)
        return score
