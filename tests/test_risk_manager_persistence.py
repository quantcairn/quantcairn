import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.risk.manager import RiskManager, TradeRecord


def test_risk_state_survives_restart():
    path = Path(tempfile.gettempdir()) / "soxs-test-risk-state.json"
    path.unlink(missing_ok=True)
    first = RiskManager(daily_loss_limit=60.0, state_path=path)
    first.update_equity(700.0)
    first.record_trade(TradeRecord(
        entry_time=datetime.now(timezone.utc),
        exit_time=datetime.now(timezone.utc),
        entry_price=10.0,
        exit_price=9.0,
        shares=2,
        pnl=-2.0,
        pnl_pct=-10.0,
        side="LONG",
    ))

    second = RiskManager(daily_loss_limit=60.0, state_path=path)

    assert second.get_daily_pnl() == -2.0
    assert second.get_daily_trades() == 1
    assert second.get_stats()["consecutive_losses"] == 1
    path.unlink(missing_ok=True)


def test_trade_timestamp_uses_new_york_trading_date():
    beijing_time = datetime(
        2026, 7, 2, 10, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert RiskManager._datetime_trading_date(beijing_time) == "2026-07-01"


def test_new_trading_day_resets_consecutive_losses():
    """When the ET trading date changes, consecutive_losses resets to 0."""
    rm = RiskManager(max_consecutive_losses=3)

    # Simulate 3 losing trades on day 1
    rm._consecutive_losses = 3
    rm._last_trading_day = "2026-08-06"

    # New trading day starts — _check_entry should reset losses
    with patch.object(rm, "_trading_date", return_value="2026-08-07"):
        rm.check_entry(price=50.0, shares=10, current_position=0)

    assert rm._consecutive_losses == 0, (
        f"Expected consecutive_losses=0 on new trading day, got {rm._consecutive_losses}"
    )
    assert rm._last_trading_day == "2026-08-07"


def test_new_trading_day_allows_trade_that_was_blocked():
    """A trade that would have been blocked by consecutive losses is
    allowed on a new trading day."""
    rm = RiskManager(max_consecutive_losses=3)

    # Day 1: 3 losses → blocked.  Both calls must mock the date
    # so _reset_on_new_trading_day doesn't fire on the first call.
    with patch.object(rm, "_trading_date", return_value="2026-08-06"):
        rm._consecutive_losses = 3
        rm._last_trading_day = "2026-08-06"
        rm.update_equity(10000.0)
        result1 = rm.check_entry(price=50.0, shares=10, current_position=0)
    assert result1.allowed is False, f"Should be blocked on same day, got: {result1.reason}"
    assert result1.rule_triggered == "consecutive_losses"

    # Day 2: should be allowed (losses reset, halt cleared by new-day logic).
    with patch.object(rm, "_trading_date", return_value="2026-08-07"):
        result2 = rm.check_entry(price=50.0, shares=10, current_position=0)

    assert result2.allowed is True, (
        f"Should be allowed on new day, got: {result2.reason}"
    )
    assert rm._consecutive_losses == 0


def test_same_day_consecutive_losses_still_block():
    """On the same trading day, consecutive losses still trigger halt."""
    rm = RiskManager(max_consecutive_losses=2, cool_down_seconds=0)
    with patch.object(rm, "_trading_date", return_value="2026-08-07"):
        rm._last_trading_day = "2026-08-07"
        rm.update_equity(10000.0)

        # Trade 1: loss
        rm.record_trade(TradeRecord(
            entry_time=datetime(2026, 8, 7, 12, 0),
            exit_time=datetime(2026, 8, 7, 13, 0),
            entry_price=50.0, exit_price=49.0, shares=5,
            pnl=-5.0, pnl_pct=-2.0, side="LONG",
        ))
        assert rm._consecutive_losses == 1

        # Trade 2: loss
        rm.record_trade(TradeRecord(
            entry_time=datetime(2026, 8, 7, 14, 0),
            exit_time=datetime(2026, 8, 7, 15, 0),
            entry_price=50.0, exit_price=49.0, shares=5,
            pnl=-5.0, pnl_pct=-2.0, side="LONG",
        ))
        assert rm._consecutive_losses == 2

        # 3rd check: should be blocked by consecutive_losses
        result = rm.check_entry(price=50.0, shares=10, current_position=0)
    assert result.allowed is False, (
        f"Should be blocked, got: allowed={result.allowed} reason={result.reason}"
    )
    assert result.rule_triggered == "consecutive_losses"


def test_consecutive_losses_reset_on_winning_trade():
    """A winning trade resets consecutive_losses on the same day."""
    rm = RiskManager(max_consecutive_losses=3)
    rm._last_trading_day = "2026-08-07"

    # 2 losses
    rm.record_trade(TradeRecord(
        entry_time=datetime(2026, 8, 7, 12, 0),
        exit_time=datetime(2026, 8, 7, 13, 0),
        entry_price=50.0, exit_price=49.0, shares=5,
        pnl=-5.0, pnl_pct=-2.0, side="LONG",
    ))
    rm.record_trade(TradeRecord(
        entry_time=datetime(2026, 8, 7, 14, 0),
        exit_time=datetime(2026, 8, 7, 15, 0),
        entry_price=50.0, exit_price=49.0, shares=5,
        pnl=-5.0, pnl_pct=-2.0, side="LONG",
    ))
    assert rm._consecutive_losses == 2

    # 1 win → reset
    rm.record_trade(TradeRecord(
        entry_time=datetime(2026, 8, 7, 16, 0),
        exit_time=datetime(2026, 8, 7, 16, 30),
        entry_price=50.0, exit_price=51.0, shares=5,
        pnl=5.0, pnl_pct=2.0, side="LONG",
    ))
    assert rm._consecutive_losses == 0


def test_first_check_sets_last_trading_day_without_reset():
    """The first call to check_entry should set _last_trading_day
    without incorrectly resetting anything."""
    rm = RiskManager(max_consecutive_losses=3)
    assert rm._last_trading_day == ""

    # First check ever — should not crash, should set the day
    today = rm._trading_date()
    rm.update_equity(10000.0)
    result = rm.check_entry(price=50.0, shares=10, current_position=0)
    assert result.allowed is True
    assert rm._last_trading_day == today
    assert rm._consecutive_losses == 0


def test_persistence_roundtrips_last_trading_day():
    """last_trading_day survives a save→load cycle."""
    path = Path(tempfile.gettempdir()) / "soxs-test-risk-trading-day.json"
    path.unlink(missing_ok=True)

    first = RiskManager(daily_loss_limit=60.0, state_path=path)
    first._last_trading_day = "2026-08-07"
    first._consecutive_losses = 2
    first._save_state(force=True)

    second = RiskManager(daily_loss_limit=60.0, state_path=path)
    assert second._last_trading_day == "2026-08-07"
    assert second._consecutive_losses == 2
    path.unlink(missing_ok=True)


def run_test_direct():
    test_risk_state_survives_restart()
    test_trade_timestamp_uses_new_york_trading_date()
    test_new_trading_day_resets_consecutive_losses()
    test_new_trading_day_allows_trade_that_was_blocked()
    test_same_day_consecutive_losses_still_block()
    test_consecutive_losses_reset_on_winning_trade()
    test_first_check_sets_last_trading_day_without_reset()
    test_persistence_roundtrips_last_trading_day()


if __name__ == "__main__":
    run_test_direct()
