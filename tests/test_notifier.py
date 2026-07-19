from src.notifier.alerts import Notifier


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


def test_trade_notification_uses_explicit_mode_label(monkeypatch):
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None)
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("SOXS", "SELL", 5, 105.0, pnl=25.0, mode="live")

    assert calls
    assert "实盘卖出" in calls[0][0][0]


def test_only_trade_notifications_reach_macos():
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None)
    calls = []
    notifier._macos_notify = lambda title, body: calls.append((title, body))

    notifier.alert("risk check", "warning")
    notifier.signal("NVDA", "BUY", 100.0, "near support")
    notifier.summary({"total_trades": 1, "win_rate": 100, "total_pnl": 1.0, "daily_pnl_today": 1.0})
    notifier.order_submitted("NVDA", "BUY", 1, "abc123456789")
    notifier.trade("NVDA", "BUY", 1, 100.0)

    assert len(calls) == 1
    assert "NVDA" in calls[0][0]


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


def test_trade_without_event_identity_is_not_deduplicated(monkeypatch):
    notifier = Notifier(console=False, macos_notification=True, webhook_url=None)
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper")
    notifier.trade("NVDA", "BUY", 1, 100.0, mode="paper")

    assert len(calls) == 2


def run_test_direct():
    test_only_trade_notifications_reach_macos()


if __name__ == "__main__":
    run_test_direct()
