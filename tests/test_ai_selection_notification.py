from __future__ import annotations

from src.notifier import alerts


def _sample_top(rank: int, ticker: str, *, fallback_used: bool = False, reason: str = "High range fitness") -> dict:
    return {
        "ticker": ticker,
        "selection": {
            "selection_date": "2026-07-09",
            "ai_score": 82 + rank,
            "range_score": 90 - rank,
            "final_score": 88 - rank,
            "trade_filter_passed": True,
            "leveraged_etf": ticker in {"SOXS", "LABD", "DRIP"},
            "fallback_used": fallback_used,
            "reason": reason,
        },
        "allocation": {
            "target_capital": 315.0,
            "target_shares": max(1, 65 - rank),
        },
    }


def _sample_report(*, fallback_used: bool = False, selection_count: int = 3) -> dict:
    return {
        "selection_date": "2026-07-09",
        "selection_count": selection_count,
        "target_top_n": 3,
        "fallback_used": fallback_used,
        "providers_used": ["TradingAgents", "FinRobot", "OpenBB"],
        "providers_disabled": ["FMP"],
        "quality_filter_report": {"warnings": ["TradingAgents timeout"]},
        "composition_filter": {"warnings": ["leveraged_etf_limit_reached"]},
    }


def test_ai_selection_message_includes_top3():
    title, body = alerts._build_ai_selection_message(
        _sample_report(),
        [
            _sample_top(1, "SOXS"),
            _sample_top(2, "AAPL"),
            _sample_top(3, "SOFI"),
        ],
    )

    assert title == "【AI 选股完成】"
    assert "TOP1：SOXS" in body
    assert "TOP2：AAPL" in body
    assert "TOP3：SOFI" in body
    assert "Provider：使用：TradingAgents, FinRobot, OpenBB" in body


def test_ai_selection_message_handles_only_top2():
    _, body = alerts._build_ai_selection_message(
        _sample_report(selection_count=2),
        [
            _sample_top(1, "SOXS"),
            _sample_top(2, "SOFI"),
        ],
    )

    assert "TOP数量：2/3" in body
    assert "TOP3：未生成 / disabled" in body
    assert "原因：top_n_not_filled" in body


def test_ai_selection_message_warns_on_fallback():
    _, body = alerts._build_ai_selection_message(
        _sample_report(fallback_used=True),
        [
            _sample_top(1, "SOXS", fallback_used=True),
            _sample_top(2, "SOFI"),
            _sample_top(3, "AAPL"),
        ],
    )

    assert "fallback：true" in body
    assert "不建议直接 live" in body


def test_notify_ai_selection_without_telegram_does_not_raise(monkeypatch):
    monkeypatch.setattr(alerts, "_load_notification_config", lambda: {})
    monkeypatch.delenv("SOXS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SOXS_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("AI_SELECTOR_WEBHOOK", raising=False)

    alerts.notify_ai_selection_result(
        _sample_report(),
        [_sample_top(1, "SOXS"), _sample_top(2, "SOFI"), _sample_top(3, "AAPL")],
    )


def test_ai_selection_message_truncates_long_reason():
    _, body = alerts._build_ai_selection_message(
        _sample_report(),
        [
            _sample_top(1, "SOXS", reason="A" * 200),
            _sample_top(2, "SOFI"),
            _sample_top(3, "AAPL"),
        ],
    )

    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA…" in body
    assert "A" * 120 not in body


def test_notify_ai_selection_result_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(alerts, "_load_notification_config", lambda: {"webhook_url": "https://example.com/hook"})

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self._telegram_enabled = False
            self.webhook_url = "https://example.com/hook"

        def _send(self, *args, **kwargs):
            raise RuntimeError("telegram down")

    monkeypatch.setattr(alerts, "Notifier", FakeNotifier)

    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])
