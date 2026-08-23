from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.config.runtime_paths import resolve_state_dir

LIFECYCLE_STATE_PATH = resolve_state_dir(PROJECT_DIR) / "lifecycle" / "dynamic_range_paper_lifecycle.json"


def _lifecycle_state_path() -> Path:
    return resolve_state_dir(PROJECT_DIR) / "lifecycle" / "dynamic_range_paper_lifecycle.json"

from src.broker.base import OrderSide, OrderStatus, OrderType
from src.broker.paper_broker import PaperBroker
from src.engine.trading_engine import append_runtime_audit
from src.reports.trade_audit import summarize_trade_log
from src.strategy import DynamicRangeCalculator, EntryLayerPlanner, ExitLayerManager


@contextmanager
def _temporary_runtime_audit_dir(path: Path):
    previous = os.environ.get("SOXS_RUNTIME_AUDIT_DIR")
    os.environ["SOXS_RUNTIME_AUDIT_DIR"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SOXS_RUNTIME_AUDIT_DIR", None)
        else:
            os.environ["SOXS_RUNTIME_AUDIT_DIR"] = previous


def _record_execution(side: OrderSide, order, price: float, layer_id: int | None = None) -> None:
    payload = {
        "phase": "execution",
        "execution_mode": "paper",
        "ticker": "TEST",
        "symbol": "TEST",
        "reduce_only": False,
        "order": {
            "side": side.value.lower(),
            "qty": int(order.quantity or 0),
            "price": float(price),
            "limit_price": float(price),
            "order_id": order.order_id,
            "status": str(order.status.value).lower(),
            "filled_quantity": int(order.filled_quantity or 0),
            "filled_price": float(order.avg_fill_price or 0.0),
        },
        "response": {
            "status": str(order.status.value).lower(),
            "order_id": order.order_id,
        },
    }
    if layer_id is not None:
        payload["layer_id"] = int(layer_id)
    append_runtime_audit(payload)


def _persist_dynamic_range_report(report: dict[str, Any]) -> None:
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


def _synthetic_bars() -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]], list[tuple[datetime, float]], list[tuple[datetime, float]]]:
    base = datetime(2026, 7, 11, 9, 30)
    highs: list[tuple[datetime, float]] = []
    lows: list[tuple[datetime, float]] = []
    closes: list[tuple[datetime, float]] = []
    current_price = 10.0
    for index in range(80):
        ts = base + timedelta(minutes=index)
        wave = math.sin(index / 6.0) * 0.18 + math.sin(index / 17.0) * 0.07
        close = round(current_price + wave, 4)
        high = round(close * 1.008, 4)
        low = round(close * 0.992, 4)
        highs.append((ts, high))
        lows.append((ts, low))
        closes.append((ts, close))
    return highs, lows, closes, list(closes)


