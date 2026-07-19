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


def run_test_direct():
    test_only_trade_notifications_reach_macos()


if __name__ == "__main__":
    run_test_direct()
