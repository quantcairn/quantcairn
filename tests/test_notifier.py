import logging

import requests

from src.notifier import alerts
from src.notifier.alerts import Notifier, _build_public_channel_message, _resolve_selection_market_state


def _sample_top(ticker: str = "SOXS", score: float = 88.0) -> dict:
    return {
        "ticker": ticker,
        "score": score,
        "final_score": score,
        "ai_score": score,
        "range_score": score - 1.0,
        "current_price": 10.0,
        "size": 10,
        "leveraged_etf": False,
        "trade_filter_passed": True,
        "current_validation_status": "DATA_VALID",
        "trade_admission_status": "TRADABLE",
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "score_source": "current_run_candidate_ranking",
        "score_provider": "local_factor_scoring",
        "score_generated_at": "2026-07-16T09:00:00-04:00",
        "score_is_current_run": True,
        "selection": {
            "selection_date": "2026-07-16",
            "score": score,
            "reason": "test",
        },
    }


def _committed_bundle_report(*, selection_run_id: str, selection_bundle_hash: str) -> dict:
    return {
        "selection_run_id": selection_run_id,
        "selection_bundle_hash": selection_bundle_hash,
        "selection_date": "2026-07-16",
        "generated_at": "2026-07-17T23:42:21+08:00",
        "selection_stage": "FINALIZED",
        "execution_status": "COMPLETED",
        "pipeline_status": "COMPLETED",
        "result_quality": "COMPLETE",
        "research_admission": "RESEARCH_READY",
        "top_sync_status": "OK",
        "requested_top_n": 3,
        "selected_top_n": 1,
        "top3": [_sample_top()],
    }


def test_trade_notification_rejects_invalid_fill(monkeypatch):
    notifier = Notifier(console=False, macos_notification=True, webhook_url="https://example.invalid")
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("SOXS", "SELL", 0, 100.0, mode="paper")
    notifier.trade("SOXS", "SELL", 5, 0.0, pnl=-500.0, mode="paper")
    notifier.trade("SOXS", "SELL", "bad-qty", 100.0, mode="paper")
    notifier.trade("SOXS", "SELL", 5, "bad-price", mode="paper")

    assert calls == []
    assert notifier._trade_count_since_summary == 0
    assert notifier._last_trades == []


def test_trade_notification_uses_explicit_mode_label(tmp_path, monkeypatch):
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None,
                        trade_notification_state_path=tmp_path / "trade_notifications.json")
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("SOXS", "SELL", 5, 105.0, pnl=25.0, mode="live")

    assert calls
    assert "实盘卖出" in calls[0][0][0]


def test_only_trade_notifications_reach_macos(tmp_path):
    notifier = Notifier(
        console=False, macos_notification=True, webhook_url=None,
        trade_notification_state_path=tmp_path / "trade_notifications.json",
    )
    calls = []
    notifier._macos_notify = lambda title, body: calls.append((title, body))

    notifier.alert("risk check", "warning")
    notifier.signal("NVDA", "BUY", 100.0, "near support")
    notifier.summary({"total_trades": 1, "win_rate": 100, "total_pnl": 1.0, "daily_pnl_today": 1.0})
    notifier.order_submitted("NVDA", "BUY", 1, "abc123456789")
    notifier.trade("NVDA", "BUY", 1, 100.0)

    assert len(calls) == 1
    assert "NVDA" in calls[0][0]


def test_selection_market_state_prefers_preflight_over_market_context(monkeypatch):
    report = {
        "preflight": {"market_state": "AFTER_HOURS"},
        "market_context": {"session_label": "REGULAR", "current_session_status": "OPEN"},
    }

    assert _resolve_selection_market_state(report) == "AFTER_HOURS"
    import src.notifier.alerts as alerts
    monkeypatch.setattr(
        alerts,
        "_resolve_manifest_first_selection_payload",
        lambda selection_report, top_configs=None: (selection_report, [], "report"),
    )
    title, body = _build_public_channel_message(report)
    assert "Market: AFTER_HOURS" in body


def test_selection_market_state_falls_back_to_market_context(monkeypatch):
    report = {
        "market_context": {"session_label": "REGULAR", "current_session_status": "OPEN"},
    }

    assert _resolve_selection_market_state(report) == "REGULAR"
    import src.notifier.alerts as alerts
    monkeypatch.setattr(
        alerts,
        "_resolve_manifest_first_selection_payload",
        lambda selection_report, top_configs=None: (selection_report, [], "report"),
    )
    title, body = _build_public_channel_message(report)
    assert "Market: REGULAR" in body


