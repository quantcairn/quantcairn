from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.dashboard import combined


def test_combined_dashboard_renders_live_account_summary(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: {
        "cash": 850.0,
        "equity": 1200.0,
        "buying_power": 425.0,
        "positions_count": 3,
        "mode": "live",
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 0.0,
        "support": 0.0,
        "resistance": 0.0,
    })
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir: {
        "execution_mode": "live",
        "reduce_only": True,
        "new_entries_allowed": False,
        "risk_pause_reason": "low_funds",
        "decision_count": 4,
        "execution_count": 2,
        "buy_count": 1,
        "sell_count": 1,
        "order_qty": 3,
        "tickers": ["AAPL.US"],
        "latest_line": "execution AAPL.US buy 1 submitted",
        "latest_execution": {
            "ticker": "AAPL.US",
            "strategy": "RangeStrategy",
            "order": {"side": "buy", "qty": 1},
            "response": {"status": "submitted"},
        },
    })

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "Live Account" in html
    assert "Buying Power" in html
    assert "$425.00" in html
    assert "Session Risk & Trade Audit" in html
    assert "Reduce Only" in html
    assert "low_funds" in html
