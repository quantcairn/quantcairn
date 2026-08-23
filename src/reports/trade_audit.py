from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ..config.runtime_paths import resolve_logs_dir
from typing import Any


def _parse_date(value: str | None) -> str:
    if value:
        return value
    return datetime.now().strftime("%Y%m%d")


def trade_log_path(log_dir: str | Path | None = None, day: str | None = None) -> Path:
    root = Path(log_dir) if log_dir is not None else resolve_logs_dir(Path(__file__).resolve().parents[2])
    return root / f"trades-{_parse_date(day)}.jsonl"


def latest_trade_log_day(log_dir: str | Path | None = None) -> str | None:
    root = Path(log_dir) if log_dir is not None else resolve_logs_dir(Path(__file__).resolve().parents[2])
    candidates = sorted(root.glob("trades-*.jsonl"))
    if not candidates:
        return None
    latest = candidates[-1].stem.replace("trades-", "")
    return latest or None


def latest_trade_activity_day(log_dir: str | Path | None = None, mode: str | None = None) -> str | None:
    root = Path(log_dir) if log_dir is not None else resolve_logs_dir(Path(__file__).resolve().parents[2])
    candidates = sorted(root.glob("trades-*.jsonl"), reverse=True)
    for path in candidates:
        day = path.stem.replace("trades-", "")
        summary = summarize_trade_log(root, day=day, mode=mode)
        if (
            int(summary.get("broker_submitted_count", 0) or 0) > 0
            or int(summary.get("broker_filled_count", 0) or 0) > 0
            or int(summary.get("broker_partial_filled_count", 0) or 0) > 0
        ):
            return day
    return latest_trade_log_day(root)


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


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _normalize_ticker(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw.split(".")[0]


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
    broker_activity_by_ticker: dict[str, dict[str, Any]] = {}
    latest_submitted = {}
    latest_filled = {}
    unresolved_oldest_seconds = 0.0

    def _ticker_bucket(ticker: str) -> dict[str, Any]:
        key = _normalize_ticker(ticker)
        return broker_activity_by_ticker.setdefault(
            key,
            {
                "ticker": key,
                "submitted_count": 0,
                "filled_count": 0,
                "partial_filled_count": 0,
                "rejected_count": 0,
                "cancelled_count": 0,
                "unresolved_count": 0,
                "latest_submitted_line": None,
                "latest_filled_line": None,
                "latest_submitted_at": "",
                "latest_filled_at": "",
                "unresolved_oldest_seconds": 0.0,
            },
        )

    for record in records:
        if str(record.get("action") or "").strip().lower() == "place_order":
            response = record.get("response") if isinstance(record.get("response"), dict) else {}
            request = record.get("request") if isinstance(record.get("request"), dict) else {}
            order_id = str(response.get("order_id") or "").strip()
            status = _normalize_status(response.get("status"))
            ticker = _normalize_ticker(request.get("ticker"))
            bucket = _ticker_bucket(ticker) if ticker else None
            if order_id:
                submitted_orders[order_id] = {
                    "order_id": order_id,
                    "ticker": ticker,
                    "side": str(request.get("side") or "").lower(),
                    "qty": int(request.get("quantity") or 0),
                    "status": status,
                    "timestamp": record.get("timestamp") or "",
                }
            if status == "submitted":
                if bucket is not None:
                    bucket["submitted_count"] += 1
                    bucket["latest_submitted_line"] = " ".join(
                        str(part)
                        for part in [ticker, str(request.get("side") or "").lower(), int(request.get("quantity") or 0), "submitted"]
                        if part not in ("", None)
                    )
                    bucket["latest_submitted_at"] = record.get("timestamp") or ""
                latest_submitted = {
                    "order_id": order_id,
                    "ticker": ticker,
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
                submitted_meta = submitted_orders.get(order_id) or {}
                ticker = _normalize_ticker(submitted_meta.get("ticker"))
                bucket = _ticker_bucket(ticker) if ticker else None
                if bucket is not None:
                    if mapped_status == "filled":
                        bucket["filled_count"] += 1
                    elif mapped_status == "partially_filled":
                        bucket["partial_filled_count"] += 1
                    elif mapped_status == "rejected":
                        bucket["rejected_count"] += 1
                    elif mapped_status == "cancelled":
                        bucket["cancelled_count"] += 1
                if mapped_status in {"filled", "partially_filled"}:
                    latest_filled = {
                        "order_id": order_id,
                        "status": mapped_status,
                        "timestamp": record.get("timestamp") or "",
                    }
                    if bucket is not None:
                        bucket["latest_filled_line"] = " ".join(
                            str(part)
                            for part in [
                                ticker,
                                submitted_meta.get("side") or "",
                                submitted_meta.get("qty") or "",
                                mapped_status,
                            ]
                            if part not in ("", None)
                        )
                        bucket["latest_filled_at"] = record.get("timestamp") or ""

    order_status_counts = Counter(order_updates.values())
    unresolved_order_ids = sorted(
        order_id
        for order_id, payload in submitted_orders.items()
        if _normalize_status(payload.get("status")) == "submitted" and order_id not in order_updates
    )
    now_utc = datetime.now(timezone.utc)
    for order_id in unresolved_order_ids:
        payload = submitted_orders.get(order_id) or {}
        placed_at = _parse_timestamp(payload.get("timestamp"))
        ticker = _normalize_ticker(payload.get("ticker"))
        bucket = _ticker_bucket(ticker) if ticker else None
        if placed_at is None:
            if bucket is not None:
                bucket["unresolved_count"] += 1
            continue
        age_seconds = max(0.0, (now_utc - placed_at.astimezone(timezone.utc)).total_seconds())
        unresolved_oldest_seconds = max(unresolved_oldest_seconds, age_seconds)
        if bucket is not None:
            bucket["unresolved_count"] += 1
            bucket["unresolved_oldest_seconds"] = max(float(bucket.get("unresolved_oldest_seconds", 0.0) or 0.0), age_seconds)
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

    broker_activity_rows = sorted(
        (
            row
            for row in broker_activity_by_ticker.values()
            if row.get("ticker")
            and (
                int(row.get("submitted_count", 0) or 0) > 0
                or int(row.get("filled_count", 0) or 0) > 0
                or int(row.get("partial_filled_count", 0) or 0) > 0
                or int(row.get("unresolved_count", 0) or 0) > 0
            )
        ),
        key=lambda row: (
            -int(row.get("unresolved_count", 0) or 0),
            -int(row.get("submitted_count", 0) or 0),
            str(row.get("ticker") or ""),
        ),
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
        "broker_unresolved_oldest_seconds": round(unresolved_oldest_seconds, 1),
        "latest_submitted_line": latest_submitted_line,
        "latest_submitted_at": latest_submitted.get("timestamp") or "",
        "latest_filled_line": latest_filled_line,
        "latest_filled_at": latest_filled.get("timestamp") or "",
        "notification_reconcile_ok": len(unresolved_order_ids) == 0,
        "broker_activity_by_ticker": broker_activity_rows,
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
