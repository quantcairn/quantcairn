from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .benchmarking import BenchmarkValidation, validate_benchmark_alignment
from .data_feed import infer_bar_frequency
from .models import Bar, StrategyComparisonResult
from .reporting import make_run_id, write_strategy_comparison_artifacts
from .strategy_backtester import StrategyBacktester


def _normalize_version(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"a", "version_a"}:
        return "a"
    if raw in {"b", "version_b"}:
        return "b"
    if raw in {"c", "version_c"}:
        return "c"
    return "baseline"


def _risk_adjusted_score(metrics: dict[str, Any]) -> float:
    annualized = float(metrics.get("annualized_return") or 0.0)
    sharpe = float(metrics.get("sharpe") or 0.0)
    calmar = float(metrics.get("calmar") or 0.0)
    max_drawdown = float(metrics.get("max_drawdown") or 0.0)
    turnover = float(metrics.get("turnover") or 0.0)
    trade_count = int(metrics.get("trade_count") or 0)
    no_trade_penalty = 2.5 if trade_count == 0 else 0.0
    low_trade_penalty = 1.0 if trade_count < 3 else 0.0
    instability_penalty = 0.0
    return (
        annualized * 100.0
        + sharpe * 5.0
        + calmar * 3.0
        - max_drawdown * 100.0
        - turnover * 0.02
        - no_trade_penalty
        - low_trade_penalty
        - instability_penalty
    )


def _copy_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    payload = dict(metrics or {})
    payload["risk_adjusted_score"] = round(_risk_adjusted_score(metrics), 6)
    payload["no_trade"] = bool(payload.get("trade_count", 0) == 0)
    return payload


