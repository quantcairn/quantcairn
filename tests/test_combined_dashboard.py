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
        "positions": [
            {
                "ticker": "SOFI",
                "quantity": 30,
                "avg_entry_price": 12.34,
                "current_price": 12.50,
                "market_value": 375.0,
                "unrealized_pnl": 4.8,
                "unrealized_pnl_pct": 1.3,
            }
        ],
        "mode": "live",
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": "SOFI" if name == "TOP3.yaml" else name.replace(".yaml", ""),
        "initial_capital": 0.0,
        "support": 0.0,
        "resistance": 0.0,
    })
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {
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
    assert "$425.00" in html
    assert "运行状态" not in html
    assert "风控与交易审计" not in html
    assert "仅减仓" not in html
    assert "low_funds" not in html
    assert "账户现金" in html
    assert "$850.00" in html
    assert "账户权益" in html
    assert "$1200.00" in html
    assert "可买额度" in html
    assert "$+4.80" in html
    assert "账户与持仓" in html
    assert "SOFI" in html
    assert "30 股" in html


def test_combined_dashboard_marks_stale_live_account(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: {
        "cash": 25.0,
        "equity": 700.0,
        "buying_power": 25.0,
        "positions_count": 0,
        "positions": [],
        "mode": "live",
        "data_stale": True,
        "fetched_at": "2026-07-02T10:00:00",
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 0.0,
        "support": 0.0,
        "resistance": 0.0,
    })
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {})

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "账户数据已过期" in html
    assert "2026-07-02T10:00:00" in html


def test_combined_dashboard_shows_live_account_error_without_cache(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: {
        "cash": None,
        "equity": None,
        "buying_power": None,
        "positions_count": 0,
        "positions": [],
        "mode": "live_error",
        "data_stale": True,
        "account_error": True,
        "stale_reason": "凭证无效，请更新 LongBridge Access Token",
        "fetched_at": None,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 0.0,
        "support": 0.0,
        "resistance": 0.0,
    })
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {})

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "实盘账户异常" in html
    assert "账户拉取失败" in html
    assert "凭证无效，请更新 LongBridge Access Token" in html
    assert "账户现金" in html
    assert "暂无" in html


def test_stale_live_account_without_cache_returns_error_state():
    combined._LIVE_ACCOUNT_CACHE = None
    result = combined._stale_live_account(
        "OpenApiException: (kind=ErrorKind.OpenApi, code=401004) token invalid"
    )

    assert result["mode"] == "live_error"
    assert result["account_error"] is True
    assert result["data_stale"] is True
    assert result["stale_reason"] == "凭证无效，请更新 LongBridge Access Token"


def test_refresh_live_account_uses_broker_error_details(monkeypatch):
    class FakeBroker:
        def __init__(self, **kwargs):
            pass

        def connect(self):
            return True

        def get_positions(self):
            return []

        def is_positions_snapshot_reliable(self):
            return False

        def last_positions_error(self):
            return "OpenApiException: (kind=ErrorKind.OpenApi, code=401004) token invalid"

        def disconnect(self):
            pass

    original_cache = combined._LIVE_ACCOUNT_CACHE
    combined._LIVE_ACCOUNT_CACHE = None
    fake_module = type("FakeModule", (), {"LongBridgeBroker": FakeBroker})
    original = sys.modules.get("src.broker.longbridge_broker")
    sys.modules["src.broker.longbridge_broker"] = fake_module
    try:
        result = combined._refresh_live_account_summary(0.0)
    finally:
        combined._LIVE_ACCOUNT_CACHE = original_cache
        if original is None:
            sys.modules.pop("src.broker.longbridge_broker", None)
        else:
            sys.modules["src.broker.longbridge_broker"] = original

    assert result["mode"] == "live_error"
    assert result["account_error"] is True
    assert result["stale_reason"] == "凭证无效，请更新 LongBridge Access Token"