def run_dynamic_range_paper_lifecycle(audit_dir: Path | None = None) -> dict[str, Any]:
    root = Path(audit_dir) if audit_dir is not None else Path(tempfile.mkdtemp(prefix="dynamic-range-paper-audit-"))
    root.mkdir(parents=True, exist_ok=True)

    broker = PaperBroker(initial_cash=1000.0, commission_per_share=0.0, slippage_pct=0.0)
    range_calculator = DynamicRangeCalculator()
    entry_planner = EntryLayerPlanner()
    exit_manager = ExitLayerManager()

    highs, lows, closes, closes_only = _synthetic_bars()
    current_price = closes_only[-1][1]
    range_result = range_calculator.calculate(
        timestamp=closes_only[-1][0],
        current_price=current_price,
        highs=highs,
        lows=lows,
        closes=closes,
        atr_period=14,
        ema_period=20,
        rolling_lookback=20,
        minimum_range_pct=1.0,
        maximum_range_pct=12.0,
        support_buffer=0.1,
        resistance_buffer=0.1,
    )

    buy_fill_price = 10.00
    sell_fill_price = 10.10
    report: dict[str, Any] = {
        "mode": "paper",
        "broker": "PaperBroker",
        "longbridge_used": False,
        "bootstrap_confirmed": False,
        "start_position_zero": False,
        "range": range_result,
        "entry_plan": {},
        "exit_plan": {},
        "buy_layers": [],
        "sell_layers": [],
        "checks": {},
        "audit": {},
    }

    with _temporary_runtime_audit_dir(root):
        report["bootstrap_confirmed"] = bool(broker.connect())
        report["start_position_zero"] = len(broker.get_positions()) == 0

        entry_plan = entry_planner.plan_layers(
            support=float(range_result.get("support") or 0.0),
            grid_width=float(range_result.get("grid_width") or 0.0),
            total_target_quantity=5,
            max_layers=5,
            existing_layers=[],
            pending_buy_exists=False,
            inventory_ratio=0.0,
            trend_buy_allowed=True,
        )
        report["entry_plan"] = entry_plan

        filled_entry_layers: list[dict[str, Any]] = []
        first_buy_confirmed = False
        for layer in entry_plan.get("layers", []):
            order = broker.place_order(
                ticker="TEST",
                side=OrderSide.BUY,
                quantity=int(layer["target_quantity"]),
                order_type=OrderType.LIMIT,
                limit_price=buy_fill_price,
                current_bid=buy_fill_price,
                current_ask=buy_fill_price,
            )
            report["buy_layers"].append(
                {
                    "layer_id": layer["layer_id"],
                    "order_id": order.order_id,
                    "status": order.status.value,
                    "filled_quantity": int(order.filled_quantity or 0),
                    "avg_fill_price": float(order.avg_fill_price or 0.0),
                }
            )
            _record_execution(OrderSide.BUY, order, buy_fill_price, layer_id=int(layer["layer_id"]))
            if order.status == OrderStatus.FILLED:
                filled_entry_layers.append(
                    {
                        "layer_id": layer["layer_id"],
                        "filled_quantity": int(order.filled_quantity or 0),
                        "average_fill_price": float(order.avg_fill_price or 0.0),
                        "status": "filled",
                        "exit_target": round(float(order.avg_fill_price or buy_fill_price) + float(range_result.get("grid_width") or 0.0), 4),
                        "stop_price": round(max(0.01, float(order.avg_fill_price or buy_fill_price) * 0.98), 4),
                    }
                )
            if not first_buy_confirmed and order.status == OrderStatus.FILLED:
                first_buy_confirmed = True

        broker.update_price("TEST", buy_fill_price)
        after_buy = broker.get_position_for_ticker("TEST")
        position_after_buy = int(getattr(after_buy, "quantity", 0) or 0) if after_buy else 0

        exit_plan = exit_manager.plan_exits(
            filled_entry_layers=filled_entry_layers,
            current_price=sell_fill_price,
            grid_width=float(range_result.get("grid_width") or 0.0),
            current_broker_position=position_after_buy,
            pending_sell_exists=False,
        )
        report["exit_plan"] = exit_plan

        for order_request in exit_plan.get("orders", []):
            order = broker.place_order(
                ticker="TEST",
                side=OrderSide.SELL,
                quantity=int(order_request["sell_quantity"]),
                order_type=OrderType.LIMIT,
                limit_price=sell_fill_price,
                current_bid=sell_fill_price,
                current_ask=sell_fill_price,
            )
            report["sell_layers"].append(
                {
                    "layer_id": order_request["layer_id"],
                    "order_id": order.order_id,
                    "status": order.status.value,
                    "filled_quantity": int(order.filled_quantity or 0),
                    "avg_fill_price": float(order.avg_fill_price or 0.0),
                }
            )
            _record_execution(OrderSide.SELL, order, sell_fill_price, layer_id=int(order_request["layer_id"]))

        broker.update_price("TEST", sell_fill_price)
        after_sell = broker.get_position_for_ticker("TEST")
        final_qty = int(getattr(after_sell, "quantity", 0) or 0) if after_sell else 0

        trade_summary = summarize_trade_log(root, mode="paper")
        report["audit"] = {
            "path": str(trade_summary.get("path") or (root / "trades-unknown.jsonl")),
            "execution_count": int(trade_summary.get("execution_count", 0) or 0),
            "buy_count": int(trade_summary.get("buy_count", 0) or 0),
            "sell_count": int(trade_summary.get("sell_count", 0) or 0),
            "tickers": trade_summary.get("tickers", []),
        }

        report["checks"]["buy_fill_confirmed"] = all(
            layer.get("status") == "FILLED" and int(layer.get("filled_quantity") or 0) > 0
            for layer in report["buy_layers"]
        )
        report["checks"]["position_increased_after_buy"] = position_after_buy > 0
        report["checks"]["sell_fill_confirmed"] = all(
            layer.get("status") == "FILLED" and int(layer.get("filled_quantity") or 0) > 0
            for layer in report["sell_layers"]
        )
        report["checks"]["position_returned_to_zero"] = final_qty == 0
        report["checks"]["audit_log_confirmed"] = (
            report["audit"]["execution_count"] == len(report["buy_layers"]) + len(report["sell_layers"])
            and report["audit"]["buy_count"] == len(report["buy_layers"])
            and report["audit"]["sell_count"] == len(report["sell_layers"])
            and report["audit"]["tickers"] == ["TEST"]
        )
        report["checks"]["overall"] = all(bool(v) for v in report["checks"].values())

    return report


def main() -> int:
    report = run_dynamic_range_paper_lifecycle()
    _persist_dynamic_range_report(report)
    print("================================")
    print("Dynamic Range Paper Lifecycle Test")
    print("")
    print(f"Mode: paper")
    print(f"Broker: PaperBroker")
    print(f"Dynamic Range Valid: {'PASS' if report['range'].get('valid') else 'FAIL'}")
    print(f"Entry Layers Generated: {'PASS' if report['entry_plan'].get('layers') else 'FAIL'}")
    print(f"Bootstrap Confirmed: {'PASS' if report['bootstrap_confirmed'] else 'FAIL'}")
    print(f"Start Position Zero: {'PASS' if report['start_position_zero'] else 'FAIL'}")
    print(f"Buy Orders Filled: {'PASS' if report['checks'].get('buy_fill_confirmed') else 'FAIL'}")
    print(f"Position Increased After Buy: {'PASS' if report['checks'].get('position_increased_after_buy') else 'FAIL'}")
    print(f"Sell Orders Filled: {'PASS' if report['checks'].get('sell_fill_confirmed') else 'FAIL'}")
    print(f"Position Returned To Zero: {'PASS' if report['checks'].get('position_returned_to_zero') else 'FAIL'}")
    print(f"Audit Log Confirmed: {'PASS' if report['checks'].get('audit_log_confirmed') else 'FAIL'}")
    print(f"Overall: {'PASS' if report['checks'].get('overall') else 'FAIL'}")
    print("================================")
    return 0 if report["checks"].get("overall") else 1


if __name__ == "__main__":
    raise SystemExit(main())
