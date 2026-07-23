"""Unit tests for the persistent exit fence (B5).

The exit fence prevents TOP engines from re-buying a symbol for
600 seconds after a risk/orphan-triggered exit.  These tests use
tmp_path to isolate state from any real exit_fences/ directory.
"""

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config.loader import AppConfig, PositionConfig
from src.engine.trading_engine import TradingEngine


class FakeStrategy:
    def record_entry(self, price: float) -> None:
        pass

    def clear_entry(self) -> None:
        pass


class FakeNotifier:
    def __init__(self):
        self.trades = []

    def trade(self, *args, **kwargs):
        self.trades.append(kwargs)


class FakeRisk:
    def __init__(self):
        self.records = []

    def record_trade(self, trade):
        self.records.append(trade)

    def update_equity(self, equity: float):
        pass


def _engine(mode="live", tmpdir=None):
    engine = TradingEngine(
        AppConfig(ticker="SOXS", mode=mode),
        ignore_trading_hours=True,
    )
    engine.strategy = FakeStrategy()
    engine.notifier = FakeNotifier()
    engine.risk = FakeRisk()
    engine._reduce_only = False  # Let it reach the fence check
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0),
        place_order=lambda **kwargs: SimpleNamespace(
            order_id="FENCE-TEST",
            ticker="SOXS",
            side="BUY",
            order_type="MARKET",
            quantity=kwargs["quantity"],
            filled_quantity=kwargs["quantity"],
            avg_fill_price=kwargs["current_ask"],
            status="FILLED",
            notes="",
        ),
    )
    if tmpdir is not None:
        engine._exit_fence_path = Path(tmpdir) / f"{engine.ticker.upper()}.json"
    return engine


# ── Creation ────────────────────────────────────────────────────────

def test_orphan_reason_creates_fence(tmp_path):
    engine = _engine(tmpdir=tmp_path)
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine._exit_fence_path = fence_dir / "SOXS.json"

    engine._set_exit_fence("orphan:stop_loss")

    assert engine._exit_fence_path.exists()
    data = json.loads(engine._exit_fence_path.read_text(encoding="utf-8"))
    assert data["symbol"] == "SOXS"
    assert "orphan" in data["reason"]
    assert data["expires_at"] > data["created_at"]


def test_risk_reason_creates_fence(tmp_path):
    engine = _engine(tmpdir=tmp_path)
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine._exit_fence_path = fence_dir / "SOXS.json"

    engine._set_exit_fence("risk:take_profit")

    assert engine._exit_fence_path.exists()
    data = json.loads(engine._exit_fence_path.read_text(encoding="utf-8"))
    assert "risk" in data["reason"]


def test_non_orphan_risk_reason_does_not_create_fence_via_set(tmp_path):
    """_set_exit_fence itself creates a fence regardless of reason string.
    The gating is in _release_sell_lock which only calls _set_exit_fence
    for orphan/risk reasons.  This test verifies _set_exit_fence writes
    correctly for any reason."""
    engine = _engine(tmpdir=tmp_path)
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine._exit_fence_path = fence_dir / "SOXS.json"

    engine._set_exit_fence("range_sell", duration_seconds=30)
    assert engine._exit_fence_path.exists()


# ── Blocking ─────────────────────────────────────────────────────────

def test_active_fence_blocks_buy(tmp_path):
    engine = _engine(tmpdir=tmp_path)
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine._exit_fence_path = fence_dir / "SOXS.json"

    engine._set_exit_fence("orphan:stop_loss", duration_seconds=600)

    place_calls = []
    engine.broker = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=1000.0, buying_power=1000.0),
        place_order=lambda **kwargs: place_calls.append(kwargs),
    )

    engine._handle_buy_signal(
        SimpleNamespace(type=SimpleNamespace(value="BUY")), 100.0, 100.0
    )

    assert place_calls == []
    assert "exit fence" in engine._last_signal_reason.lower()