def test_selection_market_state_unknown_when_fields_missing(monkeypatch):
    report = {}

    assert _resolve_selection_market_state(report) == "UNKNOWN"
    import src.notifier.alerts as alerts
    monkeypatch.setattr(
        alerts,
        "_resolve_manifest_first_selection_payload",
        lambda selection_report, top_configs=None: (selection_report, [], "report"),
    )
    title, body = _build_public_channel_message(report)
    assert "Market: UNKNOWN" in body


def test_trade_notification_key_prevents_duplicate_send(tmp_path, monkeypatch):
    notifier = Notifier(
        console=False,
        macos_notification=True,
        webhook_url="https://example.invalid",
        trade_notification_state_path=tmp_path / "trade_notifications.json",
    )
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade(
        "SOFI",
        "BUY",
        10,
        12.5,
        mode="paper",
        notification_key="paper:SOFI:BUY:order-1",
        fill_id="order-1",
        event_id="event-1",
    )
    notifier.trade(
        "SOFI",
        "BUY",
        10,
        12.5,
        mode="paper",
        notification_key="paper:SOFI:BUY:order-1",
        fill_id="order-1",
        event_id="event-1",
    )

    assert len(calls) == 1
    assert notifier._trade_count_since_summary == 1
    assert len(notifier._last_trades) == 1


def test_trade_fill_id_prevents_replay_across_notifier_instances(tmp_path, monkeypatch):
    state_path = tmp_path / "trade_notifications.json"
    first = Notifier(
        console=False,
        macos_notification=True,
        webhook_url="https://example.invalid",
        trade_notification_state_path=state_path,
    )
    second = Notifier(
        console=False,
        macos_notification=True,
        webhook_url="https://example.invalid",
        trade_notification_state_path=state_path,
    )
    first_calls = []
    second_calls = []
    monkeypatch.setattr(first, "_send", lambda *args, **kwargs: first_calls.append((args, kwargs)))
    monkeypatch.setattr(second, "_send", lambda *args, **kwargs: second_calls.append((args, kwargs)))

    first.trade("SOXS", "SELL", 3, 25.0, mode="paper", fill_id="fill-123")
    second.trade("SOXS", "SELL", 3, 25.0, mode="paper", fill_id="fill-123")

    assert len(first_calls) == 1
    assert second_calls == []


def test_trade_event_id_prevents_duplicate_send_without_fill_id(tmp_path, monkeypatch):
    notifier = Notifier(
        console=False,
        macos_notification=True,
        webhook_url="https://example.invalid",
        trade_notification_state_path=tmp_path / "trade_notifications.json",
    )
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("AAPL", "SELL", 2, 150.0, mode="paper", event_id="event-xyz")
    notifier.trade("AAPL", "SELL", 2, 150.0, mode="paper", event_id="event-xyz")

    assert len(calls) == 1


def test_trade_without_event_identity_is_not_deduplicated(tmp_path, monkeypatch):
    """Legacy: without any explicit identity, fallback key deduplicates within the same minute bucket."""
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None,
                        trade_notification_state_path=tmp_path / "trade_notifications.json")
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper")
    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper")

    # Fallback key now deduplicates identical trades in the same minute bucket
    assert len(calls) == 1


def test_fallback_key_differs_for_different_trades(tmp_path, monkeypatch):
    """Different quantity/price produce distinct fallback keys, so both send."""
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None,
                        trade_notification_state_path=tmp_path / "trade_notifications.json")
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper")
    notifier.trade("NVDA", "BUY", 5, 105.0, mode="paper")

    assert len(calls) == 2


def test_fallback_key_differs_for_different_tickers(tmp_path, monkeypatch):
    """Different tickers produce distinct fallback keys, so both send."""
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None,
                        trade_notification_state_path=tmp_path / "trade_notifications.json")
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("AAPL", "BUY", 1, 100.0, mode="paper")
    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper")

    assert len(calls) == 2


def test_fallback_key_differs_for_buy_vs_sell(tmp_path, monkeypatch):
    """BUY vs SELL produce distinct fallback keys, so both send."""
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None,
                        trade_notification_state_path=tmp_path / "trade_notifications.json")
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper")
    notifier.trade("NVDA", "SELL", 1, 100.0, mode="paper")

    assert len(calls) == 2


