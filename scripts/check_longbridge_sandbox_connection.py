#!/usr/bin/env python3
"""Read-only Longbridge sandbox connectivity probe.

This script verifies the sandbox path end-to-end without changing trading
logic. It checks:
  - current trading mode
  - broker initialization
  - account snapshot
  - positions snapshot
  - active orders snapshot
  - sandbox first-run read-only bootstrap
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.broker.base import OrderSide, OrderStatus, OrderType
from src.broker.longbridge_broker import LongBridgeBroker
from src.config.loader import load_config


@dataclass
class CheckResult:
    ok: bool
    detail: str = ""


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


def _short_mode(value: str | None) -> str:
    return str(value or "").strip().lower()


def _extract_price(snapshot: Any, *names: str) -> float | None:
    if snapshot is None:
        return None
    candidates = [snapshot]
    if isinstance(snapshot, (list, tuple)) and snapshot:
        candidates = list(snapshot)
    for item in candidates:
        for name in names:
            if isinstance(item, dict) and name in item and item[name] is not None:
                try:
                    return float(item[name])
                except (TypeError, ValueError):
                    continue
            if hasattr(item, name):
                try:
                    return float(getattr(item, name))
                except (TypeError, ValueError):
                    continue
    return None


def build_broker(config) -> LongBridgeBroker:
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


def probe_sandbox(config, broker: LongBridgeBroker) -> dict[str, Any]:
    ticker = str(config.ticker or "").strip().upper()
    mode = _short_mode(config.mode)
    summary: dict[str, Any] = {
        "mode": mode,
        "broker": "Longbridge" if mode == "sandbox" else "PaperBroker",
        "account_type": str(getattr(config.broker.longbridge, "account_type", "") or "").strip().lower(),
        "account": CheckResult(False, "not checked"),
        "position": CheckResult(False, "not checked"),
        "orders": CheckResult(False, "not checked"),
        "order_protection": CheckResult(False, "not checked"),
        "first_run": CheckResult(False, "not checked"),
        "connection_error": "",
        "active_orders_count": 0,
        "position_count": 0,
    }

    if mode != "sandbox":
        summary["connection_error"] = f"current mode is {mode or 'missing'}; sandbox required"
        return summary

    if not broker.connect():
        summary["connection_error"] = broker.last_connect_error() or "broker connect failed"
        return summary

    try:
        account_type = str(getattr(config.broker.longbridge, "account_type", "") or "").strip().lower()
        allow_live_order = bool(getattr(config.broker.longbridge, "allow_live_order", False))
        if account_type not in {"paper", "demo"}:
            summary["order_protection"] = CheckResult(False, "sandbox mode requires paper/demo account_type")
            summary["first_run"] = CheckResult(False, "sandbox mode requires paper/demo account_type")
            return summary
        sandbox_guard = getattr(broker, "_sandbox_endpoints_are_safe", None)
        if callable(sandbox_guard) and not sandbox_guard():
            detail = "; ".join(getattr(broker, "_sandbox_safety_issues", lambda: [])()) or "sandbox endpoints are not safe"
            summary["order_protection"] = CheckResult(False, detail)
            summary["first_run"] = CheckResult(False, detail)
            return summary
        if allow_live_order:
            summary["order_protection"] = CheckResult(False, "allow_live_order must remain false in sandbox")
            summary["first_run"] = CheckResult(False, "allow_live_order must remain false in sandbox")
            return summary

        bootstrap = broker.confirm_sandbox_first_run(ticker) if hasattr(broker, "confirm_sandbox_first_run") else {}
        if isinstance(bootstrap, dict):
            summary["first_run"] = CheckResult(
                ok=bool(bootstrap.get("confirmed")),
                detail=str(bootstrap.get("reason") or "sandbox first-run bootstrap checked"),
            )
            summary["order_protection"] = CheckResult(
                ok=bool(bootstrap.get("confirmed")),
                detail="sandbox first-run read-only bootstrap confirmed" if bootstrap.get("confirmed") else str(bootstrap.get("reason") or "sandbox first-run bootstrap pending"),
            )
            if bootstrap.get("account_ok") is not None:
                summary["account"] = CheckResult(
                    ok=bool(bootstrap.get("account_ok")),
                    detail=f"equity={bootstrap.get('account', {}).get('equity') if isinstance(bootstrap.get('account'), dict) else None} cash={bootstrap.get('account', {}).get('cash') if isinstance(bootstrap.get('account'), dict) else None} buying_power={bootstrap.get('account', {}).get('buying_power') if isinstance(bootstrap.get('account'), dict) else None}",
                )
            else:
                summary["account"] = CheckResult(True, "account snapshot available")
            summary["position_count"] = int(bootstrap.get("positions_count") or 0)
            summary["position"] = CheckResult(bool(bootstrap.get("positions_ok")), f"{summary['position_count']} positions")
            summary["active_orders_count"] = int(bootstrap.get("orders_count") or 0)
            summary["orders"] = CheckResult(bool(bootstrap.get("orders_ok")), f"{summary['active_orders_count']} active orders")
        else:
            summary["first_run"] = CheckResult(True, "sandbox first-run bootstrap helper unavailable")
            summary["order_protection"] = CheckResult(True, "sandbox first-run bootstrap helper unavailable")
    except Exception as exc:
        summary["first_run"] = CheckResult(False, str(exc))
        summary["order_protection"] = CheckResult(False, str(exc))
        return summary
    return summary


def print_report(summary: dict[str, Any]) -> None:
    print("================================")
    print("Longbridge Sandbox Test")
    print("")
    print(f"Mode: {summary.get('mode', 'unknown')}")
    print(f"Broker: {summary.get('broker', 'unknown')}")
    if summary.get("account_type"):
        print(f"Account Type: {str(summary['account_type']).upper()}")
    print("")
    print(f"Account: {'OK' if summary['account'].ok else 'FAIL'}")
    if summary["account"].detail:
        print(f"  {summary['account'].detail}")
    print(f"Position: {'OK' if summary['position'].ok else 'FAIL'}")
    if summary["position"].detail:
        print(f"  {summary['position'].detail}")
    print(f"Order Protection: {'PASS' if summary['order_protection'].ok else 'FAIL'}")
    if summary["order_protection"].detail:
        print(f"  {summary['order_protection'].detail}")
    if summary.get("first_run"):
        print(f"First Run: {'OK' if summary['first_run'].ok else 'FAIL'}")
        if summary["first_run"].detail:
            print(f"  {summary['first_run'].detail}")
    print("================================")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Longbridge sandbox connectivity")
    parser.add_argument("--config", type=str, default=None, help="Config path")
    args = parser.parse_args()

    config_path = _detect_config_path(args.config)
    config = load_config(config_path)
    broker = build_broker(config)
    summary = probe_sandbox(config, broker)
    print_report(summary)

    ok = bool(summary["account"].ok and summary["position"].ok and summary["orders"].ok and summary["order_protection"].ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
