from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def _parse_date(value: str | None) -> str:
    if value:
        return value
    return datetime.now().strftime("%Y%m%d")


def trade_log_path(log_dir: str | Path | None = None, day: str | None = None) -> Path:
    root = Path(log_dir) if log_dir is not None else Path(__file__).resolve().parents[2] / "logs"
    return root / f"trades-{_parse_date(day)}.jsonl"


def load_trade_records(log_dir: str | Path | None = None, day: str | None = None) -> list[dict[str, Any]]:
    path = trade_log_path(log_dir, day)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}, ()):
            return value
    return None


def summarize_trade_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    phase_counts = Counter(str(record.get("phase", "unknown")) for record in records)
    decision_records = [record for record in records if str(record.get("phase")) == "decision"]
    execution_records = [record for record in records if str(record.get("phase")) == "execution"]
    latest_record = records[-1] if records else {}
    latest_execution = execution_records[-1] if execution_records else {}
    latest_decision = decision_records[-1] if decision_records else {}

    tickers = sorted({str(record.get("ticker", "")).upper() for record in execution_records if record.get("ticker")})
    buy_count = sum(1 for record in execution_records if str(record.get("order", {}).get("side", "")).lower() == "buy")
    sell_count = sum(1 for record in execution_records if str(record.get("order", {}).get("side", "")).lower() == "sell")
    order_qty = sum(int(record.get("order", {}).get("qty", 0) or 0) for record in execution_records)

    latest_source = latest_execution or latest_record
    latest_line = None
    if isinstance(latest_source, dict) and latest_source:
        ticker = latest_source.get("ticker") or latest_source.get("symbol") or ""
        phase = latest_source.get("phase") or latest_source.get("action") or "record"
        order = latest_source.get("order") if isinstance(latest_source.get("order"), dict) else {}
        side = order.get("side") or latest_source.get("trade_signal", {}).get("action") or ""
        qty = order.get("qty") or ""
        status = ""
        response = latest_source.get("response")
        if isinstance(response, dict):
            status = response.get("status") or response.get("body", {}).get("status") or response.get("ok")
        latest_line = " ".join(str(part) for part in [phase, ticker, side, qty, status] if part not in ("", None, False))

    return {
        "record_count": len(records),
        "decision_count": len(decision_records),
        "execution_count": len(execution_records),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "order_qty": order_qty,
        "tickers": tickers,
        "phase_counts": dict(phase_counts),
        "latest_record": latest_record,
        "latest_decision": latest_decision,
        "latest_execution": latest_execution,
        "latest_line": latest_line,
        "execution_mode": _first_non_empty(
            latest_execution.get("execution_mode"),
            latest_decision.get("execution_mode"),
            latest_record.get("execution_mode"),
            "paper",
        ),
        "reduce_only": bool(_first_non_empty(
            latest_execution.get("reduce_only"),
            latest_decision.get("reduce_only"),
            latest_record.get("reduce_only"),
            False,
        )),
        "new_entries_allowed": not bool(_first_non_empty(
            latest_execution.get("reduce_only"),
            latest_decision.get("reduce_only"),
            latest_record.get("reduce_only"),
            False,
        )),
        "risk_pause_reason": _first_non_empty(
            latest_execution.get("risk_pause_reason"),
            latest_decision.get("risk_pause_reason"),
            latest_record.get("risk_pause_reason"),
            "",
        ),
        "risk_pause_reasons": _first_non_empty(
            latest_execution.get("risk_pause_reasons"),
            latest_decision.get("risk_pause_reasons"),
            latest_record.get("risk_pause_reasons"),
            [],
        ),
    }


def summarize_trade_log(log_dir: str | Path | None = None, day: str | None = None) -> dict[str, Any]:
    path = trade_log_path(log_dir, day)
    records = load_trade_records(log_dir, day)
    summary = summarize_trade_records(records)
    summary["path"] = str(path)
    summary["exists"] = path.exists()
    return summary


def daily_trade_report(log_dir: str | Path | None = None, day: str | None = None) -> dict[str, Any]:
    return summarize_trade_log(log_dir=log_dir, day=day)
