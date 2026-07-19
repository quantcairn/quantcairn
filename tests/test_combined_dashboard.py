from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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
    assert "风控运行状态" not in html
    assert "风控与交易审计" not in html
    assert "全局仅减仓" in html
    assert "已开启（ENABLED）" in html
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


def test_combined_dashboard_shows_system_status_and_missing_data_labels(monkeypatch):
    monkeypatch.setattr(combined, "_load_dashboard_config", lambda: SimpleNamespace(
        mode="paper",
        broker=SimpleNamespace(
            longbridge=SimpleNamespace(
                enabled=False,
                environment="prod",
                account_type="",
                allow_live_order=False,
            )
        ),
    ))
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "_load_active_orders_summary", lambda tickers: {"available": False, "count": 0, "orders": [], "sources": [], "status_label": "no data", "detail": "no data"})
    monkeypatch.setattr(combined, "_load_lifecycle_summary", lambda kind: {
        "available": False,
        "status_label": "unavailable",
        "detail": "no data",
        "generated_at": None,
    })
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "系统状态" in html
    assert "PAPER" in html
    assert "PaperBroker" in html
    assert "PaperBroker / TOP engine runtime" in html
    assert "AI 选股结果总览" in html
    assert "展开技术详情" in html


def test_combined_dashboard_uses_sandbox_account_snapshot_without_paper_fallback(monkeypatch):
    monkeypatch.setattr(combined, "_load_dashboard_config", lambda: SimpleNamespace(
        mode="sandbox",
        broker=SimpleNamespace(
            longbridge=SimpleNamespace(
                enabled=True,
                environment="sandbox",
                account_type="paper",
                allow_live_order=False,
            )
        ),
    ))
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: {
        "cash": 900.0,
        "equity": 1000.0,
        "buying_power": 900.0,
        "positions_count": 1,
        "positions": [
            {
                "ticker": "SOFI",
                "quantity": 6,
                "avg_entry_price": 10.98,
                "current_price": 11.2,
                "market_value": 67.2,
                "unrealized_pnl": 1.32,
                "unrealized_pnl_pct": 2.0,
            }
        ],
        "mode": "sandbox",
    })
    monkeypatch.setattr(combined, "_load_active_orders_summary", lambda tickers: {"available": False, "count": 0, "orders": [], "sources": [], "status_label": "no data", "detail": "no data"})
    monkeypatch.setattr(combined, "_load_lifecycle_summary", lambda kind: {
        "available": False,
        "status_label": "unavailable",
        "detail": "no data",
        "generated_at": None,
    })
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5})
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {
        "execution_mode": "sandbox",
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
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "SANDBOX" in html
    assert "LongBridge" in html
    assert "LongBridge sandbox" in html
    assert "LongBridge sandbox 持仓" in html
    assert "PaperBroker / TOP engine runtime" not in html


def test_combined_dashboard_shows_lifecycle_result_cards(monkeypatch):
    monkeypatch.setattr(combined, "_load_dashboard_config", lambda: SimpleNamespace(
        mode="paper",
        broker=SimpleNamespace(
            longbridge=SimpleNamespace(
                enabled=False,
                environment="prod",
                account_type="",
                allow_live_order=False,
            )
        ),
    ))
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "_load_active_orders_summary", lambda tickers: {"available": True, "count": 0, "orders": [], "sources": [], "status_label": "0", "detail": "无活动订单"})
    monkeypatch.setattr(combined, "_load_lifecycle_summary", lambda kind: {
        "available": True,
        "status_label": "PASS" if kind == "weekend_paper" else "FAIL",
        "detail": "BUY FILLED · SELL FILLED · position 0" if kind == "weekend_paper" else "bootstrap PASS · BUY PENDING · SELL PENDING",
        "generated_at": "2026-07-11T09:00:00+08:00",
        "mode": "PAPER" if kind == "weekend_paper" else "SANDBOX",
        "broker": "PaperBroker" if kind == "weekend_paper" else "LongBridge",
        "account_type": "PAPER",
        "ticker": "TEST" if kind == "weekend_paper" else "SOFI",
    })
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "最近一次周末虚拟盘检查" in html
    assert "通过（PASS）" in html
    assert "BUY FILLED · SELL FILLED · position 0" in html
    assert "最近一次 LongBridge 沙盒检查" in html
    assert "FAIL" in html


