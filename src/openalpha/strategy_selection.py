from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import sqrt
from statistics import mean
from typing import Any, Iterable

from .market_regime import analyze_market_regime
from .strategy_registry import STRATEGY_REGISTRY, StrategyRule, normalize_strategy_id, strategy_rule_for, strategy_registry_snapshot


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


def _candidate_rows(selection_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    report = dict(selection_report or {})
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


def _bucket_label(score: float | None) -> str:
    value = _safe_float(score)
    if value is None:
        return "unbucketed"
    if value >= 90:
        return "90-100"
    if value >= 80:
        return "80-90"
    if value >= 70:
        return "70-80"
    if value >= 60:
        return "60-70"
    return "under-60"


def _calibration_probability(score: float | None, calibration_curve: list[dict[str, Any]] | None) -> float | None:
    value = _safe_float(score)
    if value is None:
        return None
    curve = [
        {
            "lower": _safe_float(item.get("lower_bound") or item.get("lower")),
            "upper": _safe_float(item.get("upper_bound") or item.get("upper")),
            "probability": _safe_float(item.get("probability")),
        }
        for item in (calibration_curve or [])
        if isinstance(item, dict)
    ]
    curve = [item for item in curve if item["lower"] is not None and item["upper"] is not None and item["probability"] is not None]
    if not curve:
        return max(0.0, min(1.0, value / 100.0))
    ordered = sorted(curve, key=lambda item: float(item["lower"]))
    for bucket in ordered:
        lower = float(bucket["lower"])
        upper = float(bucket["upper"])
        if lower <= value <= upper:
            return max(0.0, min(1.0, float(bucket["probability"])))
    if value < float(ordered[0]["lower"]):
        return max(0.0, min(1.0, float(ordered[0]["probability"])))
    return max(0.0, min(1.0, float(ordered[-1]["probability"])))


def _dataset_samples_summary(samples: Iterable[Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for sample in samples or []:
        strategy_id = normalize_strategy_id(
            getattr(sample, "recommended_strategy", None)
            or getattr(sample, "strategy_family", None)
            or _safe_text(getattr(sample, "metadata", {}).get("strategy_family") if isinstance(getattr(sample, "metadata", None), dict) else "")
        )
        if not strategy_id:
            continue
        groups[strategy_id].append(sample)

    summary: dict[str, dict[str, Any]] = {}
    for strategy_id, bucket in groups.items():
        candidate_scores = [_safe_float(getattr(item, "candidate_score", None)) for item in bucket]
        forward_returns = [_safe_float(getattr(item, "outcome_return", None)) for item in bucket]
        drawdowns = [_safe_float(getattr(item, "outcome_max_drawdown", None)) for item in bucket]
        backtest_passes = [1.0 if bool(getattr(item, "label_backtest_pass", False)) else 0.0 for item in bucket if getattr(item, "label_backtest_pass", None) is not None]
        walk_forward_passes = [1.0 if bool(getattr(item, "label_walk_forward_pass", False)) else 0.0 for item in bucket if getattr(item, "label_walk_forward_pass", None) is not None]
        positive_returns = [1.0 if (_safe_float(getattr(item, "outcome_return", None)) or 0.0) > 0 else 0.0 for item in bucket if getattr(item, "outcome_return", None) is not None]
        data_valid = [1.0 if bool(getattr(item, "label_data_valid", False)) else 0.0 for item in bucket if getattr(item, "label_data_valid", None) is not None]
        summary[strategy_id] = {
            "strategy_id": strategy_id,
            "sample_count": len(bucket),
            "average_candidate_score": round(mean([score for score in candidate_scores if score is not None]), 4) if any(score is not None for score in candidate_scores) else None,
            "average_forward_return": round(mean([value for value in forward_returns if value is not None]), 4) if any(value is not None for value in forward_returns) else None,
            "average_max_drawdown": round(mean([value for value in drawdowns if value is not None]), 4) if any(value is not None for value in drawdowns) else None,
            "positive_return_rate": round(mean(positive_returns), 4) if positive_returns else None,
            "backtest_pass_rate": round(mean(backtest_passes), 4) if backtest_passes else None,
            "walk_forward_pass_rate": round(mean(walk_forward_passes), 4) if walk_forward_passes else None,
            "data_valid_rate": round(mean(data_valid), 4) if data_valid else None,
        }
    return summary


def _regime_alignment_score(strategy: str, regime: str) -> tuple[float, list[str]]:
    strategy = normalize_strategy_id(strategy)
    regime = _safe_text(regime).upper()
    reasons: list[str] = []
    if not strategy:
        return 0.0, ["strategy_unknown"]
    if strategy == "trend_following":
        if regime == "STRONG_UPTREND":
            return 100.0, ["trend_following_in_uptrend"]
        if regime == "RANGE":
            return 72.0, ["trend_following_in_range"]
        if regime == "HIGH_VOLATILITY":
            return 35.0, ["trend_following_blocked_by_volatility"]
        return 45.0, [f"trend_following_regime_{regime.lower()}"]
    if strategy == "mean_reversion":
        if regime == "RANGE":
            return 100.0, ["mean_reversion_in_range"]
        if regime == "HIGH_VOLATILITY":
            return 82.0, ["mean_reversion_in_high_volatility"]
        if regime in {"STRONG_UPTREND", "STRONG_DOWNTREND"}:
            return 30.0, [f"mean_reversion_blocked_by_{regime.lower()}"]
        return 55.0, [f"mean_reversion_regime_{regime.lower()}"]
    if strategy == "index_follow":
        if regime == "STRONG_UPTREND":
            return 100.0, ["index_follow_with_uptrend"]
        if regime == "RANGE":
            return 78.0, ["index_follow_in_range"]
        if regime == "STRONG_DOWNTREND":
            return 40.0, ["index_follow_blocked_by_downtrend"]
        return 52.0, [f"index_follow_regime_{regime.lower()}"]
    if strategy == "inverse_range":
        if regime == "STRONG_UPTREND":
            return 100.0, ["inverse_range_benefits_from_uptrend"]
        if regime == "HIGH_VOLATILITY":
            return 90.0, ["inverse_range_in_high_volatility"]
        if regime == "RANGE":
            return 68.0, ["inverse_range_in_range"]
        return 45.0, [f"inverse_range_regime_{regime.lower()}"]
    if strategy == "leveraged_range":
        if regime == "HIGH_VOLATILITY":
            return 100.0, ["leveraged_range_in_high_volatility"]
        if regime == "RANGE":
            return 84.0, ["leveraged_range_in_range"]
        if regime == "STRONG_DOWNTREND":
            return 35.0, ["leveraged_range_blocked_by_downtrend"]
        return 50.0, [f"leveraged_range_regime_{regime.lower()}"]
    if strategy == "range_etf":
        if regime == "RANGE":
            return 100.0, ["range_etf_in_range"]
        if regime == "HIGH_VOLATILITY":
            return 78.0, ["range_etf_in_high_volatility"]
        return 56.0, [f"range_etf_regime_{regime.lower()}"]
    if strategy == "benchmark":
        return 60.0, [f"benchmark_regime_{regime.lower()}"]
    return 35.0, [f"unsupported_strategy_family:{strategy}"]


def _candidate_asset_type(row: dict[str, Any]) -> str:
    value = _safe_text(_first_non_empty(row.get("asset_type"), _row_data(row).get("asset_type"))).lower()
    if value in {"common_stock", "index_etf", "leveraged_etf", "inverse_etf"}:
        return value
    return "common_stock"


def _default_strategy_for_asset(asset_type: str, trend_score: float | None) -> str:
    asset_type = _safe_text(asset_type).lower()
    trend_score = _safe_float(trend_score, 50.0) or 50.0
    if asset_type == "inverse_etf":
        return "inverse_range"
    if asset_type == "leveraged_etf":
        return "leveraged_range"
    if asset_type == "index_etf":
        return "index_follow" if trend_score >= 65.0 else "range_etf"
    if trend_score >= 70.0:
        return "trend_following"
    return "mean_reversion"


def _liquidity_quality(row: dict[str, Any]) -> tuple[float, list[str]]:
    data = _row_data(row)
    reasons: list[str] = []
    score = _safe_float(_first_non_empty(row.get("liquidity_score"), data.get("liquidity_score")), 50.0) or 50.0
    volume = _safe_float(_first_non_empty(row.get("average_dollar_volume_20d"), data.get("average_dollar_volume_20d")))
    spread = _safe_float(_first_non_empty(row.get("spread_pct"), data.get("spread_pct")))
    if volume is not None:
        if volume >= 500_000_000:
            score = max(score, 100.0)
            reasons.append("liquidity>500m")
        elif volume >= 100_000_000:
            score = max(score, 88.0)
            reasons.append("liquidity>100m")
        elif volume >= 50_000_000:
            score = max(score, 80.0)
            reasons.append("liquidity>50m")
        elif volume >= 20_000_000:
            score = max(score, 70.0)
            reasons.append("liquidity>20m")
        elif volume < 10_000_000:
            score = min(score, 45.0)
            reasons.append("liquidity_low")
    if spread is not None:
        if spread < 0.05:
            score += 8.0
            reasons.append("tight_spread")
        elif spread < 0.2:
            score += 4.0
            reasons.append("normal_spread")
        elif spread > 0.5:
            score -= 12.0
            reasons.append("spread_wide")
    return max(0.0, min(100.0, round(score, 2))), reasons or ["liquidity_ok"]


def _data_quality_score(row: dict[str, Any]) -> tuple[float, list[str]]:
    data = _row_data(row)
    score = 100.0
    reasons: list[str] = []
    data_status = _safe_text(_first_non_empty(row.get("data_status"), data.get("data_status"))).upper()
    freshness_status = _safe_text(_first_non_empty(row.get("freshness_status"), data.get("freshness_status"))).upper()
    benchmark_status = _safe_text(_first_non_empty(row.get("benchmark_status"), row.get("benchmark_alignment_status"), data.get("benchmark_status"), data.get("benchmark_alignment_status"))).upper()
    selection_stage = _safe_text(_first_non_empty(row.get("selection_stage"), data.get("selection_stage"))).upper()
    if data_status != "VALID":
        score -= 25.0
        reasons.append("data_invalid")
    if freshness_status == "STALE":
        score -= 15.0
        reasons.append("freshness_stale")
    elif freshness_status == "INVALID":
        score -= 30.0
        reasons.append("freshness_invalid")
    if benchmark_status != "VALID":
        score -= 20.0
        reasons.append("benchmark_invalid")
    if selection_stage in {"PRELIMINARY", "STALE", "INVALID"}:
        score -= 12.0
        reasons.append(f"selection_stage_{selection_stage.lower()}")
    if bool(_first_non_empty(row.get("candidate_fallback"), data.get("candidate_fallback"))):
        score -= 12.0
        reasons.append("fallback_used")
    if bool(_first_non_empty(row.get("mock_used"), data.get("mock_used"))):
        score -= 8.0
        reasons.append("mock_used")
    if not bool(_first_non_empty(row.get("scoring_eligible"), data.get("scoring_eligible"))):
        score -= 10.0
        reasons.append("not_scoring_eligible")
    return max(0.0, min(100.0, round(score, 2))), reasons or ["data_quality_ok"]


def _history_score(strategy_id: str, history: dict[str, dict[str, Any]]) -> tuple[float, dict[str, Any], list[str]]:
    summary = dict(history.get(strategy_id) or {})
    reasons: list[str] = []
    if not summary:
        return 35.0, {"sample_count": 0}, ["history_unavailable"]
    sample_count = int(summary.get("sample_count") or 0)
    positive_rate = _safe_float(summary.get("positive_return_rate"), 0.0) or 0.0
    walk_rate = _safe_float(summary.get("walk_forward_pass_rate"), 0.0) or 0.0
    backtest_rate = _safe_float(summary.get("backtest_pass_rate"), 0.0) or 0.0
    score = 30.0 + min(30.0, sample_count * 1.5)
    score += positive_rate * 25.0
    score += walk_rate * 15.0
    score += backtest_rate * 10.0
    reasons.append(f"history_samples={sample_count}")
    if positive_rate:
        reasons.append(f"positive_return_rate={positive_rate:.2f}")
    if walk_rate:
        reasons.append(f"walk_forward_pass_rate={walk_rate:.2f}")
    return max(0.0, min(100.0, round(score, 2))), summary, reasons


def _expected_edge_bundle(
    *,
    row: dict[str, Any],
    strategy_id: str,
    regime: dict[str, Any],
    history_summary: dict[str, Any],
    performance_snapshot: dict[str, Any] | None,
    model_evaluation: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str], float]:
    data = _row_data(row)
    candidate_score = _safe_float(_first_non_empty(row.get("candidate_score"), row.get("score"), data.get("candidate_score"), data.get("score")), 50.0) or 50.0
    liquidity_score = _safe_float(_first_non_empty(row.get("liquidity_score"), data.get("liquidity_score")), 50.0) or 50.0
    trend_score = _safe_float(_first_non_empty(row.get("trend_score"), data.get("trend_score")), 50.0) or 50.0
    volatility_score = _safe_float(_first_non_empty(row.get("volatility_score"), data.get("volatility_score")), 50.0) or 50.0
    risk_score = _safe_float(_first_non_empty(row.get("risk_score"), data.get("risk_score")), 50.0) or 50.0
    strategy_fit_score = _safe_float(_first_non_empty(row.get("strategy_fit_score"), data.get("strategy_fit_score")), 50.0) or 50.0

    calibrated_probability = None
    calibration_curve = (model_evaluation or {}).get("calibration_curve") if isinstance(model_evaluation, dict) else None
    if isinstance(calibration_curve, list):
        calibrated_probability = _calibration_probability(candidate_score, calibration_curve)
    if calibrated_probability is None:
        bucket_rate = None
        score_buckets = (performance_snapshot or {}).get("score_bucket_distribution") if isinstance(performance_snapshot, dict) else None
        if isinstance(score_buckets, list):
            label = _bucket_label(candidate_score)
            for bucket in score_buckets:
                if _safe_text(bucket.get("score_bucket")) == label:
                    walk_rate = _safe_float(bucket.get("walk_forward_complete_rate"))
                    backtest_rate = _safe_float(bucket.get("backtest_complete_rate"))
                    if walk_rate is not None and backtest_rate is not None:
                        bucket_rate = ((walk_rate / 100.0) * 0.6) + ((backtest_rate / 100.0) * 0.4)
                    elif walk_rate is not None:
                        bucket_rate = walk_rate / 100.0
                    elif backtest_rate is not None:
                        bucket_rate = backtest_rate / 100.0
                    break
        calibrated_probability = bucket_rate if bucket_rate is not None else max(0.0, min(1.0, candidate_score / 100.0))

    expected_return_hist = _safe_float(history_summary.get("average_forward_return"))
    positive_rate = _safe_float(history_summary.get("positive_return_rate"), 0.0) or 0.0
    walk_rate = _safe_float(history_summary.get("walk_forward_pass_rate"), 0.0) or 0.0
    backtest_rate = _safe_float(history_summary.get("backtest_pass_rate"), 0.0) or 0.0
    sample_count = int(history_summary.get("sample_count") or 0)
    regime_name = _safe_text(regime.get("regime") or "UNKNOWN")
    alignment_score, alignment_reasons = _regime_alignment_score(strategy_id, regime_name)
    regime_confidence = _safe_float(regime.get("confidence"), 0.0) or 0.0
    regime_bonus = (alignment_score / 100.0) - 0.5

    # Candidate score should influence the edge slightly but not dominate the historical evidence.
    score_bias = (candidate_score - 50.0) / 200.0
    if expected_return_hist is None:
        expected_return_hist = score_bias / 2.0
    expected_return = round((expected_return_hist * (1.0 + regime_bonus * 0.35)) + score_bias * 0.15, 4)

    expected_volatility = max(
        0.01,
        round(
            max(
                0.01,
                (100.0 - liquidity_score) / 500.0,
                volatility_score / 150.0,
                abs(expected_return_hist or 0.0) * 0.8,
            ),
            4,
        ),
    )
    expected_drawdown = max(
        0.01,
        round(
            max(
                0.01,
                risk_score / 200.0,
                abs(_safe_float(history_summary.get("average_max_drawdown"), 0.0) or 0.0),
                volatility_score / 100.0,
            ),
            4,
        ),
    )
    probability_of_success = max(
        0.0,
        min(
            1.0,
            round(
                (calibrated_probability * 0.55)
                + (positive_rate * 0.20)
                + (walk_rate * 0.15)
                + (backtest_rate * 0.10),
                4,
            ),
        ),
    )
    if sample_count == 0:
        probability_of_success = max(0.0, min(1.0, probability_of_success * 0.65))
    risk_adjusted_edge = round(expected_return / max(expected_drawdown, 0.01), 4)
    confidence_interval = {
        "lower": round(expected_return - max(0.005, expected_volatility / max(2.0, sqrt(sample_count + 1))), 4),
        "upper": round(expected_return + max(0.005, expected_volatility / max(2.0, sqrt(sample_count + 1))), 4),
    }
    reasons = [
        f"regime={regime_name}",
        f"alignment={alignment_score:.1f}",
        f"candidate_score={candidate_score:.1f}",
    ]
    reasons.extend(alignment_reasons)
    if history_summary:
        reasons.append(f"history_samples={sample_count}")
    return (
        {
            "expected_return": expected_return,
            "expected_volatility": round(expected_volatility, 4),
            "expected_drawdown": round(expected_drawdown, 4),
            "probability_of_success": round(probability_of_success, 4),
            "risk_adjusted_edge": risk_adjusted_edge,
            "confidence_interval": confidence_interval,
        },
        reasons,
        float(probability_of_success),
    )


def _composition_group(row: dict[str, Any]) -> str:
    data = _row_data(row)
    sector = _safe_text(_first_non_empty(row.get("sector"), row.get("industry"), data.get("sector"), data.get("industry")))
    if sector:
        return sector.lower()
    asset_type = _safe_text(_first_non_empty(row.get("asset_type"), data.get("asset_type"))).lower()
    if asset_type:
        return asset_type
    symbol = _safe_text(_first_non_empty(row.get("symbol"), row.get("ticker"))).upper()
    return symbol.split(".")[0] if symbol else "unclassified"


def _underlying_root(row: dict[str, Any]) -> str:
    symbol = _safe_text(_first_non_empty(row.get("symbol"), row.get("ticker"))).upper()
    if not symbol:
        return "UNKNOWN"
    base = symbol.split(".")[0]
    # remove trailing leveraged/inverse suffixes in a conservative way
    for suffix in ("UL", "US", "S", "L", "D", "T"):
        if base.endswith(suffix) and len(base) > 4:
            return base[:-1]
    return base


def _apply_composition_constraints(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    limits = {
        "max_selected": 3,
        "max_sector_weight": 0.40,
        "max_same_underlying": 1,
        "max_leveraged_or_inverse": 1,
    }
    sector_counts: Counter[str] = Counter()
    underlying_counts: Counter[str] = Counter()
    leveraged_inverse_count = 0
    selected_symbols: list[str] = []
    selected_ids: list[str] = []

    for row in sorted(rows, key=lambda item: (-float(item.get("fit_score") or 0.0), -float(item.get("expected_edge", {}).get("risk_adjusted_edge") or 0.0), str(item.get("symbol") or item.get("ticker") or ""))):
        asset_type = _safe_text(_first_non_empty(row.get("asset_type"), _row_data(row).get("asset_type"))).lower()
        group = _composition_group(row)
        underlying = _underlying_root(row)
        symbol = _safe_text(_first_non_empty(row.get("symbol"), row.get("ticker"))).upper()
        candidate_id = _safe_text(row.get("candidate_id") or symbol)

        if len(selected) >= limits["max_selected"]:
            blocked.append({
                "candidate_id": candidate_id,
                "symbol": symbol,
                "reason": "max_selected_reached",
                "sector": group,
                "underlying": underlying,
            })
            row = dict(row)
            row["selected"] = False
            row["blocked_reason"] = "max_selected_reached"
            continue

        if asset_type in {"leveraged_etf", "inverse_etf"}:
            if leveraged_inverse_count >= limits["max_leveraged_or_inverse"]:
                blocked.append({
                    "candidate_id": candidate_id,
                    "symbol": symbol,
                    "reason": "leveraged_inverse_limit_exceeded",
                    "sector": group,
                    "underlying": underlying,
                })
                row = dict(row)
                row["selected"] = False
                row["blocked_reason"] = "leveraged_inverse_limit_exceeded"
                continue
            leveraged_inverse_count += 1

        if sector_counts[group] >= max(1, int(round(limits["max_selected"] * limits["max_sector_weight"]))):
            blocked.append({
                "candidate_id": candidate_id,
                "symbol": symbol,
                "reason": "sector_cap_exceeded",
                "sector": group,
                "underlying": underlying,
            })
            row = dict(row)
            row["selected"] = False
            row["blocked_reason"] = "sector_cap_exceeded"
            continue

        if underlying_counts[underlying] >= limits["max_same_underlying"]:
            blocked.append({
                "candidate_id": candidate_id,
                "symbol": symbol,
                "reason": "same_underlying_conflict",
                "sector": group,
                "underlying": underlying,
            })
            row = dict(row)
            row["selected"] = False
            row["blocked_reason"] = "same_underlying_conflict"
            continue

        selected_row = dict(row)
        selected_row["selected"] = True
        selected_row["blocked_reason"] = ""
        selected_row["final_rank"] = len(selected) + 1
        selected.append(selected_row)
        selected_ids.append(candidate_id)
        if symbol:
            selected_symbols.append(symbol)
        sector_counts[group] += 1
        underlying_counts[underlying] += 1

    summary = {
        "max_selected": limits["max_selected"],
        "max_sector_weight": limits["max_sector_weight"],
        "max_same_underlying": limits["max_same_underlying"],
        "max_leveraged_or_inverse": limits["max_leveraged_or_inverse"],
        "selected_count": len(selected),
        "blocked_count": len(blocked),
        "selected_symbols": selected_symbols,
        "selected_candidate_ids": selected_ids,
        "selected_sectors": dict(sector_counts),
        "selected_underlyings": dict(underlying_counts),
        "leveraged_inverse_selected_count": leveraged_inverse_count,
    }
    return selected, blocked, summary


def build_strategy_research(
    selection_report: dict[str, Any] | None,
    *,
    dataset_samples: Iterable[Any] | None = None,
    performance_snapshot: dict[str, Any] | None = None,
    model_evaluation: dict[str, Any] | None = None,
    market_regime_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = dict(selection_report or {})
    rows = _candidate_rows(report)
    regime = dict(market_regime_report or analyze_market_regime(report))
    history = _dataset_samples_summary(dataset_samples or [])
    performance_snapshot = dict(performance_snapshot or {})
    model_evaluation = dict(model_evaluation or {})
    calibration_curve = list(model_evaluation.get("calibration_curve") or [])

    strategy_candidates: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        data = _row_data(row)
        symbol = _safe_text(_first_non_empty(row.get("symbol"), row.get("ticker"))).upper()
        candidate_id = _safe_text(row.get("candidate_id") or symbol or f"candidate_{index}")
        asset_type = _safe_text(_first_non_empty(row.get("asset_type"), data.get("asset_type"))).lower() or "common_stock"
        trend_score = _safe_float(_first_non_empty(row.get("trend_score"), data.get("trend_score")), 50.0) or 50.0
        raw_strategy = _first_non_empty(row.get("recommended_strategy"), row.get("strategy_family"), data.get("strategy_family"))
        strategy_id = normalize_strategy_id(raw_strategy) or _default_strategy_for_asset(asset_type, trend_score)
        rule = strategy_rule_for(strategy_id)
        if rule is None:
            rule = StrategyRule(
                strategy_id=strategy_id,
                aliases=(),
                allowed_regimes=(),
                blocked_regimes=(),
                allowed_asset_types=(asset_type,),
                requires_benchmark=True,
                requires_backtest=True,
                requires_walk_forward=True,
                min_liquidity_score=0.0,
                min_confidence=0.55,
                expected_edge_bias=0.0,
                description="unsupported_strategy_family",
                notes=("unsupported_strategy_family",),
            )

        score_bucket = _bucket_label(_safe_float(_first_non_empty(row.get("candidate_score"), row.get("score"), data.get("candidate_score"), data.get("score"))))
        strategy_history = dict(history.get(strategy_id) or {})
        liquidity_score, liquidity_reasons = _liquidity_quality(row)
        data_quality_score, data_quality_reasons = _data_quality_score(row)
        alignment_score, alignment_reasons = _regime_alignment_score(strategy_id, regime.get("regime"))
        history_score, history_summary, history_reasons = _history_score(strategy_id, history)
        candidate_score = _safe_float(_first_non_empty(row.get("candidate_score"), row.get("score"), data.get("candidate_score"), data.get("score")), 0.0) or 0.0
        strategy_fit_score = _safe_float(_first_non_empty(row.get("strategy_fit_score"), data.get("strategy_fit_score")), 50.0) or 50.0
        regime_confidence = _safe_float(regime.get("confidence"), 0.0) or 0.0
        strategy_confidence = max(
            0.0,
            min(
                1.0,
                round(
                    0.35 * regime_confidence
                    + 0.20 * (history_summary.get("positive_return_rate") or 0.0)
                    + 0.20 * (history_summary.get("walk_forward_pass_rate") or 0.0)
                    + 0.15 * (data_quality_score / 100.0)
                    + 0.10 * (liquidity_score / 100.0),
                    4,
                ),
            ),
        )
        if not bool(_first_non_empty(row.get("scoring_eligible"), data.get("scoring_eligible"))):
            strategy_confidence = max(0.0, round(strategy_confidence - 0.20, 4))
        if _safe_text(_first_non_empty(row.get("data_status"), data.get("data_status"))).upper() != "VALID":
            strategy_confidence = max(0.0, round(strategy_confidence - 0.20, 4))

        fit_score = (
            0.30 * candidate_score
            + 0.18 * strategy_fit_score
            + 0.18 * alignment_score
            + 0.14 * history_score
            + 0.12 * data_quality_score
            + 0.08 * liquidity_score
        )

        allowed_reasons: list[str] = []
        blocked_reason = ""
        allowed = True
        regime_name = _safe_text(regime.get("regime") or "UNKNOWN")
        if rule.allowed_asset_types and asset_type not in rule.allowed_asset_types:
            allowed = False
            blocked_reason = "asset_type_not_allowed"
            allowed_reasons.append(blocked_reason)
        if rule.allowed_regimes and regime_name not in rule.allowed_regimes:
            allowed = False
            blocked_reason = blocked_reason or "regime_not_allowed"
            allowed_reasons.append(f"regime_not_allowed:{regime_name}")
        if rule.blocked_regimes and regime_name in rule.blocked_regimes:
            allowed = False
            blocked_reason = blocked_reason or "regime_blocked"
            allowed_reasons.append(f"regime_blocked:{regime_name}")
        if rule.requires_benchmark and _safe_text(_first_non_empty(row.get("benchmark_status"), row.get("benchmark_alignment_status"), data.get("benchmark_status"), data.get("benchmark_alignment_status"))).upper() != "VALID":
            allowed = False
            blocked_reason = blocked_reason or "benchmark_invalid"
            allowed_reasons.append("benchmark_invalid")
        if _safe_text(_first_non_empty(row.get("freshness_status"), data.get("freshness_status"))).upper() == "INVALID":
            allowed = False
            blocked_reason = blocked_reason or "freshness_invalid"
            allowed_reasons.append("freshness_invalid")
        if data_quality_score < 55.0:
            allowed = False
            blocked_reason = blocked_reason or "data_quality_too_low"
            allowed_reasons.append("data_quality_too_low")
        if liquidity_score < rule.min_liquidity_score:
            allowed = False
            blocked_reason = blocked_reason or "liquidity_below_minimum"
            allowed_reasons.append("liquidity_below_minimum")
        if strategy_confidence < rule.min_confidence:
            allowed = False
            blocked_reason = blocked_reason or "confidence_too_low"
            allowed_reasons.append("confidence_too_low")
        if not history_summary.get("sample_count"):
            allowed_reasons.append("strategy_history_unavailable")
        if strategy_id not in STRATEGY_REGISTRY and not rule.aliases:
            allowed = False
            blocked_reason = blocked_reason or "unsupported_strategy_family"
            allowed_reasons.append("unsupported_strategy_family")

        expected_edge, edge_reasons, probability_of_success = _expected_edge_bundle(
            row=row,
            strategy_id=strategy_id,
            regime=regime,
            history_summary=history_summary,
            performance_snapshot=performance_snapshot,
            model_evaluation=model_evaluation,
        )
        if rule.expected_edge_bias:
            expected_edge["expected_return"] = round(expected_edge["expected_return"] + rule.expected_edge_bias, 4)
            expected_edge["risk_adjusted_edge"] = round(expected_edge["expected_return"] / max(expected_edge["expected_drawdown"], 0.01), 4)
        if calibration_curve:
            allowed_reasons.append("calibration_curve_applied")

        expected_risk = {
            "expected_volatility": expected_edge["expected_volatility"],
            "expected_drawdown": expected_edge["expected_drawdown"],
            "liquidity_risk": round(max(0.0, 1.0 - liquidity_score / 100.0), 4),
            "regime_risk": round(max(0.0, 1.0 - alignment_score / 100.0), 4),
        }
        factor_scores = {
            "candidate_score": round(candidate_score, 2),
            "liquidity_score": round(liquidity_score, 2),
            "trend_score": round(_safe_float(_first_non_empty(row.get("trend_score"), data.get("trend_score")), 50.0) or 50.0, 2),
            "volatility_score": round(_safe_float(_first_non_empty(row.get("volatility_score"), data.get("volatility_score")), 50.0) or 50.0, 2),
            "risk_score": round(_safe_float(_first_non_empty(row.get("risk_score"), data.get("risk_score")), 50.0) or 50.0, 2),
            "strategy_fit_score": round(strategy_fit_score, 2),
        }
        strategy_candidates.append(
            {
                "candidate_id": candidate_id,
                "symbol": symbol,
                "asset_type": asset_type,
                "sector": _composition_group(row),
                "strategy_id": strategy_id,
                "strategy_family": strategy_id,
                "recommended_strategy": strategy_id,
                "fit_score": round(max(0.0, min(100.0, fit_score)), 2),
                "expected_edge": expected_edge,
                "expected_risk": expected_risk,
                "confidence": round(strategy_confidence, 4),
                "reasons": list(dict.fromkeys([*allowed_reasons, *edge_reasons, *liquidity_reasons, *data_quality_reasons, *alignment_reasons, *history_reasons])),
                "allowed": bool(allowed),
                "blocked_reason": blocked_reason,
                "market_regime": regime_name,
                "market_regime_confidence": regime_confidence,
                "score_bucket": score_bucket,
                "candidate_score": round(candidate_score, 2),
                "factor_scores": factor_scores,
                "history_summary": history_summary,
                "calibrated_probability": round(expected_edge["probability_of_success"], 4),
                "calibrated_score": round(expected_edge["probability_of_success"] * 100.0, 2),
                "trade_admission_status": "NOT_TRADABLE" if not allowed else _safe_text(_first_non_empty(row.get("trade_admission_status"), data.get("trade_admission_status"))) or "NOT_TRADABLE",
                "selection_stage": _safe_text(_first_non_empty(row.get("selection_stage"), data.get("selection_stage"))).upper() or "FINALIZED",
                "data_status": _safe_text(_first_non_empty(row.get("data_status"), data.get("data_status"))).upper() or "UNKNOWN",
                "freshness_status": _safe_text(_first_non_empty(row.get("freshness_status"), data.get("freshness_status"))).upper() or "UNKNOWN",
                "benchmark_status": _safe_text(_first_non_empty(row.get("benchmark_status"), row.get("benchmark_alignment_status"), data.get("benchmark_status"), data.get("benchmark_alignment_status"))).upper() or "UNKNOWN",
                "scoring_eligible": bool(_first_non_empty(row.get("scoring_eligible"), data.get("scoring_eligible"))),
                "trade_filter_passed": bool(_first_non_empty(row.get("trade_filter_passed"), data.get("trade_filter_passed"))),
                "candidate_fallback": bool(_first_non_empty(row.get("candidate_fallback"), data.get("candidate_fallback"))),
                "mock_used": bool(_first_non_empty(row.get("mock_used"), data.get("mock_used"))),
                "data_mode": _safe_text(_first_non_empty(row.get("data_mode"), data.get("data_mode"))),
                "data_quality_score": round(data_quality_score, 2),
            }
        )

    eligible_rows = [row for row in strategy_candidates if row.get("allowed")]
    selected_rows, blocked_rows, composition_summary = _apply_composition_constraints(eligible_rows)
    selected_rows = [dict(row, final_selected=True, final_selection_rank=index + 1) for index, row in enumerate(selected_rows)]
    blocked_rows = [dict(row, final_selected=False) for row in blocked_rows]
    selected_map = {str(row.get("candidate_id") or ""): dict(row) for row in selected_rows}
    blocked_map = {str(row.get("candidate_id") or ""): dict(row) for row in blocked_rows}
    candidate_matrix: list[dict[str, Any]] = []
    for row in strategy_candidates:
        candidate_id = str(row.get("candidate_id") or "")
        selected_row = selected_map.get(candidate_id)
        blocked_row = blocked_map.get(candidate_id)
        annotated = dict(row)
        if selected_row:
            annotated.update({
                "selected": True,
                "blocked_reason": "",
                "final_selection_rank": selected_row.get("final_selection_rank"),
            })
        elif blocked_row:
            annotated.update({
                "selected": False,
                "blocked_reason": blocked_row.get("blocked_reason") or blocked_row.get("reason") or "composition_blocked",
            })
        elif not row.get("allowed", False):
            annotated.update({
                "selected": False,
                "blocked_reason": row.get("blocked_reason") or "eligibility_blocked",
            })
        else:
            annotated.setdefault("selected", False)
        candidate_matrix.append(annotated)

    final_selected_count = len(selected_rows)
    selection_outcome = "NO_ACTIONABLE_RESEARCH_CANDIDATE" if final_selected_count == 0 else "ACTIONABLE_RESEARCH_CANDIDATE"
    if final_selected_count == 0:
        selection_note = "insufficient_data_or_constraints_blocked"
    else:
        selection_note = "research_candidates_ready"

    candidate_ranking_summary = {
        "candidate_count": len(strategy_candidates),
        "selected_count": final_selected_count,
        "blocked_count": len(blocked_rows),
        "average_fit_score": round(mean([row["fit_score"] for row in strategy_candidates]), 4) if strategy_candidates else None,
        "average_expected_return": round(mean([row["expected_edge"]["expected_return"] for row in strategy_candidates]), 4) if strategy_candidates else None,
        "average_expected_drawdown": round(mean([row["expected_edge"]["expected_drawdown"] for row in strategy_candidates]), 4) if strategy_candidates else None,
        "average_probability_of_success": round(mean([row["expected_edge"]["probability_of_success"] for row in strategy_candidates]), 4) if strategy_candidates else None,
    }

    return {
        "title": "Strategy Selection",
        "generated_at": regime.get("generated_at") or None,
        "selection_outcome": selection_outcome,
        "selection_note": selection_note,
        "market_regime": regime,
        "strategy_registry": strategy_registry_snapshot(),
        "strategy_candidates": list(candidate_matrix),
        "candidate_strategy_matrix": list(candidate_matrix),
        "final_selected": list(selected_rows),
        "final_selected_count": final_selected_count,
        "blocked_candidates": list(blocked_rows),
        "portfolio_composition": composition_summary,
        "candidate_ranking_summary": candidate_ranking_summary,
        "actionable_candidate_status": "NO_ACTIONABLE_RESEARCH_CANDIDATE" if final_selected_count == 0 else "RESEARCH_CANDIDATE_AVAILABLE",
        "insufficient_data_warning": final_selected_count == 0,
    }
