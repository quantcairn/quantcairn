from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import floor
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..engine.position_sizing import determine_buy_quantity
from ..strategy import (
    DynamicRangeCalculator,
    EntryLayerPlanner,
    ExitLayerManager,
    InventoryAwareSizer,
    StrategyStateStore,
    TimeStop,
    TradeCostEstimator,
    TrendGuard,
)
from .data_feed import BacktestDataFeed
from .execution import BacktestExecutionModel
from .metrics import compute_backtest_metrics
from .models import BacktestOrder, BacktestResult, Bar
from .portfolio import BacktestPortfolio
from .reporting import make_run_id, write_backtest_artifacts


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _rolling_range(history: Sequence[Bar], lookback: int) -> dict[str, Any]:
    if len(history) < lookback:
        return {
            "center": None,
            "support": None,
            "resistance": None,
            "grid_width": 0.0,
            "range_width_pct": 0.0,
            "range_quality": "invalid",
            "valid": False,
            "invalid_reason": "insufficient_data",
        }
    recent = history[-lookback:]
    low = min(bar.low for bar in recent)
    high = max(bar.high for bar in recent)
    center = (low + high) / 2.0
    width = max(0.0, high - low)
    width_pct = (width / center * 100.0) if center > 0 else 0.0
    valid = low > 0 and high > low
    invalid_reason = None if valid else "range_too_narrow"
    return {
        "center": round(center, 6),
        "support": round(low, 6),
        "resistance": round(high, 6),
        "grid_width": round(width, 6),
        "range_width_pct": round(width_pct, 6),
        "range_quality": "balanced" if valid else "invalid",
        "valid": valid,
        "invalid_reason": invalid_reason,
    }