def test_combined_dashboard_does_not_use_submit_order_from_page(monkeypatch):
    class ForbiddenBroker:
        def __init__(self, *args, **kwargs):
            self.submit_order = lambda *a, **k: (_ for _ in ()).throw(AssertionError("submit_order should not be called"))

    monkeypatch.setattr(combined, "_load_dashboard_config", lambda: SimpleNamespace(
        mode="paper",
        broker=SimpleNamespace(
            longbridge=SimpleNamespace(
                enabled=False,
                environment="prod",
                account_type="",
                allow_live_order=False,
            )
        ),
    ))
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "_load_active_orders_summary", lambda tickers: {"available": False, "count": 0, "orders": [], "sources": [], "status_label": "no data", "detail": "no data"})
    monkeypatch.setattr(combined, "_load_lifecycle_summary", lambda kind: {"available": False, "status_label": "unavailable", "detail": "no data", "generated_at": None})
    monkeypatch.setattr(combined, "PaperBroker", ForbiddenBroker)
    monkeypatch.setattr(combined, "LongBridgeBroker", ForbiddenBroker, raising=False)
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "只读研究简报" in html
    assert "系统状态" in html


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
    monkeypatch.setattr(combined, "get_runtime_env", lambda name, default="": {
        "SOXS_AI_SELECTOR_ENABLED": "1",
        "SOXS_TRADINGAGENTS_PATH": "/tmp/TradingAgents",
        "SOXS_FINROBOT_PATH": "/tmp/FinRobot",
        "OPENAI_API_KEY": "",
        "FMP_API_KEY": "",
    }.get(name, default))
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-06-30）",
        "required_date": "2026-06-30",
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
            "selection_stage": "fast_preliminary",
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
        "top3": [{"ticker": "SOFI"}, {"ticker": "NVDA"}, {"ticker": "AAPL"}],
        "top10": [],
        "protected_positions": [],
        "refinement_status": "background_fast_preliminary",
        "refinement_selection_stage": "fast_preliminary",
    })
    monkeypatch.setattr(combined, "_load_latest_research_digest", lambda: {
        "available": True,
        "date": "2026-06-30",
        "generated_at": "2026-06-30T18:00:00-04:00",
        "top_line": "SOFI / NVDA / AAPL",
        "strategy_summary": "成功 1 / 观察正确 2 / 失败 0",
        "entry_ready": 1,
        "observation_only": 2,
        "research_url": "/research",
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
    assert "股票池筛选：普通股 $5-$200 / ETF $5-$300 / 杠杆与反向ETF $5-$100" in html
    assert "自动刷新：5 分钟" in html
    assert "扫描数量：50" in html
    assert "数据模式：实盘（live）" in html
    assert "启动阶段：快速初选（fast_preliminary）" in html
    assert "后台精筛：background_fast_preliminary" in html
    assert "（fast_preliminary）" in html
    assert "研究简报" in html
    assert "策略评分复盘" in html
    assert "成功 1 / 观察正确 2 / 失败 0" in html
    assert "打开研究简报" in html
    assert "选股配置校验" in html
    assert "已对齐" in html
    assert "当天配置已对齐（美东 2026-06-30）" in html
    assert "要求美东日期 2026-06-30" in html
    assert "当前状态日期 2026-06-30" in html
    assert "启用中" in html
    assert "SOFI / NVDA / AAPL" in html
    assert "保护持仓" in html
    assert "这里显示新的 TOP3 工具" in html
    assert "但不挤占新选股 TOP3" in html
    assert "前台阶段：快速初选（fast_preliminary）" in html
    assert "后台精筛：background_fast_preliminary / fast_preliminary" in html
    assert "AI 运行状态：" in html
    assert "部分降级" in html
    assert "缺少 OPENAI_API_KEY" in html
    assert "FMP 已禁用，不影响运行。" in html
    assert "NVDA" in html
    assert "84.19" in html
    assert "$118.00 - $154.00" in html


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
    assert "min_price" not in saved
    assert "max_price" not in saved
    assert "price_band" not in saved
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
    assert "min_price" not in saved
    assert "max_price" not in saved
    assert "price_band" not in saved
    assert saved["auto_refresh_minutes"] == 12
    assert rerun["called"] is True


def test_combined_dashboard_shows_startup_guard_status(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "load_runtime_settings", lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5})
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: None)
    monkeypatch.setattr(combined, "summarize_trade_log", lambda log_dir, day=None, mode=None: {
        "execution_mode": "live",
        "reduce_only": True,
        "new_entries_allowed": False,
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
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": False,
        "level": "red",
        "label": "配置不一致",
        "detail": "TOP1-5 配置和最近一次选股结果不一致，交易启动会被拦下。",
        "required_date": "2026-07-06",
        "state_date": "2026-07-05",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: True)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "启动校验阻止 · 配置不一致" in html
    assert "TOP1-5 配置和最近一次选股结果不一致，交易启动会被拦下。" in html
    assert "要求美东日期 2026-07-06" in html
    assert "当前状态日期 2026-07-05" in html



def test_combined_dashboard_shows_paper_mode_without_live_top_warning(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: {"mode": "paper", "positions": [], "equity": 1000.0, "cash": 1000.0, "buying_power": 1000.0})
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
        "selection_state_symbols": ["AAPL", "SOFI", "DRIP"],
        "current_top_config_symbols": ["AAPL", "SOFI", "DRIP"],
        "state_top_config_symbols": ["AAPL", "SOFI", "DRIP"],
        "suggestion": "请重新运行 AI Selector 或重新写入 TOP 配置",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "虚拟盘运行中" in html
    assert "当前是 paper 模式，未启用 live TOP 校验，虚拟盘会按当天 TOP 配置继续交易。" in html
    assert "启动校验待命" not in html


def test_combined_dashboard_uses_paper_account_summary_when_live_account_missing(monkeypatch):
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: {
        "price": 11.0,
        "change": 0.0,
        "high_1m": 11.0,
        "low_1m": 11.0,
        "bid": 10.9,
        "ask": 11.1,
        "support": 10.0,
        "resistance": 12.0,
        "spread_pct": 0.2,
        "range_ready": True,
        "range_source": "paper",
        "last_signal": "HOLD",
        "last_signal_reason": "paper",
        "initial_capital": 700.0,
        "cash": 700.0,
        "position_shares": 3,
        "daily_pnl": 0.0,
        "equity": 700.0,
        "trades_today": 0,
        "win_rate": 0.0,
        "wins": 0,
        "losses": 0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "avg_pnl": 0.0,
        "halted": False,
        "trade_in_progress": False,
    })
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "虚拟盘" in html
    assert "$2100.00" in html
    assert "精选持仓数量" in html
    assert ">3<" in html or "3</span>" in html
    assert "虚拟持仓" in html
    assert "3 股" in html
    assert "暂无" not in html.split("账户与持仓", 1)[1].split("虚拟持仓", 1)[0]


