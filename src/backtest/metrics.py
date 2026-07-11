from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import exp, inf, log1p, sqrt
from statistics import mean, pstdev
from typing import Any, Sequence


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


def _trade_pnl_sequence(trades: list[dict[str, Any]]) -> list[float]:
    pnl_by_symbol: dict[str, list[tuple[int, float]]] = defaultdict(list)
    realized: list[float] = []
    for trade in trades:
        symbol = str(trade.get("symbol") or "").strip().upper()
        side = str(trade.get("side") or "").strip().upper()
        qty = int(trade.get("filled_quantity") or trade.get("quantity") or 0)
        price = float(trade.get("filled_price") or trade.get("price") or 0.0)
        if not symbol or qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            pnl_by_symbol[symbol].append((qty, price))
        elif side == "SELL":
            remaining = qty
            while remaining > 0 and pnl_by_symbol[symbol]:
                lot_qty, lot_price = pnl_by_symbol[symbol][0]
                matched = min(remaining, lot_qty)
                realized.append((price - lot_price) * matched)
                if matched == lot_qty:
                    pnl_by_symbol[symbol].pop(0)
                else:
                    pnl_by_symbol[symbol][0] = (lot_qty - matched, lot_price)
                remaining -= matched
    return realized


def compute_backtest_metrics(
    *,
    initial_cash: float,
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    orders: list[dict[str, Any]] | None = None,
    rejected_signals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    orders = orders or []
    rejected_signals = rejected_signals or []
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
    total_profit = 0.0
    total_loss = 0.0
    trade_pnls = _trade_pnl_sequence(trades)
    win_pnls = [p for p in trade_pnls if p > 0]
    loss_pnls = [p for p in trade_pnls if p < 0]
    for pnl in trade_pnls:
        if pnl > 0:
            total_profit += pnl
        elif pnl < 0:
            total_loss += abs(pnl)
    profit_factor = None
    if total_loss > 0:
        profit_factor = total_profit / total_loss
    elif total_profit > 0:
        profit_factor = None
    avg_win = _safe_mean(win_pnls)
    avg_loss = _safe_mean(loss_pnls)
    payoff_ratio = None
    if avg_win is not None and avg_loss not in (None, 0):
        payoff_ratio = abs(avg_win / avg_loss)
    win_rate = len(win_pnls) / len(trade_pnls) if trade_pnls else 0.0
    average_holding_time = _safe_mean(_holding_times(trades))
    longest_losing_streak = 0
    current_streak = 0
    for pnl in trade_pnls:
        if pnl < 0:
            current_streak += 1
            longest_losing_streak = max(longest_losing_streak, current_streak)
        else:
            current_streak = 0
    turnover = 0.0
    for trade in trades:
        try:
            qty = abs(float(trade.get("filled_quantity") or trade.get("quantity") or 0))
            price = abs(float(trade.get("filled_price") or trade.get("price") or 0))
        except (TypeError, ValueError):
            continue
        turnover += qty * price
    average_equity = _safe_mean(equities) or float(initial_cash)
    exposure = None
    if average_equity > 0:
        max_equity = max(equities) if equities else float(initial_cash)
        min_equity = min(equities) if equities else float(initial_cash)
        exposure = (max_equity - min_equity) / average_equity
    return_drawdown_ratio = None
    if max_dd > 0:
        return_drawdown_ratio = total_return / max_dd
    rejected_reasons = defaultdict(int)
    for item in rejected_signals:
        rejected_reasons[str(item.get("reason") or item.get("reject_reason") or "unknown")] += 1
    blocked_by_trend_count = sum(
        count for reason, count in rejected_reasons.items() if "trend" in reason or "regime" in reason
    )
    blocked_by_cost_count = sum(
        count for reason, count in rejected_reasons.items() if "cost" in reason or "spread" in reason or "profit" in reason
    )
    blocked_by_inventory_count = sum(
        count for reason, count in rejected_reasons.items() if "inventory" in reason
    )
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
        "ending_equity": round(ending_equity, 6),
        "total_return": round(total_return, 6),
        "annualized_return": round(annualized_return, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe": _round_or_none(sharpe),
        "sortino": _round_or_none(sortino),
        "calmar": _round_or_none(total_return / max_dd if max_dd > 0 else None),
        "win_rate": round(win_rate, 6),
        "profit_factor": _round_or_none(profit_factor),
        "average_win": _round_or_none(avg_win),
        "average_loss": _round_or_none(avg_loss),
        "payoff_ratio": _round_or_none(payoff_ratio),
        "exposure": _round_or_none(exposure),
        "turnover": round(turnover, 6),
        "trade_count": len(trades),
        "average_holding_time": _round_or_none(average_holding_time),
        "longest_losing_streak": int(longest_losing_streak),
        "return_drawdown_ratio": _round_or_none(return_drawdown_ratio),
        "total_commission": round(sum(float(t.get("commission") or 0.0) for t in trades), 6),
        "total_slippage": round(sum(float(t.get("slippage") or 0.0) for t in trades), 6),
        "total_spread_cost": round(
            sum(float(t.get("fees") or 0.0) for t in trades), 6
        ),
        "rejected_order_count": len(rejected_signals),
        "blocked_by_trend_count": int(blocked_by_trend_count),
        "blocked_by_cost_count": int(blocked_by_cost_count),
        "blocked_by_inventory_count": int(blocked_by_inventory_count),
        "no_trade": len(trades) == 0,
        "equity_points": len(equity_curve),
        "annualization_factor": round(ann_factor, 6),
    }