def _bar_history_to_series(history: Sequence[Bar]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    highs = [{"timestamp": bar.timestamp, "value": bar.high} for bar in history]
    lows = [{"timestamp": bar.timestamp, "value": bar.low} for bar in history]
    closes = [{"timestamp": bar.timestamp, "value": bar.close} for bar in history]
    return highs, lows, closes


@dataclass
class StrategyBacktester:
    strategy: str = "baseline"
    initial_cash: float = 10_000.0
    max_position: int = 300
    fixed_size: int = 0
    entry_buffer_pct: float = 0.5
    exit_buffer_pct: float = 0.5
    lookback: int = 20
    commission_per_share: float = 0.005
    platform_fee_per_trade: float = 0.0
    slippage_bps: float = 5.0
    participation_limit: float = 1.0
    minimum_range_pct: float = 1.0
    maximum_range_pct: float = 12.0
    atr_period: int = 14
    ema_period: int = 20
    rolling_lookback: int = 20
    atr_multiplier: float = 1.5
    support_buffer: float = 0.2
    resistance_buffer: float = 0.2
    warmup_bars: int = 30
    dynamic_range_calculator: DynamicRangeCalculator = field(default_factory=DynamicRangeCalculator)
    data_feed: BacktestDataFeed = field(default_factory=BacktestDataFeed)
    execution_model: BacktestExecutionModel = field(default_factory=BacktestExecutionModel)

    def run(
        self,
        bars: Sequence[Bar] | Iterable[Bar] | Any,
        *,
        symbol: str | None = None,
        benchmark_bars: Sequence[Bar] | None = None,
        benchmark_status: str | None = None,
        trade_start_time: datetime | str | None = None,
        output_dir: str | None = None,
        parameter_set: dict[str, Any] | None = None,
        initial_cash: float | None = None,
    ) -> BacktestResult:
        bars_list = self._load_bars(bars, symbol=symbol)
        if not bars_list:
            raise ValueError("No bars supplied")
        symbol = symbol or bars_list[0].symbol
        effective_initial_cash = float(initial_cash if initial_cash is not None else self.initial_cash)
        params = dict(parameter_set or {})
        strategy = str(params.get("strategy") or self.strategy).strip().lower()
        if strategy not in {"baseline", "a", "version_a", "b", "version_b", "c", "version_c"}:
            raise ValueError(f"Unsupported strategy: {strategy}")
        allow_trade_after = self._coerce_trade_start_time(trade_start_time)
        normalized_strategy = "a" if strategy in {"a", "version_a"} else ("b" if strategy in {"b", "version_b"} else ("c" if strategy in {"c", "version_c"} else "baseline"))
        aligned_benchmark_bars = self._align_benchmark_bars(bars_list, benchmark_bars)
        effective_benchmark_status = str(
            benchmark_status
            or ("MISSING_BENCHMARK" if normalized_strategy == "c" and not aligned_benchmark_bars else "VALID")
        ).upper()
        if normalized_strategy in {"b", "c"}:
            result = self._run_layered_strategy(
                bars_list,
                symbol=symbol,
                benchmark_bars=aligned_benchmark_bars,
                benchmark_status=effective_benchmark_status,
                trade_start_time=allow_trade_after,
                output_dir=output_dir,
                parameter_set=params,
                initial_cash=effective_initial_cash,
                strategy=normalized_strategy,
            )
            if output_dir:
                write_backtest_artifacts(result, output_dir)
            return result

        portfolio = BacktestPortfolio(initial_cash=effective_initial_cash)
        pending_orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        rejected_signals: list[dict[str, Any]] = []
        benchmark_lookup = {bar.timestamp: bar.close for bar in (aligned_benchmark_bars or []) if getattr(bar, "timestamp", None)}
        history: list[Bar] = []
        warnings: list[str] = []
        trade_started = allow_trade_after is None

        for idx, bar in enumerate(bars_list):
            self._process_pending_orders(
                current_bar=bar,
                pending_orders=pending_orders,
                portfolio=portfolio,
                orders=orders,
                trades=trades,
                rejected_signals=rejected_signals,
            )
            prices = {symbol: bar.close}
            portfolio.mark_to_market(prices, timestamp=bar.timestamp)
            history.append(bar)
            if not trade_started and allow_trade_after is not None and bar.timestamp >= allow_trade_after:
                trade_started = True

            if not trade_started:
                continue

            position_qty = portfolio.position_quantity(symbol)
            pending_buy = portfolio.has_active_order(symbol, "BUY") or any(
                _normalize(o.get("side")) == "BUY" and _normalize(o.get("status")) == "PENDING"
                for o in pending_orders
                if _normalize(o.get("symbol")) == _normalize(symbol)
            )
            pending_sell = portfolio.has_active_order(symbol, "SELL") or any(
                _normalize(o.get("side")) == "SELL" and _normalize(o.get("status")) == "PENDING"
                for o in pending_orders
                if _normalize(o.get("symbol")) == _normalize(symbol)
            )

            range_snapshot = self._range_snapshot(strategy, history, bar)
            if not range_snapshot.get("valid"):
                rejected_signals.append(
                    {
                        "timestamp": bar.timestamp.isoformat(),
                        "symbol": symbol,
                        "reason": range_snapshot.get("invalid_reason") or "invalid_range",
                        "strategy": strategy,
                    }
                )
                continue

            trend_bias = self._benchmark_bias(symbol, history, benchmark_lookup)
            buy_trigger = range_snapshot["support"] * (1.0 + self.entry_buffer_pct / 100.0)
            sell_trigger = range_snapshot["resistance"] * (1.0 - self.exit_buffer_pct / 100.0)

            if position_qty > 0 and not pending_sell and bar.close >= sell_trigger:
                qty = position_qty
                order = self._make_order(
                    bar=bar,
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                    order_type="MARKET",
                    trigger_price=sell_trigger,
                    strategy=strategy,
                )
                pending_orders.append(order)
                portfolio.register_order(order)
                orders.append(order)
                continue

            if position_qty <= 0 and not pending_buy and bar.close <= buy_trigger:
                if trend_bias == "BLOCK_BUY":
                    rejected_signals.append(
                        {
                            "timestamp": bar.timestamp.isoformat(),
                            "symbol": symbol,
                            "reason": "trend_guard",
                            "strategy": strategy,
                        }
                    )
                    continue
                qty = determine_buy_quantity(
                    current_price=bar.close,
                    available_cash=float(portfolio.available_cash or 0.0),
                    configured_size=int(self.fixed_size or 0),
                    max_position=int(self.max_position),
                    execution_price=bar.open,
                    commission_per_share=self.commission_per_share,
                )
                if qty <= 0:
                    rejected_signals.append(
                        {
                            "timestamp": bar.timestamp.isoformat(),
                            "symbol": symbol,
                            "reason": "quantity_zero",
                            "strategy": strategy,
                        }
                    )
                    continue
                order = self._make_order(
                    bar=bar,
                    symbol=symbol,
                    side="BUY",
                    quantity=qty,
                    order_type="MARKET",
                    trigger_price=buy_trigger,
                    strategy=strategy,
                )
                pending_orders.append(order)
                portfolio.register_order(order)
                orders.append(order)

        if bars_list:
            portfolio.mark_to_market({symbol: bars_list[-1].close}, timestamp=bars_list[-1].timestamp)
        portfolio_snapshot = portfolio.snapshot(bars_list[-1].timestamp if bars_list else None)

        summary = {
            "symbol": symbol,
            "strategy": strategy,
            "benchmark_status": benchmark_status,
            "bars": len(bars_list),
            "filled_orders": len([t for t in trades if str(t.get("status") or "").upper() in {"FILLED", "PARTIALLY_FILLED"}]),
            "rejected_signals": len(rejected_signals),
            "ending_quantity": portfolio.position_quantity(symbol),
        }
        metrics = compute_backtest_metrics(
            initial_cash=effective_initial_cash,
            equity_curve=portfolio.equity_curve,
            trades=trades,
            orders=orders,
            rejected_signals=rejected_signals,
            portfolio_snapshot=portfolio_snapshot,
        )
        result = BacktestResult(
            run_id=make_run_id(strategy, symbol, bars_list[0].timestamp.isoformat(), bars_list[-1].timestamp.isoformat()),
            strategy=strategy,
            symbol=symbol,
            data_start=bars_list[0].timestamp.isoformat(),
            data_end=bars_list[-1].timestamp.isoformat(),
            configuration=self._configuration_dict(params, initial_cash=effective_initial_cash),
            summary=summary,
            metrics=metrics,
            trades=trades,
            orders=orders,
            equity_curve=portfolio.equity_curve,
            drawdown_curve=portfolio.drawdown_curve,
            rejected_signals=rejected_signals,
            warnings=warnings,
            parameter_set=params,
        )
        if output_dir:
            write_backtest_artifacts(result, output_dir)
        return result

    def _run_layered_strategy(
        self,
        bars_list: Sequence[Bar],
        *,
        symbol: str,
        benchmark_bars: Sequence[Bar] | None,
        benchmark_status: str,
        trade_start_time: datetime | None,
        output_dir: str | None,
        parameter_set: dict[str, Any],
        initial_cash: float,
        strategy: str,
    ) -> BacktestResult:
        portfolio = BacktestPortfolio(initial_cash=initial_cash)
        pending_orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        rejected_signals: list[dict[str, Any]] = []
        history: list[Bar] = []
        warnings: list[str] = []
        benchmark_lookup = {bar.timestamp: bar.close for bar in (benchmark_bars or []) if getattr(bar, "timestamp", None)}
        trade_started = trade_start_time is None
        layer_states: dict[int, dict[str, Any]] = {}
        state_store_failed = False
        state_store = StrategyStateStore(Path(output_dir) / "state") if output_dir else StrategyStateStore(Path(tempfile.mkdtemp(prefix="backtest_state_")))
        trend_guard = TrendGuard()
        inventory_sizer = InventoryAwareSizer()
        entry_planner = EntryLayerPlanner()
        exit_manager = ExitLayerManager()
        time_stop = TimeStop()
        cost_estimator = TradeCostEstimator()
        strategy_counters: dict[str, int] = {
            "time_stop_evaluation_count": 0,
            "time_stop_signal_count": 0,
            "time_stop_exit_count": 0,
            "time_stop_blocked_count": 0,
        }
        time_stop_triggered_layers: set[int] = set()
        time_stop_blocked_events: set[str] = set()
        time_stop_event_id: str | None = None
        max_layers = int(parameter_set.get("max_entry_layers") or 5)
        max_position = int(parameter_set.get("max_position") or self.max_position or 300)
        symbol_base = symbol.upper().split(".")[0]
        leveraged_etf = bool(
            parameter_set.get("leveraged_etf")
            or symbol_base
            in {"SOXS", "LABD", "DRIP", "YINN", "TQQQ", "SQQQ", "SOXL", "LABU", "BOIL", "KOLD", "UVXY", "SPXS", "SPXL", "FAS", "FAZ"}
        )
        for idx, bar in enumerate(bars_list):
            last_trade_idx = len(trades)
            self._process_pending_orders(
                current_bar=bar,
                pending_orders=pending_orders,
                portfolio=portfolio,
                orders=orders,
                trades=trades,
                rejected_signals=rejected_signals,
            )
            portfolio.mark_to_market({symbol: bar.close}, timestamp=bar.timestamp)
            history.append(bar)
            if not trade_started and trade_start_time is not None and bar.timestamp >= trade_start_time:
                trade_started = True
            self._sync_layer_state_from_trades(
                layer_states,
                trades[last_trade_idx:],
                bar,
            )

            if not trade_started:
                continue

            range_snapshot = self._range_snapshot("b" if strategy == "b" else "c", history, bar)
            if not range_snapshot.get("valid"):
                rejected_signals.append(
                    {
                        "timestamp": bar.timestamp.isoformat(),
                        "symbol": symbol,
                        "reason": range_snapshot.get("invalid_reason") or "invalid_range",
                        "strategy": strategy,
                    }
                )
                continue

            current_position = portfolio.position_quantity(symbol)
            current_position_value = portfolio.position_value(symbol)
            allowed_position_value = max_position * bar.close
            pending_buy = portfolio.has_active_order(symbol, "BUY")
            pending_sell = portfolio.has_active_order(symbol, "SELL")

            trend_result = {
                "regime": "RANGE",
                "trend_score": 0.0,
                "buy_allowed": True,
                "sell_allowed": True,
                "symbol_reduce_only": False,
                "cooldown_until": None,
                "trigger_reasons": [],
            }
            if strategy == "c":
                if benchmark_status != "VALID":
                    trend_result = {
                        "regime": "INVALID_BENCHMARK",
                        "trend_score": 0.0,
                        "buy_allowed": False,
                        "sell_allowed": True,
                        "symbol_reduce_only": False,
                        "cooldown_until": None,
                        "trigger_reasons": [f"benchmark_{benchmark_status.lower()}"],
                        "benchmark_status": benchmark_status,
                    }
                    if "invalid_benchmark" not in warnings:
                        warnings.append("invalid_benchmark")
                else:
                    benchmark_series = [benchmark_lookup.get(item.timestamp) for item in history if benchmark_lookup.get(item.timestamp) is not None]
                    trend_result = trend_guard.evaluate(
                        timestamp=bar.timestamp,
                        current_price=bar.close,
                        closes=[item.close for item in history],
                        highs=[item.high for item in history],
                        lows=[item.low for item in history],
                        benchmark_closes=benchmark_series,
                        symbol=symbol,
                    )
                    if trend_result.get("regime") == "UNKNOWN":
                        rejected_signals.append(
                            {
                                "timestamp": bar.timestamp.isoformat(),
                                "symbol": symbol,
                                "reason": "trend_guard_unknown",
                                "strategy": strategy,
                            }
                        )

            open_layers = [layer for layer in layer_states.values() if layer.get("status") == "filled" and layer.get("exit_status") not in {"exited", "closed"}]
            time_stop_triggered = False
            if strategy == "c" and open_layers:
                for layer in open_layers:
                    strategy_counters["time_stop_evaluation_count"] += 1
                    entry_index = layer.get("entry_index")
                    entry_dt = self._coerce_trade_start_time(layer.get("entry_time"))
                    time_stop_result = time_stop.evaluate(
                        symbol=symbol,
                        entry_time=entry_dt,
                        current_time=bar.timestamp,
                        holding_bars=idx - int(entry_index if entry_index is not None else idx),
                        holding_minutes=int((bar.timestamp - entry_dt).total_seconds() // 60) if entry_dt else 0,
                        leveraged_etf=leveraged_etf,
                        configured_max_bars=int(parameter_set.get("time_stop_bars") or 20),
                        configured_max_minutes=int(parameter_set.get("time_stop_minutes") or 240),
                    )
                    if time_stop_result.get("triggered"):
                        time_stop_triggered = True
                        layer_id_for_event = int(layer.get("layer_id") or 0)
                        if layer_id_for_event not in time_stop_triggered_layers:
                            time_stop_triggered_layers.add(layer_id_for_event)
                            time_stop_event_id = f"time_stop:{symbol}:{layer_id_for_event or idx}"
                            strategy_counters["time_stop_signal_count"] += 1
                        break

            exit_order_created = False
            if current_position > 0 and not pending_sell:
                exit_plan = exit_manager.plan_exits(
                    filled_entry_layers=list(open_layers),
                    current_price=bar.close,
                    grid_width=float(range_snapshot.get("grid_width") or 0.0),
                    current_broker_position=current_position,
                    pending_sell_exists=pending_sell,
                )
                if exit_plan.get("allowed") and exit_plan.get("orders"):
                    exit_order = dict(exit_plan["orders"][0])
                    layer_id = int(exit_order.get("layer_id") or 0)
                    layer = layer_states.get(layer_id)
                    if layer and layer.get("status") == "filled":
                        qty = min(int(exit_order.get("sell_quantity") or 0), current_position)
                        if qty > 0:
                            order = self._make_layer_order(
                                bar=bar,
                                symbol=symbol,
                                side="SELL",
                                quantity=qty,
                                order_type="LIMIT",
                                limit_price=float(exit_order.get("target_price") or bar.close),
                                strategy=strategy,
                                layer_id=layer_id,
                                notes=exit_order.get("reason") or "layer_take_profit",
                            )
                            pending_orders.append(order)
                            portfolio.register_order(order)
                            orders.append(order)
                            exit_order_created = True

            buy_block_reason = "pending_buy_exists" if pending_buy else None

            if not exit_order_created and buy_block_reason is None and (current_position <= 0 or current_position_value < allowed_position_value):
                if strategy == "c":
                    if not trend_result.get("buy_allowed", False) or trend_result.get("regime") == "UNKNOWN":
                        rejected_signals.append(
                            {
                                "timestamp": bar.timestamp.isoformat(),
                                "symbol": symbol,
                                "reason": f"trend_guard_{str(trend_result.get('regime') or 'unknown').lower()}",
                                "strategy": strategy,
                            }
                        )
                        continue
                    if state_store_failed:
                        rejected_signals.append(
                            {
                                "timestamp": bar.timestamp.isoformat(),
                                "symbol": symbol,
                                "reason": "state_store_unavailable",
                                "strategy": strategy,
                            }
                        )
                        continue
                    if time_stop_triggered:
                        if time_stop_event_id and time_stop_event_id not in time_stop_blocked_events:
                            time_stop_blocked_events.add(time_stop_event_id)
                            strategy_counters["time_stop_blocked_count"] += 1
                        rejected_signals.append(
                            {
                                "timestamp": bar.timestamp.isoformat(),
                                "symbol": symbol,
                                "reason": "time_stop",
                                "event_id": time_stop_event_id or f"time_stop:{symbol}:unknown",
                                "strategy": strategy,
                            }
                        )
                        continue

                planner_existing = [
                    {"layer_id": layer_id, "status": layer.get("status")}
                    for layer_id, layer in layer_states.items()
                    if layer.get("status") in {"planned", "submitted", "filled", "exited"}
                ]
                total_target_quantity = determine_buy_quantity(
                    current_price=bar.close,
                    available_cash=float(portfolio.available_cash or 0.0),
                    configured_size=int(self.fixed_size or 0),
                    max_position=max_position,
                    execution_price=bar.open,
                    commission_per_share=self.commission_per_share,
                )
                if total_target_quantity <= 0:
                    rejected_signals.append(
                        {
                            "timestamp": bar.timestamp.isoformat(),
                            "symbol": symbol,
                            "reason": "quantity_zero",
                            "strategy": strategy,
                        }
                    )
                    continue

                if strategy == "c":
                    inventory_result = inventory_sizer.adjust_quantity(
                        base_quantity=total_target_quantity,
                        current_position_value=current_position_value,
                        allowed_position_value=allowed_position_value,
                        available_cash=float(portfolio.available_cash or 0.0),
                        current_price=bar.close,
                        leveraged_etf=leveraged_etf,
                        leveraged_etf_limit=float(parameter_set.get("leveraged_etf_limit") or 0.15),
                        cash_reserve_ratio=float(parameter_set.get("cash_reserve_ratio") or 0.2),
                    )
                    if not inventory_result.get("allowed"):
                        rejected_signals.append(
                            {
                                "timestamp": bar.timestamp.isoformat(),
                                "symbol": symbol,
                                "reason": str(inventory_result.get("reject_reason") or "inventory_limit"),
                                "strategy": strategy,
                            }
                        )
                        continue
                    total_target_quantity = int(inventory_result.get("adjusted_quantity") or 0)

                plan = entry_planner.plan_layers(
                    support=float(range_snapshot.get("support") or 0.0),
                    grid_width=float(range_snapshot.get("grid_width") or 0.0),
                    total_target_quantity=total_target_quantity,
                    max_layers=max_layers,
                    existing_layers=planner_existing,
                    pending_buy_exists=pending_buy,
                    inventory_ratio=float(current_position_value / allowed_position_value) if allowed_position_value > 0 else 0.0,
                    trend_buy_allowed=bool(trend_result.get("buy_allowed", True)),
                )
                if not plan.get("allowed") or not plan.get("layers"):
                    rejected_signals.append(
                        {
                            "timestamp": bar.timestamp.isoformat(),
                            "symbol": symbol,
                            "reason": str(plan.get("reject_reason") or "no_layers_generated"),
                            "strategy": strategy,
                        }
                    )
                    continue

                eligible_layers = [layer for layer in plan["layers"] if bar.close <= float(layer.get("trigger_price") or 0.0)]
                if not eligible_layers:
                    rejected_signals.append(
                        {
                            "timestamp": bar.timestamp.isoformat(),
                            "symbol": symbol,
                            "reason": "layer_trigger_not_reached",
                            "strategy": strategy,
                        }
                    )
                    continue
                layer = sorted(eligible_layers, key=lambda item: int(item.get("layer_id") or 0))[0]
                layer_id = int(layer.get("layer_id") or 0)
                if layer_id in layer_states and layer_states[layer_id].get("status") in {"submitted", "filled"}:
                    continue

                buy_quantity = int(layer.get("target_quantity") or 0)
                buy_limit_price = float(layer.get("trigger_price") or bar.close)
                if strategy == "c":
                    estimated_exit_price = float(range_snapshot.get("resistance") or (buy_limit_price + float(range_snapshot.get("grid_width") or 0.0)))
                    cost_result = cost_estimator.estimate(
                        entry_price=buy_limit_price,
                        exit_price=estimated_exit_price,
                        quantity=buy_quantity,
                        commission_per_share=self.commission_per_share,
                        platform_fee_per_trade=self.platform_fee_per_trade,
                        spread_pct=self._spread_pct_from_bar(bar),
                        slippage_pct=self.slippage_bps / 10000.0,
                        available_cash=float(portfolio.available_cash or 0.0),
                        minimum_net_profit=float(parameter_set.get("minimum_net_profit_pct") or 0.0),
                        max_spread_profit_ratio=float(parameter_set.get("max_spread_profit_ratio") or 0.5),
                    )
                    if not cost_result.get("allowed"):
                        rejected_signals.append(
                            {
                                "timestamp": bar.timestamp.isoformat(),
                                "symbol": symbol,
                                "reason": str(cost_result.get("reject_reason") or "cost_filter"),
                                "strategy": strategy,
                            }
                        )
                        continue

                order = self._make_layer_order(
                    bar=bar,
                    symbol=symbol,
                    side="BUY",
                    quantity=buy_quantity,
                    order_type="MARKET",
                    limit_price=buy_limit_price,
                    strategy=strategy,
                    layer_id=layer_id,
                    notes=layer.get("reason") or "layered_entry_plan",
                )
                pending_orders.append(order)
                portfolio.register_order(order)
                orders.append(order)
                layer_states[layer_id] = {
                    "layer_id": layer_id,
                    "trigger_price": buy_limit_price,
                    "planned_quantity": buy_quantity,
                    "filled_quantity": 0,
                    "average_fill_price": 0.0,
                    "exit_target": float(range_snapshot.get("resistance") or (buy_limit_price + float(range_snapshot.get("grid_width") or 0.0))),
                    "stop_price": max(0.01, round(buy_limit_price - float(range_snapshot.get("grid_width") or 0.0), 6)),
                    "status": "submitted",
                    "reason": layer.get("reason") or "layered_entry_plan",
                    "entry_time": None,
                    "entry_index": idx,
                    "exit_time": None,
                    "exit_status": "",
                    "exited_quantity": 0,
                    "last_update": bar.timestamp.isoformat(),
                }

            try:
                state_payload = self._layer_state_snapshot(
                    symbol=symbol,
                    strategy=strategy,
                    range_snapshot=range_snapshot,
                    layer_states=layer_states,
                    portfolio=portfolio,
                    trend_result=trend_result,
                    current_bar=bar,
                    allowed_position_value=allowed_position_value,
                    state_store_failed=state_store_failed,
                )
                state_store.save(symbol, state_payload)
            except Exception as exc:
                state_store_failed = True
                warnings.append(f"state_store_save_failed:{type(exc).__name__}")

            self._sync_layer_state_from_trades(layer_states, trades[last_trade_idx:], bar)
            if buy_block_reason is not None:
                rejected_signals.append(
                    {
                        "timestamp": bar.timestamp.isoformat(),
                        "symbol": symbol,
                        "reason": buy_block_reason,
                        "strategy": strategy,
                    }
                )

        if bars_list:
            portfolio.mark_to_market({symbol: bars_list[-1].close}, timestamp=bars_list[-1].timestamp)
        portfolio_snapshot = portfolio.snapshot(bars_list[-1].timestamp if bars_list else None)
        summary = {
            "symbol": symbol,
            "strategy": strategy,
            "benchmark_status": benchmark_status,
            "bars": len(bars_list),
            "filled_orders": len([t for t in trades if str(t.get("status") or "").upper() in {"FILLED", "PARTIALLY_FILLED"}]),
            "rejected_signals": len(rejected_signals),
            "ending_quantity": portfolio.position_quantity(symbol),
        }
        metrics = compute_backtest_metrics(
            initial_cash=initial_cash,
            equity_curve=portfolio.equity_curve,
            trades=trades,
            orders=orders,
            rejected_signals=rejected_signals,
            portfolio_snapshot=portfolio_snapshot,
            strategy_counters=strategy_counters,
        )
        result = BacktestResult(
            run_id=make_run_id(strategy, symbol, bars_list[0].timestamp.isoformat(), bars_list[-1].timestamp.isoformat()),
            strategy=strategy,
            symbol=symbol,
            data_start=bars_list[0].timestamp.isoformat(),
            data_end=bars_list[-1].timestamp.isoformat(),
            configuration=self._configuration_dict(parameter_set, initial_cash=initial_cash),
            summary=summary,
            metrics=metrics,
            trades=trades,
            orders=orders,
            equity_curve=portfolio.equity_curve,
            drawdown_curve=portfolio.drawdown_curve,
            rejected_signals=rejected_signals,
            warnings=warnings,
            parameter_set=parameter_set,
        )
        return result

    def _load_bars(self, bars: Sequence[Bar] | Iterable[Bar] | Any, symbol: str | None = None) -> list[Bar]:
        if isinstance(bars, list) and bars and isinstance(bars[0], Bar):
            return list(bars)
        loaded = self.data_feed.load(bars, symbol=symbol)
        return loaded

    def _coerce_trade_start_time(self, value: datetime | str | None) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        raw = str(value).strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _range_snapshot(self, strategy: str, history: Sequence[Bar], bar: Bar) -> dict[str, Any]:
        if strategy == "baseline":
            return _rolling_range(history, self.lookback)
        highs, lows, closes = _bar_history_to_series(history)
        return self.dynamic_range_calculator.calculate(
            timestamp=bar.timestamp,
            current_price=bar.close,
            highs=highs,
            lows=lows,
            closes=closes,
            atr_period=self.atr_period,
            ema_period=self.ema_period,
            rolling_lookback=self.rolling_lookback,
            atr_multiplier=self.atr_multiplier,
            minimum_range_pct=self.minimum_range_pct,
            maximum_range_pct=self.maximum_range_pct,
            support_buffer=self.support_buffer,
            resistance_buffer=self.resistance_buffer,
            warmup_bars=self.warmup_bars,
        )

    def _benchmark_bias(self, symbol: str, history: Sequence[Bar], benchmark_lookup: dict[datetime, float]) -> str | None:
        if not benchmark_lookup or symbol.upper().split(".")[0] != "SOXS":
            return None
        if len(history) < 5:
            return None
        closes = [benchmark_lookup.get(bar.timestamp) for bar in history if benchmark_lookup.get(bar.timestamp) is not None]
        if len(closes) < 5:
            return None
        first = closes[0]
        last = closes[-1]
        if first and last and ((last - first) / first) * 100.0 > 2.5:
            return "BLOCK_BUY"
        return None

    def _align_benchmark_bars(self, symbol_bars: Sequence[Bar], benchmark_bars: Sequence[Bar] | None) -> list[Bar]:
        if not benchmark_bars:
            return []
        symbol_timestamps = {bar.timestamp for bar in symbol_bars if getattr(bar, "timestamp", None)}
        if not symbol_timestamps:
            return []
        aligned = [bar for bar in benchmark_bars if getattr(bar, "timestamp", None) in symbol_timestamps]
        if not aligned:
            return []
        return aligned

    def _make_order(
        self,
        *,
        bar: Bar,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        trigger_price: float | None,
        strategy: str,
    ) -> dict[str, Any]:
        order_id = f"{symbol}-{bar.timestamp.isoformat()}-{side}-{len(bar.symbol)}-{len(strategy)}-{quantity}"
        order = BacktestOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            order_type=order_type,  # type: ignore[arg-type]
            quantity=int(quantity),
            limit_price=None,
            stop_price=None,
            submitted_at=bar.timestamp,
            eligible_at=bar.timestamp + timedelta(seconds=1),
            trigger_price=trigger_price,
            submitted_price=bar.close,
            requested_quantity=int(quantity),
            strategy_version=strategy,
        )
        return order.to_dict()

    def _make_layer_order(
        self,
        *,
        bar: Bar,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: float | None,
        strategy: str,
        layer_id: int,
        notes: str,
    ) -> dict[str, Any]:
        order = self._make_order(
            bar=bar,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            trigger_price=limit_price,
            strategy=strategy,
        )
        order["limit_price"] = round(float(limit_price), 6) if limit_price is not None else None
        order["layer_id"] = int(layer_id)
        order["notes"] = str(notes or "")
        return order

    def _sync_layer_state_from_trades(self, layer_states: dict[int, dict[str, Any]], trades: list[dict[str, Any]], bar: Bar) -> None:
        for trade in trades:
            try:
                layer_id = int(trade.get("layer_id") or 0)
            except (TypeError, ValueError):
                layer_id = 0
            if layer_id <= 0 or layer_id not in layer_states:
                continue
            layer = layer_states[layer_id]
            side = str(trade.get("side") or "").strip().upper()
            filled_qty = int(trade.get("filled_quantity") or trade.get("quantity") or 0)
            filled_price = float(trade.get("filled_price") or trade.get("price") or 0.0)
            if side == "BUY":
                layer["status"] = "filled"
                layer["filled_quantity"] = int(filled_qty)
                layer["average_fill_price"] = round(filled_price, 6)
                layer["entry_time"] = trade.get("filled_at") or trade.get("submitted_at") or bar.timestamp.isoformat()
                layer["entry_index"] = layer.get("entry_index") if layer.get("entry_index") is not None else 0
                layer["exit_status"] = ""
                layer["exited_quantity"] = 0
            elif side == "SELL":
                layer["exit_status"] = "exited"
                layer["exit_time"] = trade.get("filled_at") or trade.get("submitted_at") or bar.timestamp.isoformat()
                layer["exited_quantity"] = int(filled_qty)
                layer["status"] = "exited"
                layer["filled_quantity"] = max(0, int(layer.get("filled_quantity") or 0) - int(filled_qty))

    def _spread_pct_from_bar(self, bar: Bar) -> float:
        bid = getattr(bar, "bid", None)
        ask = getattr(bar, "ask", None)
        if bid in (None, "", 0) or ask in (None, "", 0) or bid <= 0 or ask <= 0:
            return 0.0
        midpoint = (float(bid) + float(ask)) / 2.0
        if midpoint <= 0:
            return 0.0
        return max(0.0, (float(ask) - float(bid)) / midpoint)

    def _layer_state_snapshot(
        self,
        *,
        symbol: str,
        strategy: str,
        range_snapshot: dict[str, Any],
        layer_states: dict[int, dict[str, Any]],
        portfolio: BacktestPortfolio,
        trend_result: dict[str, Any],
        current_bar: Bar,
        allowed_position_value: float,
        state_store_failed: bool,
    ) -> dict[str, Any]:
        return {
            "strategy_version": strategy,
            "symbol": symbol,
            "active_range": range_snapshot,
            "range_timestamp": current_bar.timestamp.isoformat(),
            "entry_layers": [layer for layer in layer_states.values()],
            "exit_layers": [layer for layer in layer_states.values() if layer.get("status") == "exited"],
            "realized_pnl": round(portfolio.realized_pnl, 6),
            "unrealized_pnl": round(portfolio.unrealized_pnl, 6),
            "inventory_ratio": round(
                (portfolio.position_value(symbol) / allowed_position_value) if allowed_position_value > 0 else 0.0,
                6,
            ),
            "trend_guard_state": trend_result,
            "symbol_reduce_only": bool(trend_result.get("symbol_reduce_only")),
            "last_buy_time": next((layer.get("entry_time") for layer in layer_states.values() if layer.get("status") == "filled"), None),
            "last_sell_time": next((layer.get("exit_time") for layer in layer_states.values() if layer.get("exit_status") == "exited"), None),
            "cooldown_until": trend_result.get("cooldown_until"),
            "last_reconciliation_time": current_bar.timestamp.isoformat(),
            "broker_position_snapshot": portfolio.snapshot(current_bar.timestamp),
            "state_version": 1,
            "state_store_failed": state_store_failed,
        }

    def _process_pending_orders(
        self,
        *,
        current_bar: Bar,
        pending_orders: list[dict[str, Any]],
        portfolio: BacktestPortfolio,
        orders: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        rejected_signals: list[dict[str, Any]],
    ) -> None:
        if not pending_orders:
            portfolio.mark_to_market({current_bar.symbol: current_bar.close}, timestamp=current_bar.timestamp)
            return
        still_pending: list[dict[str, Any]] = []
        for order in pending_orders:
            eligible_at = order.get("eligible_at")
            if eligible_at is not None:
                eligible_dt = self._coerce_trade_start_time(eligible_at)
                if eligible_dt and current_bar.timestamp < eligible_dt:
                    still_pending.append(order)
                    continue
            fill = self.execution_model.simulate_fill(
                order,
                current_bar,
                available_cash=float(portfolio.available_cash or 0.0),
                current_position=portfolio.position_quantity(order.get("symbol")),
            )
            if fill.get("status") == "PENDING":
                still_pending.append({**order, **fill})
                continue
            if fill.get("status") == "REJECTED":
                order.update(fill)
                rejected_signals.append(
                    {
                        "timestamp": current_bar.timestamp.isoformat(),
                        "symbol": str(order.get("symbol") or "").strip().upper(),
                        "reason": str(fill.get("reject_reason") or "execution_rejected"),
                        "strategy": str(order.get("strategy_version") or ""),
                        "order_id": str(order.get("order_id") or ""),
                    }
                )
                continue
            order.update(fill)
            portfolio.apply_fill(fill)
            trades.append(order)
        pending_orders[:] = still_pending
        portfolio.mark_to_market({current_bar.symbol: current_bar.close}, timestamp=current_bar.timestamp)

    def _configuration_dict(self, params: dict[str, Any], initial_cash: float | None = None) -> dict[str, Any]:
        return {
            "strategy": params.get("strategy", self.strategy),
            "initial_cash": float(initial_cash if initial_cash is not None else self.initial_cash),
            "max_position": self.max_position,
            "fixed_size": self.fixed_size,
            "entry_buffer_pct": self.entry_buffer_pct,
            "exit_buffer_pct": self.exit_buffer_pct,
            "lookback": self.lookback,
            "commission_per_share": self.commission_per_share,
            "platform_fee_per_trade": self.platform_fee_per_trade,
            "slippage_bps": self.slippage_bps,
            "participation_limit": self.participation_limit,
            "minimum_range_pct": self.minimum_range_pct,
            "maximum_range_pct": self.maximum_range_pct,
            "atr_period": self.atr_period,
            "ema_period": self.ema_period,
            "rolling_lookback": self.rolling_lookback,
            "atr_multiplier": self.atr_multiplier,
            "support_buffer": self.support_buffer,
            "resistance_buffer": self.resistance_buffer,
            "warmup_bars": self.warmup_bars,
            "max_entry_layers": int(params.get("max_entry_layers") or 5),
            "minimum_net_profit_pct": float(params.get("minimum_net_profit_pct") or 0.0),
            "max_spread_profit_ratio": float(params.get("max_spread_profit_ratio") or 0.5),
            "time_stop_bars": int(params.get("time_stop_bars") or 20),
            "time_stop_minutes": int(params.get("time_stop_minutes") or 240),
        }


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper()
