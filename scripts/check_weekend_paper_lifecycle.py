from __future__ import annotations

import sys
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.config.runtime_paths import resolve_state_dir

LIFECYCLE_STATE_PATH = resolve_state_dir(PROJECT_DIR) / "lifecycle" / "weekend_paper_lifecycle.json"


def _lifecycle_state_path() -> Path:
    return resolve_state_dir(PROJECT_DIR) / "lifecycle" / "weekend_paper_lifecycle.json"

from src.broker.base import OrderSide, OrderStatus, OrderType
from src.broker.paper_broker import PaperBroker
from src.engine.trading_engine import append_runtime_audit
from src.reports.trade_audit import summarize_trade_log


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


def _record_execution(side: OrderSide, order, price: float) -> None:
    append_runtime_audit(
        {
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
    )


def _persist_weekend_lifecycle_report(report: dict[str, Any]) -> None:
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


def run_weekend_paper_lifecycle(audit_dir: Path | None = None) -> dict[str, Any]:
    root = Path(audit_dir) if audit_dir is not None else Path(tempfile.mkdtemp(prefix="weekend-paper-audit-"))
    root.mkdir(parents=True, exist_ok=True)

    broker = PaperBroker(initial_cash=1000.0, commission_per_share=0.0, slippage_pct=0.0)
    report: dict[str, Any] = {
        "bootstrap_confirmed": False,
        "start_position_zero": False,
        "buy": {},
        "sell": {},
        "checks": {},
        "audit": {},
    }

    with _temporary_runtime_audit_dir(root):
        report["bootstrap_confirmed"] = bool(broker.connect())
        start_positions = broker.get_positions()
        report["start_position_zero"] = len(start_positions) == 0

        buy_order = broker.place_order(
            ticker="TEST",
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=10.00,
            current_bid=10.00,
            current_ask=10.00,
        )
        report["buy"] = {
            "order_id": buy_order.order_id,
            "status": buy_order.status.value,
            "filled_quantity": int(buy_order.filled_quantity or 0),
            "avg_fill_price": float(buy_order.avg_fill_price or 0.0),
        }
        _record_execution(OrderSide.BUY, buy_order, 10.00)

        after_buy = broker.get_position_for_ticker("TEST")
        report["checks"]["buy_fill_confirmed"] = buy_order.status == OrderStatus.FILLED
        report["checks"]["position_increased_after_buy"] = bool(after_buy and after_buy.quantity == 1)

        sell_order = broker.place_order(
            ticker="TEST",
            side=OrderSide.SELL,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=10.10,
            current_bid=10.10,
            current_ask=10.10,
        )
        report["sell"] = {
            "order_id": sell_order.order_id,
            "status": sell_order.status.value,
            "filled_quantity": int(sell_order.filled_quantity or 0),
            "avg_fill_price": float(sell_order.avg_fill_price or 0.0),
        }
        _record_execution(OrderSide.SELL, sell_order, 10.10)

        after_sell = broker.get_position_for_ticker("TEST")
        final_qty = int(getattr(after_sell, "quantity", 0) or 0) if after_sell else 0
        report["checks"]["sell_fill_confirmed"] = sell_order.status == OrderStatus.FILLED
        report["checks"]["position_returned_to_zero"] = final_qty == 0

        trade_summary = summarize_trade_log(root, mode="paper")
        report["audit"] = {
            "path": str(trade_summary.get("path") or (root / "trades-unknown.jsonl")),
            "execution_count": int(trade_summary.get("execution_count", 0) or 0),
            "buy_count": int(trade_summary.get("buy_count", 0) or 0),
            "sell_count": int(trade_summary.get("sell_count", 0) or 0),
            "tickers": trade_summary.get("tickers", []),
        }
        report["checks"]["audit_log_confirmed"] = (
            report["audit"]["execution_count"] == 2
            and report["audit"]["buy_count"] == 1
            and report["audit"]["sell_count"] == 1
        )
        report["checks"]["overall"] = all(bool(v) for v in report["checks"].values())

    return report


def main() -> int:
    report = run_weekend_paper_lifecycle()
    _persist_weekend_lifecycle_report(report)
    print("================================")
    print("Weekend Paper Lifecycle Test")
    print("")
    print(f"Mode: paper")
    print(f"Broker: PaperBroker")
    print("")
    print(f"Bootstrap Confirmed: {'PASS' if report['bootstrap_confirmed'] else 'FAIL'}")
    print(f"Start Position Zero: {'PASS' if report['start_position_zero'] else 'FAIL'}")
    print(f"Buy Order Submitted: {'PASS' if report['buy'].get('order_id') else 'FAIL'}")
    print(f"Buy Fill Confirmed: {'PASS' if report['checks'].get('buy_fill_confirmed') else 'FAIL'}")
    print(f"Position Increased After Buy: {'PASS' if report['checks'].get('position_increased_after_buy') else 'FAIL'}")
    print(f"Sell Order Submitted: {'PASS' if report['sell'].get('order_id') else 'FAIL'}")
    print(f"Sell Fill Confirmed: {'PASS' if report['checks'].get('sell_fill_confirmed') else 'FAIL'}")
    print(f"Position Returned To Zero: {'PASS' if report['checks'].get('position_returned_to_zero') else 'FAIL'}")
    print(f"Audit Log Confirmed: {'PASS' if report['checks'].get('audit_log_confirmed') else 'FAIL'}")
    print(f"Overall: {'PASS' if report['checks'].get('overall') else 'FAIL'}")
    print("================================")
    return 0 if report["checks"].get("overall") else 1


if __name__ == "__main__":
    raise SystemExit(main())