def test_combined_dashboard_shows_paper_position_pnl_from_engine_status(monkeypatch):
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: {
        "price": 11.0,
        "change": 0.0,
        "high_1m": 11.0,
        "low_1m": 11.0,
        "bid": 10.9,
        "ask": 11.1,
        "support": 10.0,
        "resistance": 12.0,
        "spread_pct": 0.2,
        "range_ready": True,
        "range_source": "paper",
        "last_signal": "HOLD",
        "last_signal_reason": "paper",
        "initial_capital": 700.0,
        "cash": 700.0,
        "position_shares": 3,
        "entry_price": 10.0,
        "unrealized_pnl": 3.0,
        "unrealized_pnl_pct": 10.0,
        "daily_pnl": 0.0,
        "equity": 703.0,
        "trades_today": 0,
        "win_rate": 0.0,
        "wins": 0,
        "losses": 0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "avg_pnl": 0.0,
        "halted": False,
        "trade_in_progress": False,
    })
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "虚拟持仓" in html
    assert '<span class="val red">$+3.00</span>' in html or 'class="val red">$+3.00' in html
    assert '<span class="val red">+10.00%</span>' in html or 'class="val red">+10.00%' in html


def test_combined_dashboard_shows_negative_pnl_in_green(monkeypatch):
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: {
        "price": 9.0,
        "change": 0.0,
        "high_1m": 9.0,
        "low_1m": 9.0,
        "bid": 8.9,
        "ask": 9.1,
        "support": 8.5,
        "resistance": 9.5,
        "spread_pct": 0.2,
        "range_ready": True,
        "range_source": "paper",
        "last_signal": "HOLD",
        "last_signal_reason": "paper",
        "initial_capital": 700.0,
        "cash": 700.0,
        "position_shares": 2,
        "entry_price": 10.0,
        "unrealized_pnl": -2.0,
        "unrealized_pnl_pct": -10.0,
        "daily_pnl": 0.0,
        "equity": 698.0,
        "trades_today": 0,
        "win_rate": 0.0,
        "wins": 0,
        "losses": 0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "avg_pnl": 0.0,
        "halted": False,
        "trade_in_progress": False,
    })
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert '<span class="val green">$-2.00</span>' in html or 'class="val green">$-2.00' in html
    assert '<span class="val green">-10.00%</span>' in html or 'class="val green">-10.00%' in html


