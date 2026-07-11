from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Iterable, Sequence

from .models import Bar, WalkForwardResult, WalkForwardWindowResult
from .reporting import write_walk_forward_artifacts
from .strategy_backtester import StrategyBacktester


@dataclass
class WalkForwardConfig:
    train_size: int
    validation_size: int
    test_size: int
    step_size: int
    anchored: bool = False
    purge_gap: int = 0
    embargo_gap: int = 0
    random_seed: int = 42
    sharpe_weight: float = 1.0
    calmar_weight: float = 1.0
    drawdown_weight: float = 1.0
    turnover_weight: float = 0.1
    instability_weight: float = 0.5
    no_trade_penalty: float = 1.0


@dataclass
class WalkForwardEvaluator:
    config: WalkForwardConfig
    backtester: StrategyBacktester = field(default_factory=StrategyBacktester)

    def evaluate(
        self,
        bars: Sequence[Bar] | Iterable[Bar] | Any,
        *,
        symbol: str | None = None,
        strategy: str = "a",
        parameter_grid: list[dict[str, Any]] | None = None,
        initial_cash: float | None = None,
        output_dir: str | None = None,
    ) -> WalkForwardResult:
        bars_list = self.backtester._load_bars(bars, symbol=symbol)
        if not bars_list:
            raise ValueError("No bars supplied")
        symbol = symbol or bars_list[0].symbol
        parameter_grid = parameter_grid or [{}]
        windows = self._build_windows(bars_list)
        window_results: list[WalkForwardWindowResult] = []
        stitched_oos_equity: list[dict[str, Any]] = []
        warnings: list[str] = []
        failure_count = 0
        no_trade_count = 0
        selection_counts: dict[str, int] = {}

        for window in windows:
            best_params: dict[str, Any] | None = None
            best_score = float("-inf")
            best_validation_result = None
            for params in parameter_grid:
                candidate_params = dict(params)
                candidate_params.setdefault("strategy", strategy)
                validation_result = self.backtester.run(
                    bars_list[: window["validation_end"]],
                    symbol=symbol,
                    trade_start_time=window["validation_start"],
                    parameter_set=candidate_params,
                    initial_cash=initial_cash or self.backtester.initial_cash,
                )
                score = self._score_result(validation_result.metrics)
                if score > best_score:
                    best_score = score
                    best_params = dict(candidate_params)
                    best_validation_result = validation_result
            if best_params is None or best_validation_result is None:
                failure_count += 1
                warnings.append("no_valid_parameter_set")
                continue
            selection_key = self._param_key(best_params)
            selection_counts[selection_key] = selection_counts.get(selection_key, 0) + 1
            test_result = self.backtester.run(
                bars_list[: window["test_end"]],
                symbol=symbol,
                trade_start_time=window["test_start"],
                parameter_set=best_params,
                initial_cash=initial_cash or self.backtester.initial_cash,
            )
            if test_result.metrics.get("trade_count", 0) == 0:
                no_trade_count += 1
            stitched_oos_equity.extend(
                {
                    "timestamp": point.get("timestamp"),
                    "equity": point.get("equity"),
                    "window_start": window["test_start"].isoformat(),
                    "window_end": window["test_end_dt"].isoformat(),
                }
                for point in test_result.equity_curve
            )
            window_results.append(
                WalkForwardWindowResult(
                    train_range={"start": window["train_start"].isoformat(), "end": window["train_end"].isoformat()},
                    validation_range={"start": window["validation_start"].isoformat(), "end": window["validation_end_dt"].isoformat()},
                    test_range={"start": window["test_start"].isoformat(), "end": window["test_end_dt"].isoformat()},
                    selected_parameters=best_params,
                    validation_score=round(best_score, 6),
                    test_metrics=test_result.metrics,
                    trade_count=int(test_result.metrics.get("trade_count", 0) or 0),
                    warnings=list(test_result.warnings),
                )
            )

        aggregate_metrics = self._aggregate_metrics([window.test_metrics for window in window_results])
        parameter_stability = {
            "unique_parameter_sets": len(selection_counts),
            "selection_counts": selection_counts,
            "most_common_parameter_set": max(selection_counts.items(), key=lambda item: item[1])[0] if selection_counts else None,
            "most_common_frequency": max(selection_counts.values()) if selection_counts else 0,
            "instability_score": round(1.0 - (max(selection_counts.values()) / sum(selection_counts.values())), 6) if selection_counts else 1.0,
        }
        result = WalkForwardResult(
            strategy=strategy,
            symbol=symbol,
            windows=window_results,
            stitched_oos_equity=stitched_oos_equity,
            aggregate_oos_metrics=aggregate_metrics,
            parameter_stability=parameter_stability,
            window_failure_count=failure_count,
            no_trade_window_count=no_trade_count,
            warnings=warnings,
        )
        if output_dir:
            write_walk_forward_artifacts(result, output_dir)
        return result

    def _build_windows(self, bars: Sequence[Bar]) -> list[dict[str, Any]]:
        cfg = self.config
        windows: list[dict[str, Any]] = []
        total = len(bars)
        start = 0
        while True:
            if cfg.anchored:
                train_start = 0
            else:
                train_start = start
            train_end = train_start + cfg.train_size
            validation_start = train_end + cfg.purge_gap
            validation_end = validation_start + cfg.validation_size
            test_start = validation_end + cfg.embargo_gap
            test_end = test_start + cfg.test_size
            if test_end > total:
                break
            windows.append(
                {
                    "train_start": bars[train_start].timestamp,
                    "train_end": bars[train_end - 1].timestamp,
                    "validation_start": bars[validation_start].timestamp,
                    "validation_end_dt": bars[validation_end - 1].timestamp,
                    "validation_end": validation_end,
                    "test_start": bars[test_start].timestamp,
                    "test_end_dt": bars[test_end - 1].timestamp,
                    "test_end": test_end,
                }
            )
            if cfg.anchored:
                start += cfg.step_size
            else:
                start += cfg.step_size
        return windows

    def _score_result(self, metrics: dict[str, Any]) -> float:
        annualized = float(metrics.get("annualized_return") or 0.0)
        sharpe = float(metrics.get("sharpe") or 0.0)
        calmar = float(metrics.get("calmar") or 0.0)
        max_drawdown = float(metrics.get("max_drawdown") or 0.0)
        turnover = float(metrics.get("turnover") or 0.0)
        trade_count = float(metrics.get("trade_count") or 0.0)
        no_trade_penalty = self.config.no_trade_penalty if trade_count == 0 else 0.0
        return (
            annualized
            + self.config.sharpe_weight * sharpe
            + self.config.calmar_weight * calmar
            - self.config.drawdown_weight * max_drawdown
            - self.config.turnover_weight * turnover
            - no_trade_penalty
        )

    def _aggregate_metrics(self, metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
        if not metrics_list:
            return {}
        aggregate: dict[str, Any] = {}
        keys = sorted({key for metrics in metrics_list for key in metrics.keys()})
        for key in keys:
            numeric_values = []
            for metrics in metrics_list:
                value = metrics.get(key)
                if isinstance(value, (int, float)):
                    numeric_values.append(float(value))
            if numeric_values:
                aggregate[f"{key}_mean"] = round(mean(numeric_values), 6)
                aggregate[f"{key}_max"] = round(max(numeric_values), 6)
                aggregate[f"{key}_min"] = round(min(numeric_values), 6)
        return aggregate

    def _param_key(self, params: dict[str, Any]) -> str:
        return "|".join(f"{key}={params[key]}" for key in sorted(params))
