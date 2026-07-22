from __future__ import annotations

from typing import Any, Iterable


WEIGHT_LIQUIDITY = 0.30
WEIGHT_TREND = 0.25
WEIGHT_VOLATILITY = 0.20
WEIGHT_RISK = 0.15
WEIGHT_STRATEGY_FIT = 0.10

SUPPORTED_STRATEGY_FAMILIES = {
    "benchmark",
    "index_follow",
    "inverse_range",
    "leveraged_range",
    "mean_reversion",
    "range_etf",
    "trend_following",
}


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN check without importing math
        return default
    if number == float("inf") or number == float("-inf"):
        return default
    return number


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 50.0
    if number != number:
        number = 50.0
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_asset_type(value: Any) -> str:
    asset = _normalize_text(value).lower()
    if asset in {"common_stock", "index_etf", "leveraged_etf", "inverse_etf"}:
        return asset
    return ""


def _first_present(candidate: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in candidate and candidate.get(key) is not None:
            return candidate.get(key)
    return None


def _nested(candidate: dict[str, Any], key: str) -> dict[str, Any]:
    value = candidate.get(key)
    return value if isinstance(value, dict) else {}


def _market_data(candidate: dict[str, Any]) -> dict[str, Any]:
    return _nested(candidate, "trade_market_data") or _nested(candidate, "market_data")


def _raw_score(candidate: dict[str, Any]) -> float:
    for key in ("candidate_score", "final_score", "score", "base_score", "ai_score"):
        value = _safe_float(candidate.get(key))
        if value is not None:
            return _clamp(value)
    return 50.0


def _asset_family(asset_type: str) -> str:
    if asset_type in {"inverse_etf"}:
        return "inverse"
    if asset_type in {"leveraged_etf"}:
        return "leveraged"
    if asset_type in {"index_etf"}:
        return "index"
    return "common"


def _normalize_strategy_family(value: Any) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    return text


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _normalize_text(value).lower()
    return text in {"1", "true", "yes", "y", "on"}


def _liquidity_score(candidate: dict[str, Any]) -> tuple[float, list[str]]:
    source_reasons: list[str] = []
    market = _market_data(candidate)
    price = _safe_float(
        _first_present(
            candidate,
            (
                "current_price",
                "price",
                "last_price",
                "last_close",
                "close",
                "premarket_last_price",
            ),
        )
    )
    if price is None:
        price = _safe_float(_first_present(market, ("current_price", "price", "last_price", "close")))
    explicit_dollar_volume = _safe_float(candidate.get("average_dollar_volume_20d"))
    raw_volume = _safe_float(_first_present(candidate, ("avg_10d_volume", "avg_daily_volume_hint", "premarket_volume", "volume")))
    if raw_volume is None:
        raw_volume = _safe_float(_first_present(market, ("avg_10d_volume", "avg_daily_volume_hint", "premarket_volume", "volume")))
    if explicit_dollar_volume is None and price is not None and raw_volume is not None and raw_volume > 0:
        explicit_dollar_volume = price * raw_volume

    if explicit_dollar_volume is None:
        fallback = _safe_float(candidate.get("liquidity_score"))
        if fallback is None:
            fallback = _safe_float(candidate.get("volume_score"))
        if fallback is None:
            return 50.0, ["liquidity:unavailable"]
        return _clamp(fallback), ["liquidity:fallback"]

    dollar_volume = float(explicit_dollar_volume)
    if dollar_volume >= 500_000_000:
        score = 100.0
        source_reasons.append("dollar_volume>500m")
    elif dollar_volume >= 100_000_000:
        score = 85.0
        source_reasons.append("dollar_volume>100m")
    elif dollar_volume >= 50_000_000:
        score = 75.0
        source_reasons.append("dollar_volume>50m")
    elif dollar_volume >= 20_000_000:
        score = 62.0
        source_reasons.append("dollar_volume>20m")
    elif dollar_volume >= 10_000_000:
        score = 54.0
        source_reasons.append("dollar_volume>10m")
    elif dollar_volume >= 5_000_000:
        score = 45.0
        source_reasons.append("dollar_volume>5m")
    else:
        score = 30.0
        source_reasons.append("dollar_volume_low")

    spread_pct = _safe_float(
        _first_present(
            candidate,
            (
                "spread_pct",
                "spread_pct_live",
                "spread",
            ),
        )
    )
    if spread_pct is None:
        spread_pct = _safe_float(_first_present(market, ("spread_pct",)))
    if spread_pct is not None and spread_pct >= 0:
        if spread_pct < 0.05:
            score += 10.0
            source_reasons.append("spread<0.05pct")
        elif spread_pct < 0.2:
            score += 5.0
            source_reasons.append("spread<0.2pct")
        elif spread_pct < 0.5:
            score += 2.0
            source_reasons.append("spread<0.5pct")
        else:
            score -= 10.0
            source_reasons.append("spread_wide")

    return _clamp(score), source_reasons


def _trend_score(candidate: dict[str, Any]) -> tuple[float, list[str]]:
    source_reasons: list[str] = []
    market = _market_data(candidate)
    close = _safe_float(_first_present(candidate, ("current_price", "price", "last_price", "close", "last_close")))
    if close is None:
        close = _safe_float(_first_present(market, ("current_price", "price", "last_price", "close", "last_close")))

    ma20 = _safe_float(_first_present(candidate, ("MA20", "ma20", "sma20", "ema20")))
    ma50 = _safe_float(_first_present(candidate, ("MA50", "ma50", "sma50", "ema50")))
    ma200 = _safe_float(_first_present(candidate, ("MA200", "ma200", "sma200", "ema200")))
    if ma20 is None:
        ma20 = _safe_float(_first_present(market, ("MA20", "ma20", "sma20", "ema20")))
    if ma50 is None:
        ma50 = _safe_float(_first_present(market, ("MA50", "ma50", "sma50", "ema50")))
    if ma200 is None:
        ma200 = _safe_float(_first_present(market, ("MA200", "ma200", "sma200", "ema200")))

    adx = _safe_float(_first_present(candidate, ("ADX", "adx", "adx_14")))
    if adx is None:
        adx = _safe_float(_first_present(market, ("ADX", "adx", "adx_14")))
    relative_strength = _safe_float(
        _first_present(
            candidate,
            (
                "relative_strength_vs_SPY",
                "relative_strength_vs_spy",
                "relative_strength_60d",
                "relative_strength",
            ),
        )
    )
    if relative_strength is None:
        relative_strength = _safe_float(
            _first_present(
                market,
                (
                    "relative_strength_vs_SPY",
                    "relative_strength_vs_spy",
                    "relative_strength_60d",
                    "relative_strength",
                ),
            )
        )

    fallback = _safe_float(candidate.get("trend_score"))
    if fallback is None:
        fallback = _safe_float(candidate.get("trend_fit_score"))
    if fallback is None:
        fallback = _safe_float(candidate.get("score"))

    if close is None and ma20 is None and ma50 is None and ma200 is None and adx is None and relative_strength is None:
        if fallback is not None:
            return _clamp(fallback), ["trend:fallback"]
        return 50.0, ["trend:unavailable"]

    score = 50.0
    if close is not None and ma50 is not None and close > ma50:
        score += 10.0
        source_reasons.append("close>ma50")
    if ma20 is not None and ma50 is not None and ma20 > ma50:
        score += 5.0
        source_reasons.append("ma20>ma50")
    if ma50 is not None and ma200 is not None and ma50 > ma200:
        score += 10.0
        source_reasons.append("ma50>ma200")
    if relative_strength is not None:
        if relative_strength > 10.0:
            score += 20.0
            source_reasons.append("relative_strength>10pct")
        elif relative_strength > 5.0:
            score += 10.0
            source_reasons.append("relative_strength>5pct")
        elif relative_strength < -5.0:
            score -= 10.0
            source_reasons.append("relative_strength< -5pct")
    if adx is not None:
        if adx >= 25.0:
            score += 10.0
            source_reasons.append("adx>=25")
        elif adx >= 20.0:
            score += 5.0
            source_reasons.append("adx>=20")
        elif adx < 15.0:
            score -= 5.0
            source_reasons.append("adx<15")

    if score == 50.0 and fallback is not None:
        score = float(fallback)
        source_reasons.append("trend:fallback")
    return _clamp(score), source_reasons


def _volatility_score(candidate: dict[str, Any]) -> tuple[float, list[str]]:
    source_reasons: list[str] = []
    market = _market_data(candidate)
    asset_type = _normalize_asset_type(candidate.get("asset_type") or market.get("asset_type"))
    atr_pct = _safe_float(
        _first_present(
            candidate,
            (
                "ATR_20_percentage",
                "atr_20_percentage",
                "atr_pct",
                "atr_20_pct",
            ),
        )
    )
    if atr_pct is None:
        atr_pct = _safe_float(_first_present(market, ("ATR_20_percentage", "atr_20_percentage", "atr_pct", "atr_20_pct")))

    fallback = _safe_float(candidate.get("volatility_score"))
    if fallback is None:
        fallback = _safe_float(candidate.get("range_score"))

    if atr_pct is None:
        if fallback is not None:
            return _clamp(fallback), ["volatility:fallback"]
        return 50.0, ["volatility:unavailable"]

    score = 50.0
    if asset_type == "common_stock":
        if 1.0 <= atr_pct <= 3.0:
            score = 90.0
            source_reasons.append("common_stock_1_to_3pct")
        elif 3.0 < atr_pct <= 6.0:
            score = 75.0
            source_reasons.append("common_stock_3_to_6pct")
        elif atr_pct < 1.0:
            score = 55.0
            source_reasons.append("common_stock_too_calm")
        elif atr_pct <= 8.0:
            score = 70.0
            source_reasons.append("common_stock_6_to_8pct")
        elif atr_pct > 10.0:
            score = 30.0
            source_reasons.append("common_stock_too_volatile")
        else:
            score = 45.0
            source_reasons.append("common_stock_mid_range")
    elif asset_type == "index_etf":
        if 0.5 <= atr_pct <= 6.0:
            score = 85.0
            source_reasons.append("etf_in_preferred_band")
        elif atr_pct < 0.5:
            score = 45.0
            source_reasons.append("etf_too_calm")
        elif atr_pct <= 10.0:
            score = 65.0
            source_reasons.append("etf_moderate_volatility")
        else:
            score = 35.0
            source_reasons.append("etf_too_volatile")
    elif asset_type in {"leveraged_etf", "inverse_etf"}:
        if 1.0 <= atr_pct <= 10.0:
            score = 85.0
            source_reasons.append("leveraged_or_inverse_preferred_band")
        elif atr_pct < 1.0:
            score = 45.0
            source_reasons.append("leveraged_or_inverse_too_calm")
        else:
            score = 35.0
            source_reasons.append("leveraged_or_inverse_too_volatile")
    else:
        if atr_pct >= 1.0:
            score = 75.0
            source_reasons.append("generic_volatility_ok")
        else:
            score = 50.0
            source_reasons.append("generic_volatility_low")

    return _clamp(score), source_reasons


def _risk_score(candidate: dict[str, Any]) -> tuple[float, list[str]]:
    source_reasons: list[str] = []
    market = _market_data(candidate)
    fallback = _safe_float(candidate.get("risk_score"))
    if fallback is None:
        fallback = _safe_float(candidate.get("drawdown_safety_score"))

    score = 100.0
    penalty_applied = False
    if _as_bool(_first_present(candidate, ("earnings_within_7_days", "earnings_soon")) or _first_present(market, ("earnings_within_7_days", "earnings_soon"))):
        score -= 25.0
        penalty_applied = True
        source_reasons.append("earnings_within_7_days")
    gap_pct = _safe_float(_first_present(candidate, ("gap_pct", "premarket_change_pct", "premarket_change_pct_from_previous_close")))
    if gap_pct is None:
        gap_pct = _safe_float(_first_present(market, ("gap_pct", "premarket_change_pct", "premarket_change_pct_from_previous_close")))
    if gap_pct is not None and abs(gap_pct) >= 5.0:
        score -= 20.0
        penalty_applied = True
        source_reasons.append("large_gap")
    spread_pct = _safe_float(_first_present(candidate, ("spread_pct", "spread_pct_live", "spread")))
    if spread_pct is None:
        spread_pct = _safe_float(_first_present(market, ("spread_pct",)))
    if spread_pct is not None and spread_pct >= 0.5:
        score -= 15.0
        penalty_applied = True
        source_reasons.append("spread_widening")
    abnormal_volume = _as_bool(_first_present(candidate, ("abnormal_volume", "abnormal_volume_flag")) or _first_present(market, ("abnormal_volume", "abnormal_volume_flag")))
    volume_spike = _safe_float(_first_present(candidate, ("volume_spike",)))
    if volume_spike is None:
        volume_spike = _safe_float(_first_present(market, ("volume_spike",)))
    if abnormal_volume or (volume_spike is not None and (volume_spike >= 3.0 or volume_spike <= 0.25)):
        score -= 10.0
        penalty_applied = True
        source_reasons.append("abnormal_volume")
    benchmark_status = _normalize_text(
        _first_present(
            candidate,
            ("benchmark_status", "benchmark_alignment_status"),
        )
        or _first_present(market, ("benchmark_status", "benchmark_alignment_status"))
    ).upper()
    if benchmark_status and benchmark_status != "VALID":
        score -= 20.0
        penalty_applied = True
        source_reasons.append("benchmark_invalid")

    if not penalty_applied and fallback is not None:
        return _clamp(fallback), ["risk:fallback"]

    return _clamp(score), source_reasons or ["risk:clean"]


def _recommended_strategy(candidate: dict[str, Any], trend_score: float) -> tuple[str, list[str]]:
    asset_type = _normalize_asset_type(candidate.get("asset_type"))
    strategy_family = _normalize_strategy_family(candidate.get("strategy_family"))
    reasons: list[str] = []

    if asset_type == "inverse_etf":
        strategy = "inverse_range"
        reasons.append("inverse_etf")
    elif asset_type == "leveraged_etf":
        strategy = "leveraged_range"
        reasons.append("leveraged_etf")
    elif asset_type == "index_etf":
        strategy = "index_follow" if trend_score >= 65.0 else "mean_reversion"
        reasons.append("index_etf")
    else:
        if trend_score >= 70.0:
            strategy = "trend_following"
            reasons.append("strong_trend")
        else:
            strategy = "mean_reversion"
            reasons.append("range_or_mean_reversion")

    if strategy_family and strategy_family == strategy:
        reasons.append("strategy_family_match")
    elif strategy_family and strategy_family in SUPPORTED_STRATEGY_FAMILIES:
        reasons.append(f"strategy_family={strategy_family}")
    elif strategy_family:
        reasons.append(f"strategy_family={strategy_family}")

    return strategy, reasons


def score_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    item = dict(candidate or {})
    score_is_formal = item.get("formal_scoring_eligibility", item.get("scoring_eligible", True)) is not False
    base_score = _safe_float(_first_present(item, ("score", "final_score", "candidate_score", "base_score", "ai_score")), 50.0) or 50.0

    liquidity_score, liquidity_reasons = _liquidity_score(item)
    trend_score, trend_reasons = _trend_score(item)
    volatility_score, volatility_reasons = _volatility_score(item)
    risk_score, risk_reasons = _risk_score(item)
    recommended_strategy, strategy_reasons = _recommended_strategy(item, trend_score)

    strategy_fit_score = 100.0
    asset_type = _normalize_asset_type(item.get("asset_type"))
    if asset_type == "inverse_etf" and recommended_strategy != "inverse_range":
        strategy_fit_score = 60.0
    elif asset_type == "leveraged_etf" and recommended_strategy != "leveraged_range":
        strategy_fit_score = 60.0
    elif asset_type in {"common_stock", "index_etf"}:
        if recommended_strategy in {"trend_following", "mean_reversion", "index_follow"}:
            strategy_fit_score = 92.0
        else:
            strategy_fit_score = 72.0
    if item.get("strategy_family") and _normalize_strategy_family(item.get("strategy_family")) == recommended_strategy:
        strategy_fit_score = min(100.0, strategy_fit_score + 5.0)

    candidate_score = (
        WEIGHT_LIQUIDITY * liquidity_score
        + WEIGHT_TREND * trend_score
        + WEIGHT_VOLATILITY * volatility_score
        + WEIGHT_RISK * risk_score
        + WEIGHT_STRATEGY_FIT * strategy_fit_score
    )

    if (
        "average_dollar_volume_20d" not in item
        and "avg_10d_volume" not in item
        and "ma20" not in item
        and "MA20" not in item
        and "atr_20_percentage" not in item
        and "ATR_20_percentage" not in item
        and "relative_strength_vs_SPY" not in item
        and "relative_strength_vs_spy" not in item
        and "trade_market_data" not in item
        and "market_data" not in item
        and "close_history" not in item
    ):
        candidate_score = base_score
        score_reason = "fallback_to_existing_score"
    else:
        score_reason = "; ".join(
            [
                f"liquidity:{'|'.join(liquidity_reasons)}",
                f"trend:{'|'.join(trend_reasons)}",
                f"volatility:{'|'.join(volatility_reasons)}",
                f"risk:{'|'.join(risk_reasons)}",
                f"strategy_fit:{'|'.join(strategy_reasons)}",
            ]
        )

    range_score = _safe_float(_first_present(item, ("range_score", "range_fit_score", "range_score_raw")))
    if range_score is not None:
        candidate_score = candidate_score * 0.7 + range_score * 0.3
        if score_reason == "fallback_to_existing_score":
            score_reason = "fallback_to_existing_score; range_score_blend"
        else:
            score_reason = f"{score_reason}; range_score_blend"

    item["base_score"] = round(float(base_score), 2)
    item["liquidity_score"] = round(float(liquidity_score), 2)
    item["trend_score"] = round(float(trend_score), 2)
    item["volatility_score"] = round(float(volatility_score), 2)
    item["risk_score"] = round(float(risk_score), 2)
    item["strategy_fit_score"] = round(float(strategy_fit_score), 2)
    item["recommended_strategy"] = recommended_strategy
    item["score_reason"] = score_reason
    scored_value = round(_clamp(candidate_score), 2)
    if score_is_formal:
        item["score_type"] = "FORMAL"
        item["score_is_formal"] = True
        item["formal_candidate_score"] = scored_value
        item["candidate_score"] = scored_value
        item["score"] = scored_value
        item["final_score"] = scored_value
    else:
        item["score_type"] = "DIAGNOSTIC"
        item["score_is_formal"] = False
        item["formal_candidate_score"] = None
        item["candidate_score"] = None
        item["score"] = None
        item["final_score"] = None
        item["diagnostic_score"] = scored_value
        item["diagnostic_factor_scores"] = {
            "liquidity": item["liquidity_score"],
            "trend": item["trend_score"],
            "volatility": item["volatility_score"],
            "risk": item["risk_score"],
            "strategy_fit": item["strategy_fit_score"],
        }
        item["diagnostic_score_reason"] = score_reason
    return item


def score_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [score_candidate(candidate) for candidate in candidates]