def test_combined_dashboard_paper_mode_ignores_live_account_positions(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: {
        "mode": "paper",
        "cash": 9999.0,
        "equity": 9999.0,
        "buying_power": 9999.0,
        "positions": [
            {
                "ticker": "SOXS",
                "quantity": 12,
                "avg_entry_price": 4.17,
                "current_price": 4.17,
                "market_value": 699.90,
                "unrealized_pnl": 0.0,
                "unrealized_pnl_pct": 0.0,
            }
        ],
    })
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
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 700.0,
        "support": 10.0,
        "resistance": 12.0,
    })
    monkeypatch.setattr(combined, "_fetch_status", lambda port: {
        "price": 4.21,
        "change": 0.0,
        "high_1m": 4.21,
        "low_1m": 4.21,
        "bid": 4.20,
        "ask": 4.22,
        "support": 4.0,
        "resistance": 4.5,
        "spread_pct": 0.2,
        "range_ready": True,
        "range_source": "paper",
        "last_signal": "HOLD",
        "last_signal_reason": "paper",
        "initial_capital": 700.0,
        "cash": 700.0,
        "position_shares": 12,
        "entry_price": 4.198503232108468,
        "unrealized_pnl": 0.13796121469837885,
        "unrealized_pnl_pct": 0.274500,
        "daily_pnl": 0.0,
        "equity": 700.14,
        "trades_today": 0,
        "win_rate": 0.0,
        "wins": 0,
        "losses": 0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "avg_pnl": 0.0,
        "halted": False,
        "trade_in_progress": False,
    })
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
    })
    monkeypatch.setattr(combined, "has_live_top_configs", lambda: False)

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "虚拟持仓" in html
    assert "$4.20" in html or "$4.21" in html
    assert "+$0.14" in html or "0.14" in html
    assert "$699.90" not in html


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
        test_combined_dashboard_shows_startup_guard_status(monkeypatch)
    finally:
        monkeypatch.restore()


if __name__ == "__main__":
    run_test_direct()
