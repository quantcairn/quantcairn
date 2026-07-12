from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import exp, inf, log1p, sqrt
from statistics import mean, pstdev
from typing import Any, Sequence

from .accounting import build_trade_accounting


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_mean(values: Sequence[float]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return mean(values) if values else None


def _safe_pstdev(values: Sequence[float]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return pstdev(values) if len(values) >= 2 else None


def _round_or_none(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    if value == inf:
        return None
    return round(float(value), digits)


def _equity_returns(equity_curve: list[dict[str, Any]]) -> list[float]:
    returns: list[float] = []
    prev = None
    for point in equity_curve:
        value = point.get("equity") or point.get("total_equity")
        try:
            equity = float(value)
        except (TypeError, ValueError):
            continue
        if prev is not None and prev > 0:
            returns.append((equity - prev) / prev)
        prev = equity
    return returns


def _max_drawdown(equity_curve: list[dict[str, Any]]) -> tuple[float, list[float]]:
    peak = None
    max_dd = 0.0
    drawdowns: list[float] = []
    for point in equity_curve:
        try:
            equity = float(point.get("equity") or point.get("total_equity"))
        except (TypeError, ValueError):
            continue
        if peak is None or equity > peak:
            peak = equity
        dd = 0.0 if not peak else max(0.0, (peak - equity) / peak)
        drawdowns.append(dd)
        max_dd = max(max_dd, dd)
    return max_dd, drawdowns


def _annualization_factor(equity_curve: list[dict[str, Any]]) -> float:
    timestamps = [_parse_ts(point.get("timestamp")) for point in equity_curve]
    timestamps = [ts for ts in timestamps if ts is not None]
    if len(timestamps) < 2:
        return 252.0
    deltas = [
        (later - earlier).total_seconds()
        for earlier, later in zip(timestamps, timestamps[1:])
        if later > earlier
    ]
    if not deltas:
        return 252.0
    avg_seconds = sum(deltas) / len(deltas)
    if avg_seconds <= 0:
        return 252.0
    return (365.0 * 24.0 * 3600.0) / avg_seconds


def _holding_times(trades: list[dict[str, Any]]) -> list[float]:
    buys: dict[str, list[datetime]] = defaultdict(list)
    holding: list[float] = []
    for trade in trades:
        symbol = str(trade.get("symbol") or "").strip().upper()
        side = str(trade.get("side") or "").strip().upper()
        ts = _parse_ts(trade.get("timestamp") or trade.get("submitted_at") or trade.get("filled_at"))
        if ts is None or not symbol:
            continue
        if side == "BUY":
            buys[symbol].append(ts)
        elif side == "SELL" and buys[symbol]:
            entry = buys[symbol].pop(0)
            holding.append(max(0.0, (ts - entry).total_seconds()))
    return holding


def compute_backtest_metrics(
    *,
    initial_cash: float,
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    orders: list[dict[str, Any]] | None = None,
    rejected_signals: list[dict[str, Any]] | None = None,
    portfolio_snapshot: dict[str, Any] | None = None,
    strategy_counters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    orders = orders or []
    rejected_signals = rejected_signals or []
    strategy_counters = strategy_counters or {}
    equities = []
    for point in equity_curve:
        try:
            equities.append(float(point.get("equity") or point.get("total_equity")))
        except (TypeError, ValueError):
            continue
    ending_equity = equities[-1] if equities else float(initial_cash)
    total_return = (ending_equity - float(initial_cash)) / float(initial_cash) if initial_cash else 0.0
    max_dd, drawdowns = _max_drawdown(equity_curve)
    ann_factor = _annualization_factor(equity_curve)
    returns = _equity_returns(equity_curve)
    avg_return = _safe_mean(returns)
    std_return = _safe_pstdev(returns)
    downside = [r for r in returns if r < 0]
    downside_std = _safe_pstdev(downside)
    sharpe = None
    sortino = None
    if avg_return is not None and std_return not in (None, 0):
        sharpe = (avg_return / std_return) * sqrt(ann_factor)
    if avg_return is not None and downside_std not in (None, 0):
        sortino = (avg_return / downside_std) * sqrt(ann_factor)
    accounting = build_trade_accounting(
        initial_cash=initial_cash,
        trades=trades,
        orders=orders,
        portfolio_snapshot=portfolio_snapshot,
        rejected_signals=rejected_signals,
        strategy_counters=strategy_counters,
    )
    closed_trade_pnls = [float(pnl) for pnl in accounting.get("closed_trade_pnls") or []]
    win_pnls = [p for p in closed_trade_pnls if p > 0]
    loss_pnls = [p for p in closed_trade_pnls if p < 0]
    total_profit = sum(win_pnls)
    total_loss = sum(abs(p) for p in loss_pnls)
    profit_factor = None
    if total_loss > 0:
        profit_factor = total_profit / total_loss
    avg_win = _safe_mean(win_pnls)
    avg_loss = _safe_mean(loss_pnls)
    payoff_ratio = None
    if avg_win is not None and avg_loss not in (None, 0):
        payoff_ratio = abs(avg_win / avg_loss)
    win_rate = accounting.get("win_rate")
    average_holding_time = _safe_mean(_holding_times(trades))
    longest_losing_streak = 0
    current_streak = 0
    for pnl in closed_trade_pnls:
        if pnl < 0:
            current_streak += 1
            longest_losing_streak = max(longest_losing_streak, current_streak)
        else:
            current_streak = 0
    turnover_notional = float(accounting.get("turnover_notional") or 0.0)
    average_equity = _safe_mean(equities) or float(initial_cash)
    turnover = turnover_notional / average_equity if average_equity > 0 else 0.0
    exposure = None
    if average_equity > 0:
        max_equity = max(equities) if equities else float(initial_cash)
        min_equity = min(equities) if equities else float(initial_cash)
        exposure = (max_equity - min_equity) / average_equity
    return_drawdown_ratio = None
    if max_dd > 0:
        return_drawdown_ratio = total_return / max_dd
    rejected_reasons = accounting.get("rejected_reason_counts") or {}
    blocked_by_trend_count = sum(
        count for reason, count in rejected_reasons.items() if "trend" in reason or "regime" in reason
    )
    blocked_by_cost_count = sum(
        count for reason, count in rejected_reasons.items() if "cost" in reason or "spread" in reason or "profit" in reason
    )
    blocked_by_inventory_count = sum(
        count for reason, count in rejected_reasons.items() if "inventory" in reason
    )
    time_stop_count = int(accounting.get("time_stop_signal_count") or 0)
    annualized_return = total_return
    if equity_curve and len(equity_curve) > 1 and total_return > -1.0:
        periods = max(1.0, float(len(equity_curve) - 1))
        growth_factor = ann_factor / periods if periods > 0 else 0.0
        if 0.0 < growth_factor <= 100.0:
            try:
                annualized_return = exp(log1p(total_return) * growth_factor) - 1.0
            except OverflowError:
                annualized_return = total_return
    return {
        "initial_cash": round(float(initial_cash), 6),
        **{k: v for k, v in accounting.items() if k not in {"turnover_notional", "rejected_reason_counts"}},
        "ending_equity": round(float(accounting.get("ending_equity") or ending_equity), 6),
        "total_return": round(total_return, 6),
        "annualized_return": round(annualized_return, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe": _round_or_none(sharpe),
        "sortino": _round_or_none(sortino),
        "calmar": _round_or_none(total_return / max_dd if max_dd > 0 else None),
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "profit_factor": _round_or_none(profit_factor),
        "average_win": _round_or_none(avg_win),
        "average_loss": _round_or_none(avg_loss),
        "payoff_ratio": _round_or_none(payoff_ratio),
        "exposure": _round_or_none(exposure),
        "turnover": round(turnover, 6),
        "turnover_notional": round(turnover_notional, 6),
        "trade_count": len(trades),
        "fill_count": int(accounting.get("fill_count") or len(trades)),
        "order_count": int(accounting.get("order_count") or len(orders)),
        "trade_count_definition": accounting.get("trade_count_definition") or "fill_count",
        "round_trip_trade_count": int(accounting.get("round_trip_trade_count") or 0),
        "closed_trade_count": int(accounting.get("closed_trade_count") or 0),
        "open_position_count": int(accounting.get("open_position_count") or 0),
        "open_position_quantity": int(accounting.get("open_position_quantity") or 0),
        "winning_trade_count": int(accounting.get("winning_trade_count") or 0),
        "losing_trade_count": int(accounting.get("losing_trade_count") or 0),
        "average_holding_time": _round_or_none(average_holding_time),
        "longest_losing_streak": int(longest_losing_streak),
        "return_drawdown_ratio": _round_or_none(return_drawdown_ratio),
        "total_commission": round(sum(float(t.get("commission") or 0.0) for t in trades), 6),
        "total_slippage": round(sum(float(t.get("slippage") or 0.0) for t in trades), 6),
        "total_spread_cost": round(sum(float(t.get("spread_cost") or 0.0) for t in trades), 6),
        "total_fees": round(float(accounting.get("total_fees") or 0.0), 6),
        "slippage_cost": round(float(accounting.get("slippage_cost") or 0.0), 6),
        "spread_cost": round(float(accounting.get("spread_cost") or 0.0), 6),
        "ending_cash": accounting.get("ending_cash"),
        "position_market_value": accounting.get("position_market_value"),
        "realized_pnl": accounting.get("realized_pnl"),
        "unrealized_pnl": accounting.get("unrealized_pnl"),
        "expected_ending_equity": accounting.get("expected_ending_equity"),
        "reconciliation_difference": accounting.get("reconciliation_difference"),
        "reconciliation_status": accounting.get("reconciliation_status"),
        "reconciliation": {
            "basis": "cash_plus_mark_to_market",
            "initial_cash": round(float(initial_cash), 6),
            "ending_cash": accounting.get("ending_cash"),
            "position_market_value": accounting.get("position_market_value"),
            "realized_pnl": accounting.get("realized_pnl"),
            "unrealized_pnl": accounting.get("unrealized_pnl"),
            "total_fees": round(float(accounting.get("total_fees") or 0.0), 6),
            "slippage_cost": round(float(accounting.get("slippage_cost") or 0.0), 6),
            "spread_cost": round(float(accounting.get("spread_cost") or 0.0), 6),
            "ending_equity": round(float(accounting.get("ending_equity") or ending_equity), 6),
            "expected_ending_equity": accounting.get("expected_ending_equity"),
            "reconciliation_difference": accounting.get("reconciliation_difference"),
            "reconciliation_status": accounting.get("reconciliation_status"),
        },
        "rejected_order_count": int(sum(int(count) for count in rejected_reasons.values())),
        "blocked_by_trend_count": int(blocked_by_trend_count),
        "blocked_by_cost_count": int(blocked_by_cost_count),
        "blocked_by_inventory_count": int(blocked_by_inventory_count),
        "time_stop_evaluation_count": int(accounting.get("time_stop_evaluation_count") or 0),
        "time_stop_signal_count": int(accounting.get("time_stop_signal_count") or 0),
        "time_stop_exit_count": int(accounting.get("time_stop_exit_count") or 0),
        "time_stop_blocked_count": int(accounting.get("time_stop_blocked_count") or 0),
        "time_stop_count": int(time_stop_count),
        "no_trade": len(trades) == 0,
        "equity_points": len(equity_curve),
        "annualization_factor": round(ann_factor, 6),
        "layer_reconciliation": accounting.get("layer_reconciliation") or [],
    }