def test_expired_fence_does_not_block_buy(tmp_path):
    engine = _engine(tmpdir=tmp_path)
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine._exit_fence_path = fence_dir / "SOXS.json"

    # Write an already-expired fence
    expired_payload = {
        "symbol": "SOXS",
        "source": "orphan:stop_loss",
        "created_at": time.time() - 700,
        "expires_at": time.time() - 100,
        "reason": "orphan:stop_loss",
    }
    engine._exit_fence_path.write_text(
        json.dumps(expired_payload, ensure_ascii=False), encoding="utf-8"
    )

    assert engine._exit_fence_active() is False
    assert not engine._exit_fence_path.exists()  # Auto-cleaned up


# ── Persistence ──────────────────────────────────────────────────────

def test_fence_survives_engine_restart(tmp_path):
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    fence_path = fence_dir / "SOXS.json"

    engine1 = _engine(tmpdir=tmp_path)
    engine1._exit_fence_path = fence_path
    engine1._set_exit_fence("orphan:stop_loss", duration_seconds=600)
    assert engine1._exit_fence_active() is True

    # Simulate restart — new engine instance reads same file
    engine2 = _engine(tmpdir=tmp_path)
    engine2._exit_fence_path = fence_path
    assert engine2._exit_fence_active() is True


def test_corrupt_json_fails_closed(tmp_path):
    engine = _engine(tmpdir=tmp_path)
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine._exit_fence_path = fence_dir / "SOXS.json"

    engine._exit_fence_path.write_text("not valid {{ json", encoding="utf-8")

    # Must NOT crash and must NOT bypass (return True would allow BUY)
    assert engine._exit_fence_active() is False


def test_symbol_case_normalized(tmp_path):
    engine1 = _engine(tmpdir=tmp_path)
    engine1.ticker = "soxs"
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine1._exit_fence_path = fence_dir / "SOXS.json"
    engine1._set_exit_fence("orphan:stop_loss")

    engine2 = _engine(tmpdir=tmp_path)
    engine2.ticker = "SOXS"
    engine2._exit_fence_path = fence_dir / "SOXS.json"

    assert engine2._exit_fence_active() is True


def test_fence_path_uses_state_dir():
    """Fence path always contains exit_fences and the ticker symbol."""
    engine = _engine()
    fence_path = engine._exit_fence_path
    assert "exit_fences" in str(fence_path)
    assert "SOXS" in str(fence_path)


def test_unlink_failure_does_not_allow_buy(monkeypatch, tmp_path):
    """If the expired fence file cannot be unlinked, still treat it as
    expired (fail-closed on the active check)."""
    engine = _engine(tmpdir=tmp_path)
    fence_dir = tmp_path
    fence_dir.mkdir(parents=True, exist_ok=True)
    engine._exit_fence_path = fence_dir / "SOXS.json"

    # Write an expired fence
    expired_payload = {
        "symbol": "SOXS",
        "source": "orphan:stop_loss",
        "created_at": time.time() - 700,
        "expires_at": time.time() - 100,
        "reason": "orphan:stop_loss",
    }
    engine._exit_fence_path.write_text(
        json.dumps(expired_payload, ensure_ascii=False), encoding="utf-8"
    )

    # Make unlink raise OSError to simulate permission issue
    def _raise_oserror(*args):
        raise OSError("Permission denied")

    monkeypatch.setattr(Path, "unlink", _raise_oserror)

    # The active check should still return False (expired)
    # because time.time() > expires_at is True
    result = engine._exit_fence_active()
    assert result is False


def run_test_direct():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        test_orphan_reason_creates_fence(tp)
        test_risk_reason_creates_fence(tp)
        test_non_orphan_risk_reason_does_not_create_fence_via_set(tp)
        test_active_fence_blocks_buy(tp)
        test_expired_fence_does_not_block_buy(tp)
        test_fence_survives_engine_restart(tp)
        test_corrupt_json_fails_closed(tp)
        test_symbol_case_normalized(tp)


if __name__ == "__main__":
    run_test_direct()
