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


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _filter_records_by_mode(records: list[dict[str, Any]], mode: str | None = None) -> list[dict[str, Any]]:
    if not mode:
        return records
    normalized = str(mode).strip().lower()
    if not normalized:
        return records
    filtered = [
        record
        for record in records
        if (
            str(record.get("execution_mode", "")).strip().lower() == normalized
            or str(record.get("action", "")).strip().lower() in {"place_order", "get_order", "cancel_order"}
        )
    ]
    return filtered if filtered else records


def summarize_trade_records(records: list[dict[str, Any]], mode: str | None = None) -> dict[str, Any]:
    records = _filter_records_by_mode(records, mode=mode)
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
    submitted_orders: dict[str, dict[str, Any]] = {}
    order_updates: dict[str, str] = {}
    latest_submitted = {}
    latest_filled = {}

    for record in records:
        if str(record.get("action") or "").strip().lower() == "place_order":
            response = record.get("response") if isinstance(record.get("response"), dict) else {}
            request = record.get("request") if isinstance(record.get("request"), dict) else {}
            order_id = str(response.get("order_id") or "").strip()
            status = _normalize_status(response.get("status"))
            if order_id:
                submitted_orders[order_id] = {
                    "order_id": order_id,
                    "ticker": request.get("ticker") or "",
                    "side": str(request.get("side") or "").lower(),
                    "qty": int(request.get("quantity") or 0),
                    "status": status,
                    "timestamp": record.get("timestamp") or "",
                }
            if status == "submitted":
                latest_submitted = {
                    "order_id": order_id,
                    "ticker": request.get("ticker") or "",
                    "side": str(request.get("side") or "").lower(),
                    "qty": int(request.get("quantity") or 0),
                    "timestamp": record.get("timestamp") or "",
                }
        elif str(record.get("action") or "").strip().lower() == "get_order":
            request = record.get("request") if isinstance(record.get("request"), dict) else {}
            response = record.get("response") if isinstance(record.get("response"), dict) else {}
            order_id = str(request.get("order_id") or "").strip()
            mapped_status = _normalize_status(response.get("mapped_status"))
            if order_id and mapped_status:
                order_updates[order_id] = mapped_status
                if mapped_status in {"filled", "partially_filled"}:
                    latest_filled = {
                        "order_id": order_id,
                        "status": mapped_status,
                        "timestamp": record.get("timestamp") or "",
                    }

    order_status_counts = Counter(order_updates.values())
    unresolved_order_ids = sorted(
        order_id
        for order_id, payload in submitted_orders.items()
        if _normalize_status(payload.get("status")) == "submitted" and order_id not in order_updates
    )
    latest_submitted_line = None
    if latest_submitted:
        latest_submitted_line = " ".join(
            str(part)
            for part in [
                latest_submitted.get("ticker"),
                latest_submitted.get("side"),
                latest_submitted.get("qty"),
                "submitted",
            ]
            if part not in ("", None)
        )
    latest_filled_line = None
    if latest_filled:
        submitted_meta = submitted_orders.get(str(latest_filled.get("order_id") or ""), {})
        latest_filled_line = " ".join(
            str(part)
            for part in [
                submitted_meta.get("ticker") or "",
                submitted_meta.get("side") or "",
                submitted_meta.get("qty") or "",
                latest_filled.get("status") or "",
            ]
            if part not in ("", None)
        )

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
        "broker_submitted_count": sum(
            1
            for payload in submitted_orders.values()
            if _normalize_status(payload.get("status")) == "submitted"
        ),
        "broker_filled_count": int(order_status_counts.get("filled", 0)),
        "broker_partial_filled_count": int(order_status_counts.get("partially_filled", 0)),
        "broker_rejected_count": int(order_status_counts.get("rejected", 0)),
        "broker_cancelled_count": int(order_status_counts.get("cancelled", 0)),
        "broker_unresolved_count": len(unresolved_order_ids),
        "broker_unresolved_order_ids": unresolved_order_ids,
        "latest_submitted_line": latest_submitted_line,
        "latest_filled_line": latest_filled_line,
        "notification_reconcile_ok": len(unresolved_order_ids) == 0,
        "mode_filter": mode or "",
    }


def summarize_trade_log(log_dir: str | Path | None = None, day: str | None = None, mode: str | None = None) -> dict[str, Any]:
    path = trade_log_path(log_dir, day)
    records = load_trade_records(log_dir, day)
    summary = summarize_trade_records(records, mode=mode)
    summary["path"] = str(path)
    summary["exists"] = path.exists()
    return summary


def daily_trade_report(log_dir: str | Path | None = None, day: str | None = None, mode: str | None = None) -> dict[str, Any]:
    return summarize_trade_log(log_dir=log_dir, day=day, mode=mode)
