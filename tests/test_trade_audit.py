from __future__ import annotations

import json
from pathlib import Path

from src.reports.trade_audit import latest_trade_activity_day, latest_trade_log_day, summarize_trade_log, summarize_trade_records


def test_trade_audit_summarizes_latest_execution(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "trades-20260628.jsonl"
    records = [
        {
            "phase": "decision",
            "ticker": "AAPL.US",
            "execution_mode": "paper",
            "reduce_only": True,
            "risk_pause_reason": "low_funds",
        },
        {
            "phase": "execution",
            "ticker": "AAPL.US",
            "strategy": "RangeStrategy",
            "execution_mode": "live",
            "reduce_only": True,
            "risk_pause_reason": "low_funds",
            "order": {"side": "buy", "qty": 1},
            "response": {"status": "submitted"},
        },
    ]
    log_path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    summary = summarize_trade_log(log_dir, day="20260628")

    assert summary["execution_mode"] == "live"
    assert summary["reduce_only"] is True
    assert summary["risk_pause_reason"] == "low_funds"
    assert summary["execution_count"] == 1
    assert summary["decision_count"] == 1
    assert summary["latest_execution"]["ticker"] == "AAPL.US"
    assert summary["latest_line"] == "execution AAPL.US buy 1 submitted"


def test_trade_audit_counts_orders():
    summary = summarize_trade_records(
        [
            {"phase": "decision", "ticker": "AAPL.US"},
            {"phase": "execution", "ticker": "AAPL.US", "order": {"side": "buy", "qty": 2}},
            {"phase": "execution", "ticker": "MSFT.US", "order": {"side": "sell", "qty": 1}},
        ]
    )

    assert summary["order_qty"] == 3
    assert summary["buy_count"] == 1
    assert summary["sell_count"] == 1
    assert summary["tickers"] == ["AAPL.US", "MSFT.US"]


def test_trade_audit_can_filter_live_records():
    summary = summarize_trade_records(
        [
            {"phase": "decision", "ticker": "AAPL.US", "execution_mode": "paper"},
            {"phase": "execution", "ticker": "AAPL.US", "execution_mode": "paper", "order": {"side": "buy", "qty": 2}},
            {"phase": "decision", "ticker": "NVDA.US", "execution_mode": "live"},
            {"phase": "execution", "ticker": "NVDA.US", "execution_mode": "live", "order": {"side": "sell", "qty": 1}},
        ],
        mode="live",
    )

    assert summary["execution_mode"] == "live"
    assert summary["decision_count"] == 1
    assert summary["execution_count"] == 1
    assert summary["tickers"] == ["NVDA.US"]


def test_trade_audit_summarizes_broker_order_reconcile():
    summary = summarize_trade_records(
        [
            {
                "timestamp": "2026-07-03T01:00:00Z",
                "action": "place_order",
                "request": {"ticker": "SOFI", "side": "SELL", "quantity": 30},
                "response": {"order_id": "OID-1", "status": "submitted"},
                "execution_mode": "live",
            },
            {
                "timestamp": "2026-07-03T01:00:05Z",
                "action": "get_order",
                "request": {"order_id": "OID-1"},
                "response": {"mapped_status": "FILLED"},
                "execution_mode": "live",
            },
            {
                "timestamp": "2026-07-03T01:10:00Z",
                "action": "place_order",
                "request": {"ticker": "PLTR", "side": "BUY", "quantity": 2},
                "response": {"order_id": "OID-2", "status": "submitted"},
                "execution_mode": "live",
            },
        ],
        mode="live",
    )

    assert summary["broker_submitted_count"] == 2
    assert summary["broker_filled_count"] == 1
    assert summary["broker_unresolved_count"] == 1
    assert summary["notification_reconcile_ok"] is False
    assert summary["latest_submitted_line"] == "PLTR buy 2 submitted"
    assert summary["latest_filled_line"] == "SOFI sell 30 filled"


def test_trade_audit_tracks_unresolved_age_and_latest_day(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "trades-20260701.jsonl").write_text("", encoding="utf-8")
    (log_dir / "trades-20260703.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-07-03T00:00:00Z",
                "action": "place_order",
                "request": {"ticker": "SOFI", "side": "SELL", "quantity": 30},
                "response": {"order_id": "OID-3", "status": "submitted"},
                "execution_mode": "live",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_trade_log(log_dir, day="20260703", mode="live")

    assert latest_trade_log_day(log_dir) == "20260703"
    assert summary["broker_unresolved_count"] == 1
    assert summary["broker_unresolved_oldest_seconds"] >= 0


def test_trade_audit_prefers_latest_day_with_broker_activity(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "trades-20260703.jsonl").write_text(
        json.dumps({"phase": "decision", "execution_mode": "live", "ticker": "SOFI"}),
        encoding="utf-8",
    )
    (log_dir / "trades-20260702.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-07-02T13:30:11Z",
                "action": "place_order",
                "request": {"ticker": "SOXS", "side": "SELL", "quantity": 132},
                "response": {"order_id": "OID-4", "status": "submitted"},
                "execution_mode": "live",
            }
        ),
        encoding="utf-8",
    )

    assert latest_trade_activity_day(log_dir, mode="live") == "20260702"
