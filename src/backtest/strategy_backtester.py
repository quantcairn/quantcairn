from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import floor
from typing import Any, Iterable, Sequence

from ..engine.position_sizing import determine_buy_quantity
from ..strategy.dynamic_range import DynamicRangeCalculator
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
        if strategy not in {"baseline", "a", "version_a"}:
            raise ValueError(f"Unsupported strategy: {strategy}")
        allow_trade_after = self._coerce_trade_start_time(trade_start_time)
        portfolio = BacktestPortfolio(initial_cash=effective_initial_cash)
        pending_orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        rejected_signals: list[dict[str, Any]] = []
        benchmark_lookup = {bar.timestamp: bar.close for bar in (benchmark_bars or []) if getattr(bar, "timestamp", None)}
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

        summary = {
            "symbol": symbol,
            "strategy": strategy,
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
        }


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper()
