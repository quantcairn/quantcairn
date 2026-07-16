from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean
from typing import Any, Iterable

from .strategy_registry import REGIME_NAMES


MISSING_INDICATORS = (
    "vix",
    "breadth",
    "realized_volatility",
    "credit_risk_proxy",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in {float("inf"), float("-inf")}:
        return default
    return number


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _candidate_rows(selection_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    report = selection_report or {}
    raw_rows = list(report.get("top10") or report.get("top3") or report.get("report") or [])
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        payload = dict(row)
        market_data = payload.get("trade_market_data") if isinstance(payload.get("trade_market_data"), dict) else payload.get("market_data")
        if isinstance(market_data, dict):
            payload["market_data"] = dict(market_data)
        rows.append(payload)
    return rows


def _row_data(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row.get("market_data") or row.get("trade_market_data") or row)


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    data = _row_data(row)
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
        if key in data and data.get(key) is not None:
            return data.get(key)
    return None


def _row_text(row: dict[str, Any], *keys: str) -> str:
    return _safe_text(_row_value(row, *keys))


def _mean(values: Iterable[float | None]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return round(mean(numbers), 4)


def _ratio(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round((part / whole) * 100.0, 2)


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, dict)) and not value:
            continue
        return value
    return None


def _flatten_benchmark_changes(row: dict[str, Any]) -> list[float]:
    raw = _row_value(row, "benchmark_change_pct")
    if isinstance(raw, dict):
        return [float(value) for value in raw.values() if isinstance(value, (int, float))]
    if isinstance(raw, (list, tuple)):
        return [float(value) for value in raw if isinstance(value, (int, float))]
    value = _safe_float(raw)
    return [value] if value is not None else []


def _structure_indicator_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    indicators: dict[str, list[float]] = {
        "average_dollar_volume_20d": [],
        "spread_pct": [],
        "gap_pct": [],
        "adx": [],
        "relative_strength_vs_SPY": [],
        "ma20": [],
        "ma50": [],
        "ma200": [],
        "benchmark_change_pct": [],
        "candidate_score": [],
        "liquidity_score": [],
        "trend_score": [],
        "volatility_score": [],
        "risk_score": [],
        "strategy_fit_score": [],
    }
    counts = {
        "valid_rows": 0,
        "fresh_rows": 0,
        "benchmark_valid_rows": 0,
        "trend_aligned_rows": 0,
        "bearish_rows": 0,
        "high_volatility_rows": 0,
    }
    benchmark_symbols: list[str] = []
    selection_stages: list[str] = []
    asset_types: list[str] = []
    sectors: list[str] = []
    missing_indicators = list(MISSING_INDICATORS)
    for row in rows:
        data = _row_data(row)
        asset_type = _safe_text(_first_non_empty(row.get("asset_type"), data.get("asset_type"))).lower()
        if asset_type:
            asset_types.append(asset_type)
        sector = _safe_text(_first_non_empty(row.get("sector"), row.get("industry"), data.get("sector"), data.get("industry")))
        if sector:
            sectors.append(sector)
        selection_stage = _safe_text(_first_non_empty(row.get("selection_stage"), data.get("selection_stage"))).upper()
        if selection_stage:
            selection_stages.append(selection_stage)
        benchmark_status = _safe_text(_first_non_empty(row.get("benchmark_status"), row.get("benchmark_alignment_status"), data.get("benchmark_status"), data.get("benchmark_alignment_status"))).upper()
        if benchmark_status == "VALID":
            counts["benchmark_valid_rows"] += 1
        freshness_status = _safe_text(_first_non_empty(row.get("freshness_status"), data.get("freshness_status"))).upper()
        data_status = _safe_text(_first_non_empty(row.get("data_status"), data.get("data_status"))).upper()
        if data_status == "VALID":
            counts["valid_rows"] += 1
        if freshness_status in {"SAFE", "GOOD"}:
            counts["fresh_rows"] += 1
        benchmark_symbols.extend(
            str(symbol).strip().upper()
            for symbol in (_first_non_empty(row.get("benchmark_symbols"), data.get("benchmark_symbols")) or [])
            if str(symbol).strip()
        )
        close = _safe_float(_first_non_empty(row.get("current_price"), row.get("price"), data.get("current_price"), data.get("price")))
        ma20 = _safe_float(_first_non_empty(row.get("ma20"), data.get("ma20")))
        ma50 = _safe_float(_first_non_empty(row.get("ma50"), data.get("ma50")))
        ma200 = _safe_float(_first_non_empty(row.get("ma200"), data.get("ma200")))
        adx = _safe_float(_first_non_empty(row.get("adx"), data.get("adx")))
        rs = _safe_float(_first_non_empty(row.get("relative_strength_vs_SPY"), row.get("relative_strength_vs_spy"), row.get("relative_strength_60d"), data.get("relative_strength_vs_SPY"), data.get("relative_strength_vs_spy"), data.get("relative_strength_60d")))
        avg_dollar_volume = _safe_float(_first_non_empty(row.get("average_dollar_volume_20d"), data.get("average_dollar_volume_20d")))
        spread = _safe_float(_first_non_empty(row.get("spread_pct"), data.get("spread_pct")))
        gap = _safe_float(_first_non_empty(row.get("gap_pct"), data.get("gap_pct")))
        benchmark_change = _mean(_flatten_benchmark_changes(row))
        candidate_score = _safe_float(_first_non_empty(row.get("candidate_score"), row.get("score"), data.get("candidate_score"), data.get("score")))
        liquidity_score = _safe_float(_first_non_empty(row.get("liquidity_score"), data.get("liquidity_score")))
        trend_score = _safe_float(_first_non_empty(row.get("trend_score"), data.get("trend_score")))
        volatility_score = _safe_float(_first_non_empty(row.get("volatility_score"), data.get("volatility_score")))
        risk_score = _safe_float(_first_non_empty(row.get("risk_score"), data.get("risk_score")))
        strategy_fit_score = _safe_float(_first_non_empty(row.get("strategy_fit_score"), data.get("strategy_fit_score")))

        if close is not None and ma50 is not None and ma200 is not None:
            if close >= ma50 and ma50 >= ma200:
                counts["trend_aligned_rows"] += 1
            if close <= ma50 and ma50 <= ma200:
                counts["bearish_rows"] += 1
        if (
            (spread is not None and spread >= 0.6)
            or (gap is not None and abs(gap) >= 4.0)
            or (adx is not None and adx >= 28.0)
            or (avg_dollar_volume is not None and avg_dollar_volume < 25_000_000)
        ):
            counts["high_volatility_rows"] += 1

        for key, value in (
            ("average_dollar_volume_20d", avg_dollar_volume),
            ("spread_pct", spread),
            ("gap_pct", gap),
            ("adx", adx),
            ("relative_strength_vs_SPY", rs),
            ("ma20", ma20),
            ("ma50", ma50),
            ("ma200", ma200),
            ("benchmark_change_pct", benchmark_change),
            ("candidate_score", candidate_score),
            ("liquidity_score", liquidity_score),
            ("trend_score", trend_score),
            ("volatility_score", volatility_score),
            ("risk_score", risk_score),
            ("strategy_fit_score", strategy_fit_score),
        ):
            if value is not None:
                indicators[key].append(value)

    coverage = {
        "total_rows": total,
        "valid_ratio": _ratio(counts["valid_rows"], total),
        "fresh_ratio": _ratio(counts["fresh_rows"], total),
        "benchmark_valid_ratio": _ratio(counts["benchmark_valid_rows"], total),
        "trend_alignment_ratio": _ratio(counts["trend_aligned_rows"], total),
        "bearish_alignment_ratio": _ratio(counts["bearish_rows"], total),
        "high_volatility_ratio": _ratio(counts["high_volatility_rows"], total),
        "asset_types": sorted(dict.fromkeys(asset_types)),
        "sectors": sorted(dict.fromkeys(sectors)),
        "selection_stages": sorted(dict.fromkeys(selection_stages)),
        "benchmark_symbols": sorted(dict.fromkeys(benchmark_symbols)),
        "missing_indicators": sorted(dict.fromkeys(missing_indicators)),
        "indicators": {key: _mean(values) for key, values in indicators.items()},
    }
    return coverage


def analyze_market_regime(
    selection_report: dict[str, Any] | None,
    *,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    report = dict(selection_report or {})
    rows = _candidate_rows(report)
    coverage = _structure_indicator_coverage(rows)
    total = int(coverage["total_rows"] or 0)
    fallback_used = bool(report.get("fallback_used") or report.get("provider_fallback_used"))
    top_n_missing_count = int(report.get("top_n_missing_count") or 0)
    freshness_states = [
        _safe_text(_first_non_empty(row.get("freshness_status"), _row_data(row).get("freshness_status"))).upper()
        for row in rows
    ]
    benchmark_states = [
        _safe_text(_first_non_empty(row.get("benchmark_status"), row.get("benchmark_alignment_status"), _row_data(row).get("benchmark_status"), _row_data(row).get("benchmark_alignment_status"))).upper()
        for row in rows
    ]
    invalid_rows = sum(1 for state in freshness_states if state == "INVALID")
    invalid_benchmarks = sum(1 for state in benchmark_states if state != "VALID" and state)
    enough_data = total >= 2 and coverage["valid_ratio"] is not None
    valid_ratio = float(coverage["valid_ratio"] or 0.0)
    fresh_ratio = float(coverage["fresh_ratio"] or 0.0)
    benchmark_valid_ratio = float(coverage["benchmark_valid_ratio"] or 0.0)
    trend_ratio = float(coverage["trend_alignment_ratio"] or 0.0)
    bearish_ratio = float(coverage["bearish_alignment_ratio"] or 0.0)
    high_vol_ratio = float(coverage["high_volatility_ratio"] or 0.0)
    avg_rel_strength = float(coverage["indicators"].get("relative_strength_vs_SPY") or 0.0)
    avg_adx = float(coverage["indicators"].get("adx") or 0.0)
    avg_spread = float(coverage["indicators"].get("spread_pct") or 0.0)
    avg_gap = float(coverage["indicators"].get("gap_pct") or 0.0)
    avg_dollar_volume = float(coverage["indicators"].get("average_dollar_volume_20d") or 0.0)
    benchmark_change = float(coverage["indicators"].get("benchmark_change_pct") or 0.0)

    if total == 0:
        regime = "UNKNOWN"
        regime_score = 0.0
        confidence = 0.0
        regime_reasons = ["no_candidate_rows"]
    elif invalid_rows > 0 or invalid_benchmarks > max(1, total // 2):
        regime = "INVALID_RANGE"
        regime_score = 12.5
        confidence = 0.25 if total else 0.0
        regime_reasons = ["freshness_or_benchmark_invalid"]
    elif high_vol_ratio >= 50.0 or avg_spread >= 0.75 or avg_gap >= 4.0 or avg_adx >= 28.0:
        regime = "HIGH_VOLATILITY"
        regime_score = 78.0
        confidence = 0.72 if enough_data else 0.55
        regime_reasons = ["high_volatility_profile"]
    elif trend_ratio >= 60.0 and avg_rel_strength >= 5.0 and avg_adx >= 18.0 and benchmark_change >= 0.0:
        regime = "STRONG_UPTREND"
        regime_score = 86.0
        confidence = 0.82 if enough_data else 0.68
        regime_reasons = ["trend_alignment_bullish"]
    elif bearish_ratio >= 60.0 and avg_rel_strength <= -5.0 and avg_adx >= 18.0 and benchmark_change <= 0.0:
        regime = "STRONG_DOWNTREND"
        regime_score = 84.0
        confidence = 0.80 if enough_data else 0.66
        regime_reasons = ["trend_alignment_bearish"]
    elif valid_ratio >= 50.0 and fresh_ratio >= 50.0 and benchmark_valid_ratio >= 50.0:
        regime = "RANGE"
        regime_score = 68.0
        confidence = 0.74 if enough_data else 0.60
        regime_reasons = ["balanced_range_conditions"]
    else:
        regime = "UNKNOWN"
        regime_score = 30.0
        confidence = 0.40 if enough_data else 0.20
        regime_reasons = ["insufficient_consensus"]

    if fallback_used:
        regime_reasons.append("fallback_used")
        confidence = max(0.0, round(confidence - 0.10, 4))
    if top_n_missing_count > 0:
        regime_reasons.append("top_n_missing")
        confidence = max(0.0, round(confidence - min(0.15, top_n_missing_count * 0.03), 4))

    selection_stage = _safe_text(report.get("selection_stage") or report.get("settings", {}).get("selection_stage") or "FINALIZED").upper()
    actionability = regime not in {"UNKNOWN", "INVALID_RANGE"} and confidence >= 0.45 and valid_ratio >= 40.0

    benchmark_symbols = list(coverage["benchmark_symbols"])
    if not benchmark_symbols:
        fallback_benchmarks = []
        for row in rows:
            fallback_benchmarks.extend(
                str(symbol).strip().upper()
                for symbol in (_first_non_empty(row.get("benchmark_symbols"), _row_data(row).get("benchmark_symbols")) or [])
                if str(symbol).strip()
            )
        benchmark_symbols = list(dict.fromkeys(fallback_benchmarks))

    indicators = dict(coverage["indicators"])
    for key in MISSING_INDICATORS:
        indicators.setdefault(key, None)

    if now_et is None:
        now_et = datetime.now(timezone.utc)

    return {
        "title": "Market Regime",
        "generated_at": _utc_now_iso(),
        "selection_stage": selection_stage,
        "selection_date": _safe_text(report.get("selection_date") or report.get("generated_at") or ""),
        "regime": regime,
        "regime_score": regime_score,
        "confidence": round(confidence, 4),
        "actionable": bool(actionability),
        "selection_outcome": "NO_ACTIONABLE_RESEARCH_CANDIDATE" if not actionability else "ACTIONABLE_RESEARCH_CANDIDATE",
        "regime_reasons": list(dict.fromkeys(regime_reasons)),
        "indicators": indicators,
        "coverage": {
            "total_rows": total,
            "valid_ratio": coverage["valid_ratio"],
            "fresh_ratio": coverage["fresh_ratio"],
            "benchmark_valid_ratio": coverage["benchmark_valid_ratio"],
            "trend_alignment_ratio": coverage["trend_alignment_ratio"],
            "bearish_alignment_ratio": coverage["bearish_alignment_ratio"],
            "high_volatility_ratio": coverage["high_volatility_ratio"],
            "asset_types": list(coverage["asset_types"]),
            "sectors": list(coverage["sectors"]),
            "benchmark_symbols": benchmark_symbols,
            "selection_stages": list(coverage["selection_stages"]),
        },
        "signals": [
            {
                "symbol": _safe_text(row.get("symbol") or row.get("ticker")),
                "strategy_family": _safe_text(_first_non_empty(row.get("strategy_family"), _row_data(row).get("strategy_family"))),
                "selection_stage": _safe_text(_first_non_empty(row.get("selection_stage"), _row_data(row).get("selection_stage"))).upper() or "UNKNOWN",
                "benchmark_status": _safe_text(_first_non_empty(row.get("benchmark_status"), row.get("benchmark_alignment_status"), _row_data(row).get("benchmark_status"), _row_data(row).get("benchmark_alignment_status"))).upper() or "UNKNOWN",
                "data_status": _safe_text(_first_non_empty(row.get("data_status"), _row_data(row).get("data_status"))).upper() or "UNKNOWN",
                "freshness_status": _safe_text(_first_non_empty(row.get("freshness_status"), _row_data(row).get("freshness_status"))).upper() or "UNKNOWN",
                "trend_score": _safe_float(_first_non_empty(row.get("trend_score"), _row_data(row).get("trend_score"))),
                "volatility_score": _safe_float(_first_non_empty(row.get("volatility_score"), _row_data(row).get("volatility_score"))),
                "candidate_score": _safe_float(_first_non_empty(row.get("candidate_score"), row.get("score"), _row_data(row).get("candidate_score"), _row_data(row).get("score"))),
            }
            for row in rows[:10]
        ],
        "now_et": now_et.isoformat(),
        "missing_indicators": [key for key, value in indicators.items() if value is None and key in MISSING_INDICATORS],
    }
