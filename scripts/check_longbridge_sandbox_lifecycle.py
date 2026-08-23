#!/usr/bin/env python3
"""Longbridge sandbox order lifecycle diagnostic.

This script is intentionally read-only with respect to strategy/runtime state
until the user explicitly runs it in sandbox mode. It performs a single-share
buy/sell lifecycle against the configured sandbox paper/demo account and
prints a structured test report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.config.runtime_paths import resolve_state_dir

LIFECYCLE_STATE_PATH = resolve_state_dir(PROJECT_DIR) / "lifecycle" / "longbridge_sandbox_lifecycle.json"


def _lifecycle_state_path() -> Path:
    return resolve_state_dir(PROJECT_DIR) / "lifecycle" / "longbridge_sandbox_lifecycle.json"

from src.broker.base import Order, OrderSide, OrderStatus, OrderType
from src.broker.longbridge_broker import LongBridgeBroker
from src.config.loader import AppConfig, load_config
from src.safety.trading_environment_guard import TradingEnvironmentGuard


TERMINAL_STATUSES = {
    OrderStatus.FILLED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.CANCELLED.value,
    OrderStatus.REJECTED.value,
}

LONGBRIDGE_PRICE_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,3})?$")
DEFAULT_TEST_LIMIT_PRICE = 1.23
DEFAULT_PRICE_TICK = 0.01


def _detect_config_path(cli_path: str | None) -> str:
    if cli_path:
        return cli_path
    candidates = [
        PROJECT_DIR / "config.local.yaml",
        PROJECT_DIR / "config.yaml",
        PROJECT_DIR / "config.sample.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise FileNotFoundError("No config file found")


def _jsonable(value: Any) -> Any:
    try:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, Decimal):
            return float(value)

        if isinstance(value, Enum):
            return _jsonable(value.value)

        if isinstance(value, (datetime, date)):
            return value.isoformat()

        if isinstance(value, Path):
            return str(value)

        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}

        if isinstance(value, (list, tuple, set, frozenset)):
            return [_jsonable(v) for v in value]

        if is_dataclass(value) and not isinstance(value, type):
            return _jsonable(asdict(value))

        if callable(value):
            return repr(value)

        attrs = getattr(value, "__dict__", None)
        if isinstance(attrs, dict):
            return {
                str(k): _jsonable(v)
                for k, v in attrs.items()
                if not str(k).startswith("_") and not callable(v)
            }

        slots = getattr(type(value), "__slots__", None)
        if slots:
            if isinstance(slots, str):
                slots = [slots]

            result = {}
            for name in slots:
                if str(name).startswith("_"):
                    continue
                try:
                    item = getattr(value, name)
                except Exception:
                    continue
                if not callable(item):
                    result[str(name)] = _jsonable(item)

            if result:
                return result

        return repr(value)

    except Exception as exc:
        return {
            "serialization_error": f"{type(exc).__name__}: {exc}",
            "value_type": type(value).__name__,
            "repr": repr(value),
        }


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _status_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(getattr(value, "value") or "").strip().upper()
    return str(value).strip().upper()


def _quantity_from_position(position: Any, ticker: str) -> int:
    target = _normalize_symbol(ticker)
    if position is None:
        return 0
    if isinstance(position, dict):
        if _normalize_symbol(position.get("ticker")) != target:
            return 0
        try:
            return int(float(position.get("quantity") or 0))
        except (TypeError, ValueError):
            return 0
    if _normalize_symbol(getattr(position, "ticker", "")) != target:
        return 0
    try:
        return int(float(getattr(position, "quantity", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _find_position(broker: Any, ticker: str) -> tuple[int, Any | None]:
    get_specific = getattr(broker, "get_position_for_ticker", None)
    if callable(get_specific):
        position = get_specific(ticker)
        return _quantity_from_position(position, ticker), position
    positions = broker.get_positions() or []
    for position in positions:
        if _normalize_symbol(getattr(position, "ticker", None) if not isinstance(position, dict) else position.get("ticker")) == _normalize_symbol(ticker):
            return _quantity_from_position(position, ticker), position
    return 0, None


def _positions_snapshot(broker: Any) -> list[dict[str, Any]]:
    positions = broker.get_positions() or []
    snapshot: list[dict[str, Any]] = []
    for position in positions:
        snapshot.append(_jsonable(position))
    return snapshot


def _orders_snapshot(broker: Any, ticker: str) -> list[dict[str, Any]]:
    getter = getattr(broker, "get_orders", None)
    if not callable(getter):
        getter = getattr(broker, "get_active_orders", None)
    if not callable(getter):
        return []
    try:
        orders = getter(ticker) or []
    except TypeError:
        orders = getter() or []
    return [_jsonable(order) for order in orders]


def _order_to_dict(order: Any) -> dict[str, Any]:
    if order is None:
        return {}
    payload = _jsonable(order)
    if not isinstance(payload, dict):
        return {"raw": payload}
    payload.setdefault("status", _status_text(getattr(order, "status", payload.get("status"))))
    payload.setdefault("order_id", str(getattr(order, "order_id", payload.get("order_id")) or ""))
    payload.setdefault("ticker", _normalize_symbol(getattr(order, "ticker", payload.get("ticker"))))
    payload.setdefault("quantity", int(getattr(order, "quantity", payload.get("quantity")) or 0))
    payload.setdefault("filled_quantity", int(getattr(order, "filled_quantity", payload.get("filled_quantity")) or 0))
    limit_price = getattr(order, "limit_price", payload.get("limit_price"))
    if limit_price in (None, ""):
        limit_price = getattr(order, "price", payload.get("price"))
    if limit_price in (None, ""):
        limit_price = getattr(order, "submitted_price", payload.get("submitted_price"))
    if limit_price in (None, ""):
        limit_price = getattr(order, "monitor_price", payload.get("monitor_price"))
    if limit_price not in (None, ""):
        try:
            payload["limit_price"] = float(limit_price)
        except (TypeError, ValueError):
            payload["limit_price"] = limit_price
    payload.setdefault("avg_fill_price", float(getattr(order, "avg_fill_price", payload.get("avg_fill_price")) or 0.0))
    payload.setdefault("notes", str(getattr(order, "notes", payload.get("notes")) or ""))
    return payload


def _order_debug_snapshot(order: Any) -> dict[str, Any]:
    if order is None:
        return {
            "type": "NoneType",
            "repr": "None",
            "fields": [],
            "order_id": "",
            "status": "",
            "error_field": None,
            "notes": "",
        }
    payload = _order_to_dict(order)
    if isinstance(order, dict):
        fields = sorted(str(key) for key in order.keys())
    else:
        try:
            fields = sorted(str(key) for key in vars(order).keys() if not str(key).startswith("_"))
        except Exception:
            fields = []
    return {
        "type": type(order).__name__,
        "repr": repr(order)[:600],
        "fields": fields,
        "order_id": str(payload.get("order_id") or ""),
        "status": str(payload.get("status") or ""),
        "error_field": payload.get("error") if isinstance(payload, dict) else None,
        "notes": str(payload.get("notes") or ""),
    }


def _is_valid_longbridge_price(value: Any) -> bool:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    if price <= 0:
        return False
    text = f"{price:.3f}".rstrip("0").rstrip(".")
    return bool(LONGBRIDGE_PRICE_RE.match(text))


def _quote_to_float(quote: Any, *names: str) -> float | None:
    if quote is None:
        return None
    candidates = [quote]
    if isinstance(quote, (list, tuple)):
        candidates = list(quote)
    for item in candidates:
        for name in names:
            value = None
            if isinstance(item, dict):
                value = item.get(name)
            else:
                value = getattr(item, name, None)
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
    return None


def _build_test_limit_price(broker: Any, ticker: str) -> float:
    quote_fetchers = [
        getattr(broker, "get_realtime_quote", None),
        getattr(broker, "get_quote", None),
    ]
    for fetch in quote_fetchers:
        if not callable(fetch):
            continue
        try:
            quote = fetch(ticker)
        except Exception:
            quote = None
        ask = _quote_to_float(quote, "ask", "ask_price", "best_ask", "sell", "offer")
        last = _quote_to_float(quote, "last_done", "last_price", "price", "close")
        bid = _quote_to_float(quote, "bid", "bid_price", "best_bid", "buy")
        if ask and _is_valid_longbridge_price(ask + DEFAULT_PRICE_TICK):
            return round(ask + DEFAULT_PRICE_TICK, 3)
        if last and _is_valid_longbridge_price(last + DEFAULT_PRICE_TICK):
            return round(last + DEFAULT_PRICE_TICK, 3)
        if bid and _is_valid_longbridge_price(bid + DEFAULT_PRICE_TICK):
            return round(bid + DEFAULT_PRICE_TICK, 3)
    return DEFAULT_TEST_LIMIT_PRICE


def _build_sell_limit_price(broker: Any, ticker: str) -> float:
    quote_fetchers = [
        getattr(broker, "get_realtime_quote", None),
        getattr(broker, "get_quote", None),
    ]
    for fetch in quote_fetchers:
        if not callable(fetch):
            continue
        try:
            quote = fetch(ticker)
        except Exception:
            quote = None
        bid = _quote_to_float(quote, "bid", "bid_price", "best_bid", "buy")
        last = _quote_to_float(quote, "last_done", "last_price", "price", "close")
        ask = _quote_to_float(quote, "ask", "ask_price", "best_ask", "sell", "offer")
        if bid and _is_valid_longbridge_price(max(0.01, bid - DEFAULT_PRICE_TICK)):
            return round(max(0.01, bid - DEFAULT_PRICE_TICK), 3)
        if last and _is_valid_longbridge_price(max(0.01, last - DEFAULT_PRICE_TICK)):
            return round(max(0.01, last - DEFAULT_PRICE_TICK), 3)
        if ask and _is_valid_longbridge_price(max(0.01, ask - DEFAULT_PRICE_TICK)):
            return round(max(0.01, ask - DEFAULT_PRICE_TICK), 3)
    return DEFAULT_TEST_LIMIT_PRICE


def _new_report(mode: str, ticker: str, account_type: str) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": mode,
        "broker": "Longbridge",
        "account_type": account_type,
        "ticker": ticker,
        "checks": {
            "mode_ok": False,
            "paper_account_ok": False,
            "live_order_disabled": False,
            "bootstrap_confirmed": False,
            "start_position_zero": False,
            "buy_order_submitted": False,
            "buy_order_status_checked": False,
            "buy_fill_confirmed": False,
            "position_increased_after_buy": False,
            "sell_order_submitted": False,
            "sell_order_status_checked": False,
            "sell_fill_confirmed": False,
            "position_returned_to_zero": False,
        },
        "steps": [],
        "precheck": {},
        "buy": {},
        "sell": {},
        "final_position": {},
        "reason": "",
    }


def _record_step(report: dict[str, Any], name: str, ok: bool, detail: str = "", **extra: Any) -> None:
    step = {"name": name, "ok": bool(ok), "detail": detail}
    if extra:
        step.update({k: _jsonable(v) for k, v in extra.items()})
    report["steps"].append(step)


def _persist_lifecycle_report(report: dict[str, Any]) -> None:
    try:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "report": report,
        }
        path = _lifecycle_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _current_account_type(config: AppConfig) -> str:
    return str(getattr(config.broker.longbridge, "account_type", "") or "").strip().lower()


def _mode_ok(config: AppConfig) -> bool:
    return str(getattr(config, "mode", "") or "").strip().lower() == "sandbox"


def build_broker(config: AppConfig) -> LongBridgeBroker:
    lb = config.broker.longbridge
    return LongBridgeBroker(
        app_key=lb.app_key,
        app_secret=lb.app_secret,
        access_token=lb.access_token,
        account_type=lb.account_type,
        region=lb.region,
        environment=lb.environment,
        http_url=lb.http_url,
        quote_ws_url=lb.quote_ws_url,
        trade_ws_url=lb.trade_ws_url,
        log_path=lb.log_path,
        allow_live_order=lb.allow_live_order,
    )


def _wait_for_order_and_position(
    broker: Any,
    order_id: str,
    ticker: str,
    *,
    expected_position_qty: int,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    observed_orders: list[dict[str, Any]] = []
    last_position_qty = 0
    terminal_order: dict[str, Any] = {}

    while time.monotonic() <= deadline:
        invalidate = getattr(broker, "invalidate_cache", None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception:
                pass

        order = broker.get_order(order_id)
        order_dict = _order_to_dict(order)
        observed_orders.append(order_dict)
        last_position_qty, _ = _find_position(broker, ticker)
        status = _status_text(order_dict.get("status"))
        if status in TERMINAL_STATUSES or last_position_qty == expected_position_qty:
            terminal_order = order_dict
            break
        time.sleep(max(0.1, float(poll_interval_seconds)))

    terminal_status = _status_text(terminal_order.get("status"))
    filled_quantity = int(terminal_order.get("filled_quantity") or 0)
    buy_fill_confirmed = (
        terminal_status in {"FILLED", "PARTIALLY_FILLED"}
        or filled_quantity >= 1
        or last_position_qty == expected_position_qty
    )
    return {
        "observed_orders": observed_orders,
        "final_order": terminal_order or (observed_orders[-1] if observed_orders else {}),
        "final_status": terminal_status if terminal_status else (_status_text(observed_orders[-1].get("status")) if observed_orders else ""),
        "final_position_qty": last_position_qty,
        "filled_confirmed": bool(buy_fill_confirmed),
        "filled_quantity": int((terminal_order or (observed_orders[-1] if observed_orders else {})).get("filled_quantity") or 0),
    }


def run_lifecycle_test(
    *,
    config_path: str | None = None,
    config: AppConfig | None = None,
    ticker: str | None = None,
    broker_factory: Callable[[AppConfig], Any] = build_broker,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    if config is None:
        config = load_config(_detect_config_path(config_path))

    mode = str(getattr(config, "mode", "") or "").strip().lower()
    account_type = _current_account_type(config)
    ticker = _normalize_symbol(ticker or getattr(config, "ticker", "") or "SOFI")
    report = _new_report(mode, ticker, account_type)

    guard = TradingEnvironmentGuard().validate(config)
    report["precheck"] = _jsonable(guard.summary)
    report["checks"]["mode_ok"] = _mode_ok(config)
    report["checks"]["paper_account_ok"] = account_type in {"paper", "demo"}
    report["checks"]["live_order_disabled"] = not bool(getattr(config.broker.longbridge, "allow_live_order", False))
    guard_formatter = TradingEnvironmentGuard()
    _record_step(
        report,
        "environment_guard",
        guard.ok and report["checks"]["mode_ok"] and report["checks"]["paper_account_ok"] and report["checks"]["live_order_disabled"],
        guard_formatter.format_report(guard),
    )

    if not report["checks"]["mode_ok"]:
        report["reason"] = "sandbox mode required"
        return report
    if not report["checks"]["paper_account_ok"]:
        report["reason"] = "sandbox paper/demo account required"
        return report
    if not report["checks"]["live_order_disabled"]:
        report["reason"] = "allow_live_order must be false"
        return report
    if not guard.ok:
        report["reason"] = "; ".join(guard.errors) or "trading environment guard failed"
        return report

    broker = broker_factory(config)
    if not broker.connect():
        report["reason"] = "broker connect failed"
        _record_step(report, "connect", False, getattr(broker, "last_connect_error", lambda: "")() or "connect failed")
        return report
    _record_step(report, "connect", True, "broker connected")

    bootstrap = {}
    confirm_fn = getattr(broker, "confirm_sandbox_first_run", None)
    if callable(confirm_fn):
        bootstrap = confirm_fn(ticker) or {}
    report["precheck"]["bootstrap"] = _jsonable(bootstrap)
    report["checks"]["bootstrap_confirmed"] = bool(bootstrap.get("confirmed"))
    _record_step(
        report,
        "bootstrap",
        report["checks"]["bootstrap_confirmed"],
        str(bootstrap.get("reason") or "sandbox bootstrap checked"),
        account_ok=bootstrap.get("account_ok"),
        positions_ok=bootstrap.get("positions_ok"),
        orders_ok=bootstrap.get("orders_ok"),
    )
    if not report["checks"]["bootstrap_confirmed"]:
        report["reason"] = str(bootstrap.get("reason") or "sandbox first-run bootstrap not confirmed")
        return report

    account = broker.get_account()
    positions_before = _positions_snapshot(broker)
    active_orders_before = _orders_snapshot(broker, ticker)
    current_quote_snapshot = _jsonable(getattr(broker, "get_realtime_quote", lambda _ticker: None)(ticker))
    start_qty, start_position = _find_position(broker, ticker)
    report["precheck"]["account"] = _jsonable(account)
    report["precheck"]["positions_before"] = positions_before
    report["precheck"]["active_orders_before"] = active_orders_before
    report["precheck"]["current_quote"] = current_quote_snapshot
    report["precheck"]["start_position"] = _jsonable(start_position)
    report["precheck"]["start_quantity"] = start_qty
    report["checks"]["start_position_zero"] = start_qty == 0
    _record_step(
        report,
        "precheck_snapshot",
        report["checks"]["start_position_zero"],
        f"start_qty={start_qty}; active_orders={len(active_orders_before)}",
        account=account,
        positions=positions_before,
        active_orders=active_orders_before,
    )
    if not report["checks"]["start_position_zero"]:
        report["reason"] = "starting position must be zero for lifecycle test"
        return report

    # BUY 1 share
    buy_limit_price = _build_test_limit_price(broker, ticker)
    buy_quote = _jsonable(getattr(broker, "get_realtime_quote", lambda _ticker: None)(ticker))
    report["precheck"]["buy_limit_price"] = buy_limit_price
    report["precheck"]["buy_limit_price_valid"] = _is_valid_longbridge_price(buy_limit_price)
    report["precheck"]["buy_quote"] = buy_quote
    buy_order = broker.place_order(
        ticker,
        OrderSide.BUY,
        1,
        order_type=OrderType.LIMIT,
        limit_price=buy_limit_price,
    )
    report["buy"]["submitted_order"] = _order_to_dict(buy_order)
    report["buy"]["response_debug"] = _order_debug_snapshot(buy_order)
    report["buy"]["submitted_price"] = buy_limit_price
    report["buy"]["current_price"] = _quote_to_float(buy_quote, "last_done", "last_price", "price", "close")
    report["checks"]["buy_order_submitted"] = True
    _record_step(report, "buy_submit", True, "BUY 1 share submitted", order=buy_order)
    buy_order_id = str(getattr(buy_order, "order_id", "") or "")
    if not buy_order_id:
        report["buy"]["response_debug"]["missing_order_id_reason"] = "broker.place_order returned an Order object without order_id"
        report["reason"] = "buy order id missing"
        return report
    buy_probe = _wait_for_order_and_position(
        broker,
        buy_order_id,
        ticker,
        expected_position_qty=start_qty + 1,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    report["buy"]["observed_orders"] = buy_probe["observed_orders"]
    report["buy"]["final_order"] = buy_probe["final_order"]
    report["buy"]["final_status"] = buy_probe["final_status"]
    report["buy"]["final_position_qty"] = buy_probe["final_position_qty"]
    report["buy"]["filled_quantity"] = buy_probe["filled_quantity"]
    report["buy"]["unfilled_reason"] = (
        str((buy_probe["final_order"] or {}).get("notes") or "")
        if not buy_probe["filled_confirmed"]
        else ""
    )
    report["checks"]["buy_order_status_checked"] = bool(buy_probe["observed_orders"])
    report["checks"]["buy_fill_confirmed"] = bool(buy_probe["filled_confirmed"])
    report["checks"]["position_increased_after_buy"] = buy_probe["final_position_qty"] >= start_qty + 1
    _record_step(
        report,
        "buy_observe",
        report["checks"]["buy_fill_confirmed"] and report["checks"]["position_increased_after_buy"],
        f"status={buy_probe['final_status'] or 'unknown'}; position={buy_probe['final_position_qty']}",
        observed_orders=buy_probe["observed_orders"],
    )
    if not report["checks"]["buy_fill_confirmed"]:
        cancel_fn = getattr(broker, "cancel_order", None)
        if callable(cancel_fn):
            try:
                cancel_fn(buy_order_id)
            except Exception:
                pass
        report["reason"] = "buy order did not fill in sandbox"
        return report

    positions_after_buy = _positions_snapshot(broker)
    report["buy"]["positions_after"] = positions_after_buy
    _record_step(
        report,
        "buy_position_snapshot",
        report["checks"]["position_increased_after_buy"],
        f"position after buy={buy_probe['final_position_qty']}",
        positions=positions_after_buy,
    )

    # SELL 1 share
    sell_limit_price = _build_sell_limit_price(broker, ticker)
    sell_quote = _jsonable(getattr(broker, "get_realtime_quote", lambda _ticker: None)(ticker))
    sell_order = broker.place_order(
        ticker,
        OrderSide.SELL,
        1,
        order_type=OrderType.LIMIT,
        limit_price=sell_limit_price,
    )
    report["sell"]["submitted_order"] = _order_to_dict(sell_order)
    report["sell"]["response_debug"] = _order_debug_snapshot(sell_order)
    report["sell"]["submitted_price"] = sell_limit_price
    report["sell"]["current_price"] = _quote_to_float(sell_quote, "last_done", "last_price", "price", "close")
    report["checks"]["sell_order_submitted"] = True
    _record_step(report, "sell_submit", True, "SELL 1 share submitted", order=sell_order)
    sell_order_id = str(getattr(sell_order, "order_id", "") or "")
    if not sell_order_id:
        report["sell"]["response_debug"]["missing_order_id_reason"] = "broker.place_order returned an Order object without order_id"
        report["reason"] = "sell order id missing"
        return report
    sell_probe = _wait_for_order_and_position(
        broker,
        sell_order_id,
        ticker,
        expected_position_qty=start_qty,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    report["sell"]["observed_orders"] = sell_probe["observed_orders"]
    report["sell"]["final_order"] = sell_probe["final_order"]
    report["sell"]["final_status"] = sell_probe["final_status"]
    report["sell"]["final_position_qty"] = sell_probe["final_position_qty"]
    report["sell"]["filled_quantity"] = sell_probe["filled_quantity"]
    report["sell"]["unfilled_reason"] = (
        str((sell_probe["final_order"] or {}).get("notes") or "")
        if not sell_probe["filled_confirmed"]
        else ""
    )
    report["checks"]["sell_order_status_checked"] = bool(sell_probe["observed_orders"])
    report["checks"]["sell_fill_confirmed"] = bool(sell_probe["filled_confirmed"])
    report["checks"]["position_returned_to_zero"] = sell_probe["final_position_qty"] == start_qty
    _record_step(
        report,
        "sell_observe",
        report["checks"]["sell_fill_confirmed"] and report["checks"]["position_returned_to_zero"],
        f"status={sell_probe['final_status'] or 'unknown'}; position={sell_probe['final_position_qty']}",
        observed_orders=sell_probe["observed_orders"],
    )

    final_positions = _positions_snapshot(broker)
    report["final_position"] = {
        "quantity": sell_probe["final_position_qty"],
        "snapshot": final_positions,
    }
    _record_step(
        report,
        "final_position_snapshot",
        report["checks"]["position_returned_to_zero"],
        f"final_position={sell_probe['final_position_qty']}",
        positions=final_positions,
    )

    report["ok"] = all(report["checks"].values())
    if not report["ok"] and not report["reason"]:
        report["reason"] = "lifecycle test did not complete successfully"
    return report


def print_report(report: dict[str, Any]) -> None:
    print("================================")
    print("Longbridge Sandbox Lifecycle Test")
    print("")
    print(f"Mode: {str(report.get('mode') or 'unknown').upper()}")
    print(f"Broker: {report.get('broker') or 'unknown'}")
    print(f"Account Type: {str(report.get('account_type') or 'unknown').upper()}")
    print(f"Ticker: {report.get('ticker') or 'unknown'}")
    print("")
    print("Checks:")
    for key, value in report.get("checks", {}).items():
        label = key.replace("_", " ").title()
        print(f"  {label}: {'PASS' if value else 'FAIL'}")
    if report.get("reason"):
        print("")
        print(f"Reason: {report['reason']}")
    if report.get("precheck"):
        print("")
        print("Precheck:")
        print(json.dumps(_jsonable(report["precheck"]), ensure_ascii=False, indent=2))
    if report.get("buy"):
        print("")
        print("BUY:")
        print(json.dumps(_jsonable(report["buy"]), ensure_ascii=False, indent=2))
    if report.get("sell"):
        print("")
        print("SELL:")
        print(json.dumps(_jsonable(report["sell"]), ensure_ascii=False, indent=2))
    if report.get("final_position"):
        print("")
        print("Final Position:")
        print(json.dumps(_jsonable(report["final_position"]), ensure_ascii=False, indent=2))
    print("")
    print(f"Overall: {'PASS' if report.get('ok') else 'FAIL'}")
    print("================================")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Longbridge sandbox order lifecycle diagnostic")
    parser.add_argument("--config", type=str, default=None, help="Config path")
    parser.add_argument("--ticker", type=str, default=None, help="Test ticker (defaults to config ticker)")
    parser.add_argument("--timeout-seconds", type=float, default=60.0, help="Polling timeout")
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0, help="Polling interval")
    args = parser.parse_args()

    config_path = _detect_config_path(args.config)
    config = load_config(config_path)
    report = run_lifecycle_test(
        config=config,
        ticker=args.ticker,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    _persist_lifecycle_report(report)
    print_report(report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