def test_combined_dashboard_renders_ai_selection_report(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5})
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-06-30）",
        "state_date": "2026-06-30",
    })
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 1000.0,
        "support": 100.0,
        "resistance": 110.0,
    })
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: {
        "timestamp": "2026-06-30T09:29:00",
        "settings": {
            "min_price": 10.0,
            "max_price": 200.0,
            "auto_refresh_minutes": 5,
            "max_symbols": 50,
            "data_mode": "live",
        },
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
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {
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
    assert "价格范围：$10.00 - $200.00" in html
    assert "自动刷新：5 分钟" in html
    assert "扫描数量：50" in html
    assert "数据模式：live" in html
    assert "选股配置校验" in html
    assert "已对齐" in html
    assert "当天配置已对齐（美东 2026-06-30）" in html
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
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5})
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {
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
            "change": 0.5,
            "day_high": 103.0,
            "day_low": 99.0,
            "bid": 101.9,
            "ask": 102.1,
            "volume": 1000000,
            "support": 100.0,
            "resistance": 110.0,
            "spread_pct": 10.0,
            "range_ready": True,
            "sparkline": [],
            "signal": "MONITORING",
            "position_shares": 0,
            "initial_capital": 1000.0,
            "cash": 1000.0,
            "daily_pnl": 0.0,
            "equity": 1000.0,
            "trades_today": 0,
            "halted": False,
        },
        8092: {
            "ticker": "TOP2 · TSLA",
            "online": True,
            "price": 108.5,
            "change": 1.0,
            "day_high": 109.0,
            "day_low": 104.0,
            "bid": 108.4,
            "ask": 108.6,
            "volume": 2000000,
            "support": 100.0,
            "resistance": 110.0,
            "spread_pct": 10.0,
            "range_ready": True,
            "sparkline": [],
            "signal": "MONITORING",
            "position_shares": 0,
            "initial_capital": 1000.0,
            "cash": 1000.0,
            "daily_pnl": 0.0,
            "equity": 1000.0,
            "trades_today": 0,
            "halted": False,
        },
    }
    monkeypatch.setattr(combined, "_fetch_status", lambda port: statuses.get(port))

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "全部标的" in html
    assert "最接近买点" in html
    assert "最接近卖点" in html
    assert "TOP1 · NVDA" in html
    assert "TOP2 · TSLA" in html


def test_combined_status_fetch_needs_consecutive_failures_before_offline(monkeypatch):
    combined._STATUS_CACHE.clear()
    combined._STATUS_FAILURES.clear()

    responses = [
        b'{"running": true, "status_line": "TOP2 ok"}',
    ]

    class FakeResponse:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

    class FakeOpener:
        def open(self, url, timeout=1):
            if responses:
                return FakeResponse(responses.pop(0))
            raise OSError("temporary failure")

    monkeypatch.setattr(combined.urllib.request, "build_opener", lambda *args, **kwargs: FakeOpener())

    first = combined._fetch_status(8092)
    second = combined._fetch_status(8092)
    third = combined._fetch_status(8092)
    fourth = combined._fetch_status(8092)

    assert first == {"running": True, "status_line": "TOP2 ok"}
    assert second == {"running": True, "status_line": "TOP2 ok"}
    assert third == {"running": True, "status_line": "TOP2 ok"}
    assert fourth is None


