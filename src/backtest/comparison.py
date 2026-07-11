from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

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
) -> StrategyComparisonResult:
    backtester = StrategyBacktester(strategy="baseline", initial_cash=initial_cash)
    bars_list = backtester._load_bars(bars, symbol=symbol)
    if not bars_list:
        raise ValueError("No bars supplied")
    symbol = symbol or bars_list[0].symbol
    comparison_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    results: list[Any] = []
    warnings: list[str] = []
    parameter_candidates: list[dict[str, Any]] = []

    for version in versions:
        normalized = _normalize_version(version)
        result = StrategyBacktester(strategy=normalized, initial_cash=initial_cash).run(
            bars_list,
            symbol=symbol,
            benchmark_bars=benchmark_bars,
            parameter_set=dict(parameter_set or {}, strategy=normalized),
            initial_cash=initial_cash,
        )
        results.append(result)
        metrics = _copy_metrics(result.metrics)
        metric_row = {
            "version": normalized,
            "strategy": result.strategy,
            "trade_count": int(metrics.get("trade_count", 0) or 0),
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
            "time_stop_count": metrics.get("time_stop_count"),
            "risk_adjusted_score": metrics.get("risk_adjusted_score"),
            "no_trade": metrics.get("no_trade"),
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
    summary = {
        "baseline_version": baseline_row,
        "best_version": ranking[0] if ranking else None,
        "risk_adjusted_ranking": [row["version"] for row in ranking],
        "trade_count_warning": any(int(row.get("trade_count", 0) or 0) < 3 for row in comparison_rows),
        "no_trade_versions": [row["version"] for row in comparison_rows if row.get("no_trade")],
    }
    warnings.extend(
        [
            "no_trade_version_detected" if any(row.get("no_trade") for row in comparison_rows) else "",
            "low_trade_count_detected" if any(int(row.get("trade_count", 0) or 0) < 3 for row in comparison_rows) else "",
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