def test_explicit_notification_key_takes_priority_over_fallback(tmp_path, monkeypatch):
    """notification_key is always used verbatim — no fallback hash involved."""
    state_path = tmp_path / "trade_notifications.json"
    notifier = Notifier(
        console=False, macos_notification=True, webhook_url=None,
        trade_notification_state_path=state_path,
    )
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper",
                   notification_key="my-custom-key-001")
    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper",
                   notification_key="my-custom-key-001")

    assert len(calls) == 1


def test_fallback_key_persists_across_instances(tmp_path, monkeypatch):
    """Two Notifier instances sharing the same state file dedup via fallback keys."""
    state_path = tmp_path / "trade_notifications.json"
    first = Notifier(
        console=False, macos_notification=True, webhook_url=None,
        trade_notification_state_path=state_path,
    )
    second = Notifier(
        console=False, macos_notification=True, webhook_url=None,
        trade_notification_state_path=state_path,
    )
    first_calls = []
    second_calls = []
    monkeypatch.setattr(first, "_send", lambda *args, **kwargs: first_calls.append((args, kwargs)))
    monkeypatch.setattr(second, "_send", lambda *args, **kwargs: second_calls.append((args, kwargs)))

    first.trade("NVDA", "BUY", 1, 100.0, mode="paper")
    second.trade("NVDA", "BUY", 1, 100.0, mode="paper")

    assert len(first_calls) == 1
    assert second_calls == []


def test_telegram_send_single_redacts_token_from_exception_logs(monkeypatch, caplog):
    notifier = Notifier(
        console=False,
        macos_notification=False,
        telegram_bot_token="123456:SECRET",
        telegram_chat_id="@test_channel",
    )

    def fake_post(url, **kwargs):
        raise requests.RequestException(f"boom url={url}")

    monkeypatch.setattr(alerts.requests, "post", fake_post)

    with caplog.at_level(logging.WARNING):
        ok = notifier._telegram_send_single("hello", use_html=False, plain_title="title", plain_body="body")

    assert ok is False
    assert "123456:SECRET" not in caplog.text
    assert "***REDACTED***" in caplog.text


def test_notify_ai_selection_result_redacts_token_in_failure_logs(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH", str(tmp_path / "notification_ledger.jsonl"))
    monkeypatch.setattr(
        alerts,
        "load_committed_selection_bundle",
        lambda *_args, **_kwargs: {"report": _committed_bundle_report(selection_run_id="bundle-run-redact", selection_bundle_hash="bundle-hash-redact")},
    )
    monkeypatch.setattr(
        alerts,
        "_load_ai_selector_notification_config",
        lambda: {"ai_selector_telegram_bot_token": "123456:SECRET", "ai_selector_telegram_chat_id": "ai-chat"},
    )

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self._telegram_enabled = True
            self.webhook_url = ""

        def _send(self, *args, **kwargs):
            raise RuntimeError("telegram down: https://api.telegram.org/bot123456:SECRET/sendMessage")

    monkeypatch.setattr(alerts, "Notifier", FakeNotifier)

    with caplog.at_level(logging.WARNING):
        alerts.notify_ai_selection_result(
            _committed_bundle_report(selection_run_id="bundle-run-redact", selection_bundle_hash="bundle-hash-redact"),
            [_sample_top("SOXS")],
        )

    assert "123456:SECRET" not in caplog.text
    assert "***REDACTED***" in caplog.text


def test_telegram_send_single_preserves_successful_payload(monkeypatch):
    notifier = Notifier(
        console=False,
        macos_notification=False,
        telegram_bot_token="123456:SECRET",
        telegram_chat_id="@test_channel",
    )
    captured = {}

    class FakeResponse:
        ok = True
        status_code = 200
        text = "ok"

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(alerts.requests, "post", fake_post)

    ok = notifier._telegram_send_single("<b>hello</b>", use_html=True, plain_title="title", plain_body="body")

    assert ok is True
    assert captured["url"] == "https://api.telegram.org/bot123456:SECRET/sendMessage"
    assert captured["kwargs"]["json"]["chat_id"] == "@test_channel"
    assert captured["kwargs"]["json"]["parse_mode"] == "HTML"
    assert captured["kwargs"]["json"]["text"] == "<b>hello</b>"


def run_test_direct():
    test_only_trade_notifications_reach_macos()


if __name__ == "__main__":
    run_test_direct()
