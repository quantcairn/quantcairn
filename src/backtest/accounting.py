from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean
from typing import Any


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _trade_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[datetime, int]:
    index, trade = item
    timestamp = (
        _parse_ts(trade.get("filled_at"))
        or _parse_ts(trade.get("timestamp"))
        or _parse_ts(trade.get("submitted_at"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return timestamp, index


@dataclass(slots=True)
class _OpenLot:
    quantity: int
    original_quantity: int
    price: float
    commission: float
    fees: float
    slippage: float
    timestamp: str | None
    layer_id: int | None

    @property
    def remaining_quantity(self) -> int:
        return self.quantity


def _layer_reconciliation_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    layer_map: dict[tuple[str, int], dict[str, Any]] = {}
    for trade in trades:
        symbol = _normalize(trade.get("symbol"))
        try:
            layer_id = int(trade.get("layer_id") or 0)
        except (TypeError, ValueError):
            layer_id = 0
        if not symbol or layer_id <= 0:
            continue
        side = _normalize(trade.get("side"))
        qty = max(0, _safe_int(trade.get("filled_quantity") or trade.get("quantity")))
        price = _safe_float(trade.get("filled_price") or trade.get("price"))
        commission = _safe_float(trade.get("commission"))
        fees = _safe_float(trade.get("fees"))
        row = layer_map.setdefault(
            (symbol, layer_id),
            {
                "symbol": symbol,
                "layer_id": layer_id,
                "buy_quantity": 0,
                "buy_notional": 0.0,
                "buy_commission": 0.0,
                "buy_fees": 0.0,
                "sell_quantity": 0,
                "sell_notional": 0.0,
                "sell_commission": 0.0,
                "sell_fees": 0.0,
                "first_timestamp": None,
                "last_timestamp": None,
            },
        )
        ts = trade.get("filled_at") or trade.get("submitted_at") or trade.get("timestamp")
        if row["first_timestamp"] is None or str(ts or "") < str(row["first_timestamp"]):
            row["first_timestamp"] = ts
        if row["last_timestamp"] is None or str(ts or "") > str(row["last_timestamp"]):
            row["last_timestamp"] = ts
        if qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            row["buy_quantity"] += qty
            row["buy_notional"] += qty * price
            row["buy_commission"] += commission
            row["buy_fees"] += fees
        elif side == "SELL":
            row["sell_quantity"] += qty
            row["sell_notional"] += qty * price
            row["sell_commission"] += commission
            row["sell_fees"] += fees

    rows: list[dict[str, Any]] = []
    for row in sorted(layer_map.values(), key=lambda item: (item["symbol"], item["layer_id"])):
        buy_qty = int(row["buy_quantity"])
        sell_qty = int(row["sell_quantity"])
        remaining = max(0, buy_qty - sell_qty)
        realized_pnl = (row["sell_notional"] - row["buy_notional"]) - (row["buy_commission"] + row["buy_fees"] + row["sell_commission"] + row["sell_fees"])
        if buy_qty == 0 and sell_qty == 0:
            status = "empty"
        elif remaining > 0 and sell_qty > 0:
            status = "partial"
        elif remaining > 0:
            status = "open"
        elif buy_qty > 0 and sell_qty > 0:
            status = "closed"
        else:
            status = "unknown"
        rows.append(
            {
                "symbol": row["symbol"],
                "layer_id": row["layer_id"],
                "buy_quantity": buy_qty,
                "buy_price": round(row["buy_notional"] / buy_qty, 6) if buy_qty > 0 else None,
                "sell_quantity": sell_qty,
                "sell_price": round(row["sell_notional"] / sell_qty, 6) if sell_qty > 0 else None,
                "realized_pnl": round(realized_pnl, 6),
                "fees": round(row["buy_commission"] + row["buy_fees"] + row["sell_commission"] + row["sell_fees"], 6),
                "remaining_quantity": remaining,
                "status": status,
                "first_timestamp": row["first_timestamp"],
                "last_timestamp": row["last_timestamp"],
            }
        )
    return rows


def build_trade_accounting(
    *,
    initial_cash: float,
    trades: list[dict[str, Any]],
    orders: list[dict[str, Any]] | None = None,
    portfolio_snapshot: dict[str, Any] | None = None,
    rejected_signals: list[dict[str, Any]] | None = None,
    strategy_counters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    orders = orders or []
    rejected_signals = rejected_signals or []
    strategy_counters = strategy_counters or {}
    ordered_trades = sorted(enumerate(trades), key=_trade_sort_key)

    turnover_notional = 0.0
    commissions = 0.0
    platform_fees = 0.0
    regulatory_fees = 0.0
    slippage_cost = 0.0
    spread_cost = 0.0
    rejected_reasons = defaultdict(int)
    seen_rejections: set[tuple[str, str]] = set()
    for item in rejected_signals:
        reason = str(item.get("reason") or item.get("reject_reason") or "unknown")
        event_id = str(item.get("event_id") or item.get("timestamp") or item.get("order_id") or reason)
        key = (reason, event_id)
        if key in seen_rejections:
            continue
        seen_rejections.add(key)
        rejected_reasons[reason] += 1

    lot_books: dict[str, deque[_OpenLot]] = defaultdict(deque)
    closed_trade_rows: list[dict[str, Any]] = []
    for _, trade in ordered_trades:
        symbol = _normalize(trade.get("symbol"))
        side = _normalize(trade.get("side"))
        qty = max(0, _safe_int(trade.get("filled_quantity") or trade.get("quantity")))
        price = _safe_float(trade.get("filled_price") or trade.get("price"))
        commission = _safe_float(trade.get("commission"))
        fees = _safe_float(trade.get("fees"))
        slippage = _safe_float(trade.get("slippage"))
        spread = _safe_float(trade.get("spread_cost"))
        turnover_notional += abs(qty * price)
        commissions += commission
        platform_fees += fees
        slippage_cost += slippage
        spread_cost += spread
        if not symbol or qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            lot_books[symbol].append(
                _OpenLot(
                    quantity=qty,
                    original_quantity=qty,
                    price=price,
                    commission=commission,
                    fees=fees,
                    slippage=slippage,
                    timestamp=trade.get("filled_at") or trade.get("submitted_at") or trade.get("timestamp"),
                    layer_id=_safe_int(trade.get("layer_id")) or None,
                )
            )
            continue
        if side != "SELL":
            continue
        remaining = qty
        sell_cost_total = commission + fees
        while remaining > 0 and lot_books[symbol]:
            lot = lot_books[symbol][0]
            matched = min(remaining, lot.quantity)
            if matched <= 0:
                break
            buy_alloc = (lot.commission + lot.fees) * (matched / lot.original_quantity) if lot.original_quantity > 0 else 0.0
            sell_alloc = sell_cost_total * (matched / qty) if qty > 0 else 0.0
            net_pnl = (price - lot.price) * matched - buy_alloc - sell_alloc
            closed_trade_rows.append(
                {
                    "symbol": symbol,
                    "side": "ROUND_TRIP",
                    "buy_quantity": matched,
                    "buy_price": round(lot.price, 6),
                    "sell_quantity": matched,
                    "sell_price": round(price, 6),
                    "realized_pnl": round(net_pnl, 6),
                    "fees": round(buy_alloc + sell_alloc, 6),
                    "remaining_quantity": max(0, lot.quantity - matched),
                    "status": "closed" if matched == lot.quantity else "partial",
                    "layer_id": lot.layer_id,
                    "buy_timestamp": lot.timestamp,
                    "sell_timestamp": trade.get("filled_at") or trade.get("submitted_at") or trade.get("timestamp"),
                }
            )
            lot.quantity -= matched
            remaining -= matched
            if lot.quantity <= 0:
                lot_books[symbol].popleft()
            else:
                lot_books[symbol][0] = lot

    closed_trade_pnls = [float(row["realized_pnl"]) for row in closed_trade_rows]
    winning_trade_count = sum(1 for pnl in closed_trade_pnls if pnl > 0)
    losing_trade_count = sum(1 for pnl in closed_trade_pnls if pnl < 0)
    closed_trade_count = len(closed_trade_pnls)
    round_trip_trade_count = closed_trade_count
    total_profit = sum(pnl for pnl in closed_trade_pnls if pnl > 0)
    total_loss = sum(abs(pnl) for pnl in closed_trade_pnls if pnl < 0)
    win_rate = round(winning_trade_count / closed_trade_count, 6) if closed_trade_count else None
    profit_factor = round(total_profit / total_loss, 6) if total_loss > 0 else None
    profit_factor_status = "NO_CLOSED_TRADES" if closed_trade_count == 0 else ("NO_LOSSES" if total_loss == 0 and total_profit > 0 else "OK")

    average_win = round(mean([pnl for pnl in closed_trade_pnls if pnl > 0]), 6) if any(pnl > 0 for pnl in closed_trade_pnls) else None
    average_loss = round(mean([pnl for pnl in closed_trade_pnls if pnl < 0]), 6) if any(pnl < 0 for pnl in closed_trade_pnls) else None

    open_position_count = sum(1 for lots in lot_books.values() if sum(lot.quantity for lot in lots) > 0)
    open_position_quantity = sum(sum(lot.quantity for lot in lots) for lots in lot_books.values())
    layer_reconciliation = _layer_reconciliation_rows(trades)

    ending_cash = None
    position_market_value = None
    realized_pnl = None
    unrealized_pnl = None
    ending_equity = None
    if portfolio_snapshot is not None:
        ending_cash = _safe_float(portfolio_snapshot.get("available_cash"))
        position_market_value = 0.0
        positions = portfolio_snapshot.get("positions") or {}
        if isinstance(positions, dict):
            for position in positions.values():
                if isinstance(position, dict):
                    position_market_value += _safe_float(position.get("market_value"))
        realized_pnl = _safe_float(portfolio_snapshot.get("realized_pnl"))
        unrealized_pnl = _safe_float(portfolio_snapshot.get("unrealized_pnl"))
        ending_equity = _safe_float(portfolio_snapshot.get("total_equity"))
    total_fees = round(commissions + platform_fees + regulatory_fees, 6)
    expected_ending_equity = None
    reconciliation_difference = None
    reconciliation_status = "UNKNOWN"
    if ending_equity is not None:
        expected_ending_equity = round(float(initial_cash) + float(realized_pnl or 0.0) + float(unrealized_pnl or 0.0) - total_fees, 6)
        reconciliation_difference = round(float(ending_equity) - expected_ending_equity, 6)
        reconciliation_status = "OK" if abs(reconciliation_difference) <= 0.05 else "MISMATCH"

    return {
        "trade_count_definition": "fill_count",
        "order_count": len(orders),
        "fill_count": len(trades),
        "trade_count": len(trades),
        "round_trip_trade_count": round_trip_trade_count,
        "closed_trade_count": closed_trade_count,
        "open_position_count": open_position_count,
        "open_position_quantity": open_position_quantity,
        "winning_trade_count": winning_trade_count,
        "losing_trade_count": losing_trade_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "profit_factor_status": profit_factor_status,
        "average_win": average_win,
        "average_loss": average_loss,
        "closed_trade_pnls": [round(pnl, 6) for pnl in closed_trade_pnls],
        "layer_reconciliation": layer_reconciliation,
        "ending_cash": round(ending_cash, 6) if ending_cash is not None else None,
        "position_market_value": round(position_market_value, 6) if position_market_value is not None else None,
        "realized_pnl": round(realized_pnl, 6) if realized_pnl is not None else None,
        "unrealized_pnl": round(unrealized_pnl, 6) if unrealized_pnl is not None else None,
        "total_fees": total_fees,
        "slippage_cost": round(slippage_cost, 6),
        "spread_cost": round(spread_cost, 6),
        "ending_equity": round(ending_equity, 6) if ending_equity is not None else round(float(initial_cash), 6),
        "expected_ending_equity": expected_ending_equity,
        "reconciliation_difference": reconciliation_difference,
        "reconciliation_status": reconciliation_status,
        "turnover_notional": round(turnover_notional, 6),
        "rejected_reason_counts": dict(sorted(rejected_reasons.items(), key=lambda item: (-item[1], item[0]))),
        "time_stop_evaluation_count": int(strategy_counters.get("time_stop_evaluation_count", 0) or 0),
        "time_stop_signal_count": int(strategy_counters.get("time_stop_signal_count", 0) or 0),
        "time_stop_exit_count": int(strategy_counters.get("time_stop_exit_count", 0) or 0),
        "time_stop_blocked_count": int(strategy_counters.get("time_stop_blocked_count", 0) or 0),
    }
