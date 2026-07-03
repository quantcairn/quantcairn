from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..engine.trading_engine import TradingEngine, append_runtime_audit
from ..config.runtime_values import get_runtime_env
from .trade_audit import load_trade_records

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_DIR / "reports"
ORDER_ID_RE = re.compile(r'order_id:\s*"([^"]+)"')
SYMBOL_RE = re.compile(r'symbol:\s*"([A-Z.\-]+)"')
SIDE_RE = re.compile(r"side:\s*(Buy|Sell)", re.IGNORECASE)
EXEC_QTY_RE = re.compile(r"executed_quantity:\s*([0-9]+)")
EXEC_PRICE_RE = re.compile(r"executed_price:\s*Some\(([0-9.]+)\)")
SUBMITTED_AT_RE = re.compile(r'submitted_at:\s*"([^"]+)"')
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STARTED = False
TEST_ORDER_ID_RE = re.compile(r"^(?:TEST-.*|(?:BUY|SELL)-[0-9]+)$", re.IGNORECASE)


def _env(name: str, default: str = "") -> str:
    return get_runtime_env(name, default)


def _ny_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.utcnow()


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _report_path(trade_day: date, reports_dir: Path | None = None) -> Path:
    root = reports_dir or REPORTS_DIR
    return root / f"daily_{trade_day.isoformat()}.json"


def _parse_report_date(path: Path) -> date | None:
    stem = path.stem
    if not stem.startswith("daily_"):
        return None
    try:
        return date.fromisoformat(stem.removeprefix("daily_"))
    except ValueError:
        return None


def is_trading_day(trade_day: date) -> bool:
    if trade_day.weekday() >= 5:
        return False
    return trade_day not in TradingEngine._market_holidays(trade_day.year)


def latest_trading_day(reference_day: date | None = None) -> date:
    value = reference_day or _ny_now().date()
    while not is_trading_day(value):
        value -= timedelta(days=1)
    return value


def previous_trading_day(reference_day: date) -> date:
    value = reference_day - timedelta(days=1)
    while not is_trading_day(value):
        value -= timedelta(days=1)
    return value


def should_generate_daily_report(now_et: datetime | None = None, reports_dir: Path | None = None) -> bool:
    now_et = now_et or _ny_now()
    trade_day = now_et.date()
    if not is_trading_day(trade_day):
        return False
    if not (now_et.hour == 16 and now_et.minute == 5):
        return False
    return not _report_path(trade_day, reports_dir).exists()


def _coerce_float(value: Any) -> float | None:
    try:
        if value in (None, "", False):
            return None
        return float(value)
    except Exception:
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, "", False):
            return None
        return int(float(value))
    except Exception:
        return None