def _evidence_status(
    *,
    metrics: dict[str, Any],
    benchmark_validation: BenchmarkValidation,
    minimum_trade_count_for_ranking: int,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    trade_count = int(metrics.get("fill_count") or metrics.get("trade_count") or 0)
    if benchmark_validation.status != "VALID":
        reasons.append(benchmark_validation.status.lower())
    if trade_count < minimum_trade_count_for_ranking:
        reasons.append("trade_count_below_threshold")
    if not reasons:
        return "ELIGIBLE", []
    return "INSUFFICIENT_EVIDENCE", reasons


def _profitability_status(metrics: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    total_return = float(metrics.get("total_return") or 0.0)
    max_drawdown = float(metrics.get("max_drawdown") or 0.0)
    closed_trade_count = int(metrics.get("closed_trade_count") or 0)
    profit_factor = metrics.get("profit_factor")
    reconciliation_status = str(metrics.get("reconciliation_status") or "UNKNOWN").upper()
    if closed_trade_count <= 0:
        reasons.append("no_closed_trades")
    if total_return <= 0:
        reasons.append("non_positive_return")
    if profit_factor is not None and float(profit_factor) <= 1.0:
        reasons.append("profit_factor_below_threshold")
    if max_drawdown > 0.2:
        reasons.append("drawdown_above_threshold")
    if reconciliation_status != "OK":
        reasons.append("reconciliation_failed")
    if not reasons:
        return "ELIGIBLE", []
    return "INELIGIBLE", reasons


def _deployment_status(
    *,
    evidence_status: str,
    profitability_status: str,
    benchmark_validation: BenchmarkValidation,
    metrics: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if evidence_status != "ELIGIBLE":
        reasons.append("evidence_not_eligible")
    if profitability_status != "ELIGIBLE":
        reasons.append("profitability_not_eligible")
    if benchmark_validation.status != "VALID":
        reasons.append("invalid_benchmark")
    if str(metrics.get("reconciliation_status") or "UNKNOWN").upper() != "OK":
        reasons.append("reconciliation_failed")
    if not reasons:
        return "ELIGIBLE", []
    return "INELIGIBLE", reasons


def _parameter_key(params: dict[str, Any]) -> str:
    return "|".join(f"{key}={params[key]}" for key in sorted(params))


def compare_versions(
    bars: Sequence[Bar] | Iterable[Bar] | Any,
    *,
    symbol: str | None = None,
    benchmark_bars: Sequence[Bar] | None = None,
    versions: Sequence[str] = ("baseline", "a", "b", "c"),
    initial_cash: float = 10_000.0,
    parameter_set: dict[str, Any] | None = None,
    output_dir: str | Path | None = None,
    scenario_name: str | None = None,
    minimum_trade_count_for_ranking: int = 20,
) -> StrategyComparisonResult:
    backtester = StrategyBacktester(strategy="baseline", initial_cash=initial_cash)
    bars_list = backtester._load_bars(bars, symbol=symbol)
    if not bars_list:
        raise ValueError("No bars supplied")
    symbol = symbol or bars_list[0].symbol
    benchmark_validation = validate_benchmark_alignment(symbol, bars_list, benchmark_bars)
    symbol_frequency = infer_bar_frequency(bars_list)
    comparison_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    results: list[Any] = []
    warnings: list[str] = []
    parameter_candidates: list[dict[str, Any]] = []
    eligible_rows: list[dict[str, Any]] = []

    for version in versions:
        normalized = _normalize_version(version)
        result = StrategyBacktester(strategy=normalized, initial_cash=initial_cash).run(
            bars_list,
            symbol=symbol,
            benchmark_bars=benchmark_bars if benchmark_validation.status == "VALID" else None,
            benchmark_status=benchmark_validation.status,
            parameter_set=dict(parameter_set or {}, strategy=normalized),
            initial_cash=initial_cash,
        )
        results.append(result)
        metrics = _copy_metrics(result.metrics)
        evidence_status, evidence_reasons = _evidence_status(
            metrics=metrics,
            benchmark_validation=benchmark_validation,
            minimum_trade_count_for_ranking=minimum_trade_count_for_ranking,
        )
        profitability_status, profitability_reasons = _profitability_status(metrics)
        deployment_status, deployment_reasons = _deployment_status(
            evidence_status=evidence_status,
            profitability_status=profitability_status,
            benchmark_validation=benchmark_validation,
            metrics=metrics,
        )
        metric_row = {
            "version": normalized,
            "strategy": result.strategy,
            "trade_count_definition": metrics.get("trade_count_definition"),
            "order_count": metrics.get("order_count"),
            "fill_count": metrics.get("fill_count"),
            "trade_count": int(metrics.get("trade_count", 0) or 0),
            "round_trip_trade_count": metrics.get("round_trip_trade_count"),
            "closed_trade_count": metrics.get("closed_trade_count"),
            "open_position_count": metrics.get("open_position_count"),
            "open_position_quantity": metrics.get("open_position_quantity"),
            "winning_trade_count": metrics.get("winning_trade_count"),
            "losing_trade_count": metrics.get("losing_trade_count"),
            "total_return": metrics.get("total_return"),
            "annualized_return": metrics.get("annualized_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "sharpe": metrics.get("sharpe"),
            "sortino": metrics.get("sortino"),
            "calmar": metrics.get("calmar"),
            "profit_factor": metrics.get("profit_factor"),
            "win_rate": metrics.get("win_rate"),
            "turnover": metrics.get("turnover"),
            "exposure": metrics.get("exposure"),
            "average_holding_time": metrics.get("average_holding_time"),
            "longest_losing_streak": metrics.get("longest_losing_streak"),
            "total_commission": metrics.get("total_commission"),
            "total_slippage": metrics.get("total_slippage"),
            "blocked_by_trend_count": metrics.get("blocked_by_trend_count"),
            "blocked_by_cost_count": metrics.get("blocked_by_cost_count"),
            "blocked_by_inventory_count": metrics.get("blocked_by_inventory_count"),
            "time_stop_evaluation_count": metrics.get("time_stop_evaluation_count"),
            "time_stop_signal_count": metrics.get("time_stop_signal_count"),
            "time_stop_exit_count": metrics.get("time_stop_exit_count"),
            "time_stop_blocked_count": metrics.get("time_stop_blocked_count"),
            "time_stop_count": metrics.get("time_stop_count"),
            "profitability_status": profitability_status,
            "profitability_reasons": profitability_reasons,
            "deployment_status": deployment_status,
            "deployment_reasons": deployment_reasons,
            "reconciliation_status": metrics.get("reconciliation_status"),
            "reconciliation_difference": metrics.get("reconciliation_difference"),
            "risk_adjusted_score": metrics.get("risk_adjusted_score"),
            "no_trade": metrics.get("no_trade"),
            "evidence_status": evidence_status,
            "evidence_reasons": evidence_reasons,
            "benchmark_status": benchmark_validation.status,
            "benchmark_symbol": benchmark_validation.benchmark_symbol,
        }
        full_row = dict(metric_row)
        full_row.update(
            {
                "trades": result.trades,
                "orders": result.orders,
                "equity_curve": result.equity_curve,
                "drawdown_curve": result.drawdown_curve,
                "rejected_signals": result.rejected_signals,
            }
        )
        comparison_rows.append(full_row)
        metric_rows.append(metric_row)
        if evidence_status == "ELIGIBLE":
            eligible_rows.append(metric_row)
        parameter_candidates.append(
            {
                "version": normalized,
                "parameter_set": dict(parameter_set or {}),
                "risk_adjusted_score": metrics.get("risk_adjusted_score"),
                "trade_count": metrics.get("trade_count"),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "calmar": metrics.get("calmar"),
            }
        )

    baseline_row = next((row for row in comparison_rows if row["version"] == "baseline"), None)
    ranking = sorted(metric_rows, key=lambda row: (row.get("risk_adjusted_score") or float("-inf")), reverse=True)
    eligible_ranking = sorted(eligible_rows, key=lambda row: (row.get("risk_adjusted_score") or float("-inf")), reverse=True)
    insufficient_evidence_rows = [row for row in metric_rows if row.get("evidence_status") != "ELIGIBLE"]
    profitability_rows = [row for row in metric_rows if row.get("profitability_status") == "ELIGIBLE"]
    deployment_rows = [row for row in metric_rows if row.get("deployment_status") == "ELIGIBLE"]
    ranking_status = "ELIGIBLE" if benchmark_validation.status == "VALID" and eligible_ranking else "INSUFFICIENT_EVIDENCE"
    if benchmark_validation.status != "VALID":
        ranking_status = "INVALID_BENCHMARK"
    summary = {
        "baseline_version": baseline_row,
        "best_version": eligible_ranking[0] if eligible_ranking else None,
        "best_version_all": ranking[0] if ranking else None,
        "risk_adjusted_ranking": [row["version"] for row in ranking],
        "eligible_ranking": [row["version"] for row in eligible_ranking],
        "profitability_eligible_versions": [row["version"] for row in profitability_rows],
        "deployment_eligible_versions": [row["version"] for row in deployment_rows],
        "insufficient_evidence_versions": [row["version"] for row in insufficient_evidence_rows],
        "benchmark_status": benchmark_validation.status,
        "benchmark_validation": benchmark_validation.to_dict(),
        "data_frequency": symbol_frequency,
        "benchmark_frequency": benchmark_validation.benchmark_frequency,
        "ranking_status": ranking_status,
        "evidence_status": "ELIGIBLE" if not insufficient_evidence_rows and benchmark_validation.status == "VALID" else "INSUFFICIENT_EVIDENCE",
        "profitability_status": "ELIGIBLE" if profitability_rows else "INELIGIBLE",
        "deployment_status": "ELIGIBLE" if deployment_rows else "INELIGIBLE",
        "evidence_thresholds": {
            "minimum_trade_count_for_ranking": int(minimum_trade_count_for_ranking),
        },
        "trade_count_warning": any(int(row.get("fill_count", row.get("trade_count", 0)) or 0) < minimum_trade_count_for_ranking for row in comparison_rows),
        "no_trade_versions": [row["version"] for row in comparison_rows if row.get("no_trade")],
    }
    warnings.extend(
        [
            "invalid_benchmark" if benchmark_validation.status != "VALID" else "",
            "no_trade_version_detected" if any(row.get("no_trade") for row in comparison_rows) else "",
            "low_trade_count_detected" if any(int(row.get("trade_count", 0) or 0) < minimum_trade_count_for_ranking for row in comparison_rows) else "",
            "insufficient_evidence" if ranking_status != "ELIGIBLE" else "",
        ]
    )
    warnings = [warning for warning in warnings if warning]
    result = StrategyComparisonResult(
        run_id=make_run_id("comparison", symbol, bars_list[0].timestamp.isoformat(), bars_list[-1].timestamp.isoformat()),
        symbol=symbol,
        data_start=bars_list[0].timestamp.isoformat(),
        data_end=bars_list[-1].timestamp.isoformat(),
        comparison=comparison_rows,
        summary=summary,
        metrics=metric_rows,
        ranking=ranking,
        parameter_candidates=parameter_candidates,
        parameter_stability=_parameter_stability(parameter_candidates),
        warnings=warnings,
        configuration={
            "initial_cash": float(initial_cash),
            "versions": [str(version) for version in versions],
            "scenario_name": scenario_name,
            "strategy_parameters": dict(parameter_set or {}),
            "benchmark_validation": benchmark_validation.to_dict(),
            "data_frequency": symbol_frequency,
            "evidence_thresholds": {
                "minimum_trade_count_for_ranking": int(minimum_trade_count_for_ranking),
            },
        },
    )
    if output_dir:
        write_strategy_comparison_artifacts(result, output_dir)
    return result


def _parameter_stability(parameter_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not parameter_candidates:
        return {
            "unique_parameter_sets": 0,
            "selection_counts": {},
            "most_common_parameter_set": None,
            "most_common_frequency": 0,
            "instability_score": 1.0,
            "isolated_optimum": False,
            "regime_dependent": False,
            "too_few_trades": True,
            "high_turnover": False,
            "unstable_parameters": False,
            "drawdown_sensitive": False,
        }
    counts: dict[str, int] = {}
    for candidate in parameter_candidates:
        key = _parameter_key(candidate.get("parameter_set") or {})
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values()) or 1
    most_common_key = max(counts.items(), key=lambda item: item[1])[0]
    most_common_freq = counts[most_common_key]
    instability_score = 1.0 - (most_common_freq / total)
    return {
        "unique_parameter_sets": len(counts),
        "selection_counts": counts,
        "most_common_parameter_set": most_common_key,
        "most_common_frequency": most_common_freq,
        "instability_score": round(instability_score, 6),
        "isolated_optimum": len(counts) > 1 and most_common_freq / total < 0.5,
        "regime_dependent": len(counts) > 2,
        "too_few_trades": any(int(candidate.get("trade_count") or 0) < 3 for candidate in parameter_candidates),
        "high_turnover": any(float(candidate.get("risk_adjusted_score") or 0.0) < 0 for candidate in parameter_candidates),
        "unstable_parameters": instability_score > 0.5,
        "drawdown_sensitive": any(float(candidate.get("max_drawdown") or 0.0) > 0.2 for candidate in parameter_candidates),
    }
