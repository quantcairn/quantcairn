from __future__ import annotations

import json
from pathlib import Path

from src.reports.trade_audit import summarize_trade_log, summarize_trade_records


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