def _parse_string_order(value: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not isinstance(value, str):
        return payload
    order_id = ORDER_ID_RE.search(value)
    symbol = SYMBOL_RE.search(value)
    side = SIDE_RE.search(value)
    executed_quantity = EXEC_QTY_RE.search(value)
    executed_price = EXEC_PRICE_RE.search(value)
    submitted_at = SUBMITTED_AT_RE.search(value)
    if order_id:
        payload["order_id"] = order_id.group(1)
    if symbol:
        payload["symbol"] = symbol.group(1)
    if side:
        payload["side"] = side.group(1).upper()
    if executed_quantity:
        payload["executed_quantity"] = int(executed_quantity.group(1))
    if executed_price:
        payload["executed_price"] = float(executed_price.group(1))
    if submitted_at:
        payload["submitted_at"] = submitted_at.group(1)
    return payload


def _event_score(event: dict[str, Any]) -> int:
    return (
        (4 if event.get("avg_cost") is not None else 0)
        + (2 if event.get("order_id") else 0)
        + (1 if event.get("fill_price") is not None else 0)
    )


def _event_key(event: dict[str, Any]) -> str:
    if event.get("order_id"):
        return f"order:{event['order_id']}"
    return "|".join(
        [
            str(event.get("timestamp") or ""),
            str(event.get("symbol") or ""),
            str(event.get("side") or ""),
            str(event.get("quantity") or ""),
            str(event.get("fill_price") or ""),
        ]
    )


def _is_test_audit_record(record: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    phase = str(record.get("phase") or "").strip().lower()
    if phase not in {"risk_exit_trigger", "orphan_stop_loss"}:
        return False
    order = record.get("order") if isinstance(record.get("order"), dict) else {}
    order_id = str(record.get("order_id") or order.get("order_id") or "").strip()
    return bool(order_id and TEST_ORDER_ID_RE.fullmatch(order_id))


def _fill_event_from_runtime_record(record: dict[str, Any]) -> dict[str, Any] | None:
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    order = record.get("order") if isinstance(record.get("order"), dict) else {}
    status = str(response.get("status") or "").strip().lower()
    if status not in {"filled", "partially_filled", "partially-filled"}:
        return None
    side = str(order.get("side") or "").strip().upper()
    symbol = _normalize_symbol(record.get("symbol") or record.get("ticker"))
    quantity = _coerce_int(order.get("filled_qty") or order.get("qty") or record.get("quantity"))
    fill_price = _coerce_float(
        response.get("fill_price")
        or order.get("fill_price")
        or record.get("fill_price")
        or record.get("current_price")
    )
    if not symbol or side not in {"BUY", "SELL"} or not quantity or not fill_price:
        return None
    return {
        "order_id": str(record.get("order_id") or order.get("order_id") or "").strip() or None,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "fill_price": fill_price,
        "avg_cost": _coerce_float(record.get("avg_cost")),
        "timestamp": str(record.get("timestamp") or ""),
        "source": str(record.get("phase") or "runtime"),
    }


def _fill_event_from_get_order_record(record: dict[str, Any]) -> dict[str, Any] | None:
    if str(record.get("action") or "").strip().lower() != "get_order":
        return None
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    mapped_status = str(response.get("mapped_status") or "").strip().upper()
    if mapped_status not in {"FILLED", "PARTIALLY_FILLED"}:
        return None
    raw_order = response.get("order")
    parsed = raw_order if isinstance(raw_order, dict) else _parse_string_order(str(raw_order or ""))
    if not parsed:
        return None
    side = str(parsed.get("side") or "").strip().upper()
    symbol = _normalize_symbol(parsed.get("symbol") or record.get("ticker") or record.get("symbol"))
    quantity = _coerce_int(parsed.get("executed_quantity") or parsed.get("filled_quantity") or parsed.get("quantity"))
    fill_price = _coerce_float(parsed.get("executed_price") or parsed.get("avg_fill_price") or parsed.get("price"))
    if not symbol or side not in {"BUY", "SELL"} or not quantity or not fill_price:
        return None
    return {
        "order_id": str(parsed.get("order_id") or record.get("request", {}).get("order_id") or "").strip() or None,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "fill_price": fill_price,
        "avg_cost": None,
        "timestamp": str(record.get("timestamp") or parsed.get("submitted_at") or ""),
        "source": "broker_get_order",
    }


def extract_fill_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        if _is_test_audit_record(record):
            continue
        candidates = [
            _fill_event_from_runtime_record(record),
            _fill_event_from_get_order_record(record),
        ]
        for event in candidates:
            if not event:
                continue
            key = _event_key(event)
            current = deduped.get(key)
            if current is None or _event_score(event) >= _event_score(current):
                deduped[key] = event
    return sorted(deduped.values(), key=lambda item: str(item.get("timestamp") or ""))


def _compute_realized_pnl(fill_events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    warnings: list[str] = []
    by_symbol: dict[str, float | None] = {}
    total: float | None = 0.0
    profitable_trades = 0
    losing_trades = 0
    open_lots: dict[str, deque[tuple[int, float]]] = defaultdict(deque)

    for event in fill_events:
        symbol = _normalize_symbol(event.get("symbol"))
        side = str(event.get("side") or "").strip().upper()
        quantity = int(event.get("quantity") or 0)
        fill_price = float(event.get("fill_price") or 0.0)
        avg_cost = _coerce_float(event.get("avg_cost"))
        if not symbol or quantity <= 0 or fill_price <= 0:
            continue

        if symbol not in by_symbol:
            by_symbol[symbol] = 0.0

        if side == "BUY":
            open_lots[symbol].append((quantity, fill_price))
            continue

        if side != "SELL":
            continue

        event_pnl: float | None = None
        if avg_cost is not None and avg_cost > 0:
            event_pnl = (fill_price - avg_cost) * quantity
        else:
            remaining = quantity
            matched_cost = 0.0
            while remaining > 0 and open_lots[symbol]:
                lot_qty, lot_price = open_lots[symbol][0]
                consumed = min(remaining, lot_qty)
                matched_cost += consumed * lot_price
                remaining -= consumed
                lot_qty -= consumed
                if lot_qty <= 0:
                    open_lots[symbol].popleft()
                else:
                    open_lots[symbol][0] = (lot_qty, lot_price)
            if remaining == 0:
                event_pnl = (fill_price * quantity) - matched_cost

        if event_pnl is None:
            by_symbol[symbol] = None
            total = None
            if "incomplete_fill_data" not in warnings:
                warnings.append("incomplete_fill_data")
            continue

        if by_symbol[symbol] is not None:
            by_symbol[symbol] = float(by_symbol[symbol] or 0.0) + event_pnl
        if total is not None:
            total += event_pnl
        if event_pnl > 0:
            profitable_trades += 1
        elif event_pnl < 0:
            losing_trades += 1

    realized = {
        "by_symbol": {
            symbol: (round(value, 2) if isinstance(value, (int, float)) else None)
            for symbol, value in sorted(by_symbol.items())
        },
        "total": round(total, 2) if isinstance(total, (int, float)) else None,
    }
    trade_summary = {
        "total_trades_today": len(fill_events),
        "profitable_trades": profitable_trades,
        "losing_trades": losing_trades,
        "win_rate": None,
    }
    completed = profitable_trades + losing_trades
    if len(fill_events) == 0:
        warnings.append("no_trades_today")
    elif completed == 0:
        warnings.append("win_rate_unavailable")
    else:
        trade_summary["win_rate"] = round((profitable_trades / completed) * 100.0, 2)
    return realized, trade_summary, warnings


def _current_holdings(positions: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_symbol: dict[str, float] = {}
    total = 0.0
    for pos in positions or []:
        symbol = _normalize_symbol(getattr(pos, "ticker", ""))
        quantity = int(getattr(pos, "quantity", 0) or 0)
        if not symbol or quantity <= 0:
            continue
        avg_cost = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
        current_price = float(getattr(pos, "current_price", 0.0) or 0.0)
        market_value = float(getattr(pos, "market_value", 0.0) or 0.0)
        unrealized_pnl = float(getattr(pos, "unrealized_pnl", 0.0) or 0.0)
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "avg_cost": round(avg_cost, 2),
                "current_price": round(current_price, 2),
                "market_value": round(market_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
            }
        )
        by_symbol[symbol] = round(unrealized_pnl, 2)
        total += unrealized_pnl
    rows.sort(key=lambda item: item["symbol"])
    return rows, {"by_symbol": by_symbol, "total": round(total, 2)}


def _load_previous_report(trade_day: date, reports_dir: Path | None = None) -> dict[str, Any] | None:
    path = _report_path(previous_trading_day(trade_day), reports_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _build_live_broker():
    from ..broker.longbridge_broker import LongBridgeBroker

    return LongBridgeBroker(
        app_key=_env("LONGBRIDGE_APP_KEY") or _env("LONGBRIDGE_API_KEY"),
        app_secret=_env("LONGBRIDGE_APP_SECRET") or _env("LONGBRIDGE_API_SECRET"),
        access_token=_env("LONGBRIDGE_ACCESS_TOKEN"),
        region=_env("LONGBRIDGE_REGION", "cn"),
        environment=_env("LONGBRIDGE_ENV", "prod"),
        http_url=_env("LONGBRIDGE_HTTP_URL") or _env("LONGBRIDGE_BASE_URL"),
        quote_ws_url=_env("LONGBRIDGE_QUOTE_WS_URL"),
        trade_ws_url=_env("LONGBRIDGE_TRADE_WS_URL"),
        log_path=_env("LONGBRIDGE_LOG_PATH"),
    )


def generate_daily_report(
    trade_day: date | None = None,
    *,
    broker=None,
    reports_dir: Path | None = None,
    log_dir: Path | None = None,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    now_et = now_et or _ny_now()
    trade_day = trade_day or latest_trading_day(now_et.date())
    root = reports_dir or REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    close_broker = False
    if broker is None:
        broker = _build_live_broker()
        close_broker = True

    try:
        if close_broker and not broker.connect():
            raise RuntimeError("broker_connect_failed")

        positions = broker.get_positions() or []
        positions_reliable = getattr(
            broker, "is_positions_snapshot_reliable", lambda: True
        )()
        if not positions_reliable:
            raise RuntimeError("broker_positions_unverified")
        account = broker.get_account()
        account_reliable = getattr(
            broker, "is_account_snapshot_reliable", lambda: True
        )()
        if not account_reliable:
            raise RuntimeError("broker_account_unverified")
        records = load_trade_records(log_dir=log_dir or (PROJECT_DIR / "logs"), day=trade_day.strftime("%Y%m%d"))
        if any(_is_test_audit_record(record) for record in records):
            warnings.append("test_audit_events_ignored")
        fill_events = extract_fill_events(records)
        realized_pnl, trade_stats, realized_warnings = _compute_realized_pnl(fill_events)
        warnings.extend(realized_warnings)
        holdings, unrealized_pnl = _current_holdings(positions)

        previous_report = _load_previous_report(trade_day, root)
        equity = float(getattr(account, "equity", 0.0) or 0.0)
        cash = float(getattr(account, "cash", 0.0) or 0.0)
        buying_power = float(getattr(account, "buying_power", 0.0) or 0.0)
        if previous_report is None:
            equity_change_vs_yesterday = None
            warnings.append("previous_daily_report_missing")
        else:
            previous_equity = _coerce_float(((previous_report.get("account") or {}).get("equity")))
            equity_change_vs_yesterday = (
                round(equity - previous_equity, 2) if previous_equity is not None else None
            )
            if previous_equity is None:
                warnings.append("previous_daily_report_missing")

        report = {
            "date": trade_day.isoformat(),
            "generated_at": now_et.isoformat(),
            "account": {
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "buying_power": round(buying_power, 2),
                "equity_change_vs_yesterday": equity_change_vs_yesterday,
            },
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "trades": trade_stats,
            "current_holdings": holdings,
            "warnings": list(dict.fromkeys(warnings)),
        }

        path = _report_path(trade_day, root)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        append_runtime_audit(
            {
                "phase": "daily_report_generated",
                "execution_mode": "live",
                "report_path": str(path),
                "realized_pnl_total": report["realized_pnl"]["total"],
                "unrealized_pnl_total": report["unrealized_pnl"]["total"],
                "equity": report["account"]["equity"],
                "warnings": report["warnings"],
            }
        )
        return report
    finally:
        if close_broker:
            try:
                broker.disconnect()
            except Exception:
                pass


def latest_daily_report_response(
    *,
    reports_dir: Path | None = None,
    now_et: datetime | None = None,
) -> tuple[dict[str, Any], int]:
    root = reports_dir or REPORTS_DIR
    if not root.exists():
        return {"status": "no_report_available", "reports_dir": str(root)}, 404

    report_files = sorted(root.glob("daily_*.json"))
    if not report_files:
        return {"status": "no_report_available", "reports_dir": str(root)}, 404

    latest_path = report_files[-1]
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "status": "no_report_available",
            "reports_dir": str(root),
            "warning": "latest_report_invalid",
        }, 404

    expected_day = latest_trading_day((now_et or _ny_now()).date())
    report_day = _parse_report_date(latest_path)
    payload["status"] = "ok"
    payload["report_path"] = str(latest_path)
    payload["is_latest_trading_day_report"] = bool(report_day == expected_day)
    return payload, 200


def _scheduler_loop() -> None:
    while True:
        try:
            now_et = _ny_now()
            if should_generate_daily_report(now_et):
                generate_daily_report(now_et=now_et)
        except Exception as exc:
            logger.error("Daily report scheduler failed: %s", exc)
        time.sleep(30)


def ensure_daily_report_scheduler() -> None:
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return
        thread = threading.Thread(target=_scheduler_loop, name="daily-report-scheduler", daemon=True)
        thread.start()
        _SCHEDULER_STARTED = True