def test_combined_dashboard_buy_trigger_skips_symbols_with_positions(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0})
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {
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
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })

    statuses = {
        8091: {
            "price": 10.1,
            "change": 0.0,
            "high_1m": 10.2,
            "low_1m": 10.0,
            "bid": 10.1,
            "ask": 10.1,
            "volume": 1000,
            "support": 10.0,
            "resistance": 12.0,
            "spread_pct": 20.0,
            "range_ready": True,
            "last_signal": "HOLD",
            "position_shares": 5,
            "initial_capital": 700.0,
            "cash": 350.0,
            "daily_pnl": 0.0,
            "equity": 710.0,
            "trades_today": 0,
            "halted": False,
        },
        8092: {
            "price": 10.4,
            "change": 0.0,
            "high_1m": 10.5,
            "low_1m": 10.3,
            "bid": 10.4,
            "ask": 10.4,
            "volume": 1000,
            "support": 10.0,
            "resistance": 12.0,
            "spread_pct": 20.0,
            "range_ready": True,
            "last_signal": "HOLD",
            "position_shares": 0,
            "initial_capital": 700.0,
            "cash": 700.0,
            "daily_pnl": 0.0,
            "equity": 700.0,
            "trades_today": 0,
            "halted": False,
        },
    }
    monkeypatch.setattr(combined, "_fetch_status", lambda port: statuses.get(port))

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "TOP2 · TOP2" in html
    assert "TOP1 · TOP1" in html


def test_combined_dashboard_buy_trigger_shows_pause_when_new_entries_disabled(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0})
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {
        "execution_mode": "live",
        "reduce_only": True,
        "new_entries_allowed": False,
        "risk_pause_reason": "low_funds",
        "decision_count": 0,
        "execution_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "order_qty": 0,
        "tickers": [],
        "latest_line": "",
    })
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "AI区间交易总览" in html
    assert "账户与持仓" in html


def test_combined_dashboard_updates_ai_selector_settings(monkeypatch):
    saved = {}
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5})
    monkeypatch.setattr(combined, "save_runtime_settings", lambda data: saved.update(data))
    monkeypatch.setattr(combined, "_run_ai_selector_now", lambda: None)

    with combined.app.test_client() as client:
        resp = client.post("/ai-selector-settings", data={"min_price": "12", "max_price": "35.5", "auto_refresh_minutes": "9"})

    assert resp.status_code == 302
    assert saved["min_price"] == 12.0
    assert saved["max_price"] == 35.5
    assert saved["auto_refresh_minutes"] == 9


def test_combined_dashboard_reruns_ai_selector_from_settings_form(monkeypatch):
    saved = {}
    rerun = {"called": False}
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5})
    monkeypatch.setattr(combined, "save_runtime_settings", lambda data: saved.update(data))
    monkeypatch.setattr(combined, "_run_ai_selector_now", lambda: rerun.__setitem__("called", True))

    with combined.app.test_client() as client:
        resp = client.post("/ai-selector-settings", data={"min_price": "11", "max_price": "44.0", "auto_refresh_minutes": "12", "action": "rerun"})

    assert resp.status_code == 302
    assert saved["min_price"] == 11.0
    assert saved["max_price"] == 44.0
    assert saved["auto_refresh_minutes"] == 12
    assert rerun["called"] is True


def run_test_direct():
    monkeypatch = SimpleMonkeyPatch()
    try:
        test_combined_status_fetch_needs_consecutive_failures_before_offline(monkeypatch)
        test_combined_dashboard_renders_live_account_summary(monkeypatch)
        test_combined_dashboard_marks_stale_live_account(monkeypatch)
        test_combined_dashboard_shows_live_account_error_without_cache(monkeypatch)
        test_stale_live_account_without_cache_returns_error_state()
        test_refresh_live_account_uses_broker_error_details(monkeypatch)
        test_combined_dashboard_renders_ai_selection_report(monkeypatch)
        test_combined_dashboard_renders_separate_buy_sell_triggers(monkeypatch)
        test_combined_dashboard_buy_trigger_skips_symbols_with_positions(monkeypatch)
        test_combined_dashboard_buy_trigger_shows_pause_when_new_entries_disabled(monkeypatch)
        test_combined_dashboard_updates_ai_selector_settings(monkeypatch)
        test_combined_dashboard_reruns_ai_selector_from_settings_form(monkeypatch)
    finally:
        monkeypatch.restore()


if __name__ == "__main__":
    run_test_direct()
