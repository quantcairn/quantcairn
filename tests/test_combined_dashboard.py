from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.dashboard import combined


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = {}

    def setattr(self, target_or_obj, name_or_value, value=None):
        if value is None:
            obj = target_or_obj
            name = name_or_value
            new_value = None
        else:
            obj = target_or_obj
            name = name_or_value
            new_value = value

        if value is None:
            raise TypeError("SimpleMonkeyPatch.setattr requires explicit value")

        key = (obj, name)
        if key not in self._originals:
            self._originals[key] = getattr(obj, name)
        setattr(obj, name, new_value)

    def restore(self):
        for (obj, name), original in self._originals.items():
            setattr(obj, name, original)


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
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, mode=None: {
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

    assert "实盘账户" in html
    assert "购买力" in html
    assert "$425.00" in html
    assert "风控与交易审计" in html
    assert "仅减仓" in html
    assert "low_funds" in html
    assert "总本金：$850.00" in html
    assert "总权益：$1200.00" in html


def test_combined_dashboard_renders_ai_selection_report(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 1000.0,
        "support": 100.0,
        "resistance": 110.0,
    })
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: {
        "timestamp": "2026-06-30T09:29:00",
        "report": [
            {
                "rank": 1,
                "ticker": "NVDA",
                "score": 84.19,
                "volatility": 94.71,
                "volume": 81.89,
                "trend_fit": 58.0,
                "repeatability": 62.0,
                "drawdown": 55.0,
                "correlation_penalty": 0.0,
                "suggested_range": "$118.00 - $154.00",
                "sector": "Semiconductors",
            }
        ],
        "top5": [],
        "top3": [],
        "top10": [],
    })
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, mode=None: {
        "execution_mode": "live",
        "reduce_only": False,
        "new_entries_allowed": True,
        "risk_pause_reason": "",
        "decision_count": 0,
        "execution_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "order_qty": 0,
        "tickers": [],
        "latest_line": "",
    })

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "AI 区间选股" in html
    assert "最新选股时间：2026-06-30T09:29:00" in html
    assert "NVDA" in html
    assert "84.19" in html
    assert "$118.00 - $154.00" in html
    assert "TOP4" in html
    assert "TOP5" in html


def test_combined_dashboard_renders_separate_buy_sell_triggers(monkeypatch):
    ticker_map = {
        "TOP1.yaml": "NVDA",
        "TOP2.yaml": "TSLA",
        "TOP3.yaml": "MSFT",
        "TOP4.yaml": "GOOGL",
        "TOP5.yaml": "AMD",
    }
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, mode=None: {
        "execution_mode": "paper",
        "reduce_only": False,
        "new_entries_allowed": True,
        "risk_pause_reason": "",
        "decision_count": 0,
        "execution_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "order_qty": 0,
        "tickers": [],
        "latest_line": "",
    })
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": ticker_map.get(name, name.replace(".yaml", "")),
        "initial_capital": 1000.0,
        "support": 100.0,
        "resistance": 110.0,
    })

    statuses = {
        8091: {
            "ticker": "TOP1 · NVDA",
            "online": True,
            "price": 102.0,
            "price_change": 0.5,
            "day_high": 103.0,
            "day_low": 99.0,
            "bid": 101.9,
            "ask": 102.1,
            "volume": 1000000,
            "support": 100.0,
            "resistance": 110.0,
            "spread_pct": 10.0,
            "pos_pct": 20.0,
            "sparkline": [],
            "signal": "MONITORING",
            "shares": 0,
            "initial_capital": 1000.0,
            "cash": 1000.0,
            "pnl": 0.0,
            "equity": 1000.0,
            "trades_today": 0,
            "halted": False,
        },
        8092: {
            "ticker": "TOP2 · TSLA",
            "online": True,
            "price": 108.5,
            "price_change": 1.0,
            "day_high": 109.0,
            "day_low": 104.0,
            "bid": 108.4,
            "ask": 108.6,
            "volume": 2000000,
            "support": 100.0,
            "resistance": 110.0,
            "spread_pct": 10.0,
            "pos_pct": 85.0,
            "sparkline": [],
            "signal": "MONITORING",
            "shares": 0,
            "initial_capital": 1000.0,
            "cash": 1000.0,
            "pnl": 0.0,
            "equity": 1000.0,
            "trades_today": 0,
            "halted": False,
        },
    }
    monkeypatch.setattr(combined, "_fetch_status", lambda port: statuses.get(port))

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "最近触发买点" in html
    assert "TOP1 · NVDA 接近买点" in html
    assert "最近触发卖点" in html
    assert "TOP2 · TSLA 接近卖点" in html


def run_test_direct():
    monkeypatch = SimpleMonkeyPatch()
    try:
        test_combined_dashboard_renders_live_account_summary(monkeypatch)
        test_combined_dashboard_renders_ai_selection_report(monkeypatch)
        test_combined_dashboard_renders_separate_buy_sell_triggers(monkeypatch)
    finally:
        monkeypatch.restore()


if __name__ == "__main__":
    run_test_direct()
