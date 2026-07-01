import tempfile
from datetime import datetime
from pathlib import Path

from src.risk.manager import RiskManager, TradeRecord


def test_risk_state_survives_restart():
    path = Path(tempfile.gettempdir()) / "soxs-test-risk-state.json"
    path.unlink(missing_ok=True)
    first = RiskManager(daily_loss_limit=60.0, state_path=path)
    first.update_equity(700.0)
    first.record_trade(TradeRecord(
        entry_time=datetime.now(),
        exit_time=datetime.now(),
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


def run_test_direct():
    test_risk_state_survives_restart()


if __name__ == "__main__":
    run_test_direct()
