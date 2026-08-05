"""Tests for Paper Position Tracker — Phase 5B.

Verifies:
  1. Snapshot creation from OPEN trade
  2. Current price from OHLCV data
  3. Unrealized return calculation
  4. Path-based MFE/MAE since entry
  5. Range breakdown detection
  6. Range breakout detection
  7. No exit signal when within range
  8. No data handling (empty bars)
  9. Idempotent snapshot
  10. Daily summary aggregation
  11. Atomic write (no tmp files)
  12. Empty trades → valid empty output
  13. Trade status NOT modified (safety boundary)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.openalpha import paper_tracker as pt
from src.openalpha.paper_tracker import (
    PositionSnapshot,
    DailySummary,
    compute_snapshot,
    run_position_tracking,
    load_snapshots,
    load_daily_summaries,
    TRACKING_ROOT,
    PAPER_TRADING_ROOT,
)

from src.openalpha.paper_research import (
    create_paper_trade,
    save_paper_trades,
    load_paper_trades,
    PAPER_ROOT as PAPER_TRADES_ROOT,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_roots(monkeypatch, base: Path) -> tuple[Path, Path, Path]:
    """Redirect all module-level paths to temp dirs.

    paper_tracker.py imports load_paper_trades from paper_research.py,
    which uses its own module-level PAPER_ROOT.  So we patch the
    SOURCE module (paper_research), not paper_tracker.
    """
    import src.openalpha.paper_research as pr
    trading = base / "paper_trading"
    tracking = base / "paper_tracking"
    monkeypatch.setattr(pr, "PAPER_ROOT", trading)
    monkeypatch.setattr(pt, "PAPER_TRADING_ROOT", trading)
    monkeypatch.setattr(pt, "TRACKING_ROOT", tracking)
    return trading, tracking


def _make_mock_bars(
    entry_price: float = 80.0,
    n_bars: int = 10,
    trend: float = 0.001,
    high_extra: float = 1.0,
    low_extra: float = -1.0,
    start_date: str = "2026-08-05",
) -> list[dict[str, float]]:
    """Build synthetic OHLCV bars."""
    from datetime import date, timedelta
    s = date.fromisoformat(start_date)
    bars = []
    price = entry_price
    for i in range(n_bars):
        d = s + timedelta(days=i + 1)
        price = price * (1.0 + trend)
        bars.append({
            "date": d.isoformat(),
            "open": round(price * 0.998, 2),
            "high": round(price + high_extra, 2),
            "low": round(price + low_extra, 2),
            "close": round(price, 2),
            "volume": 1_000_000.0 + i * 10000,
        })
    return bars


def _create_and_save_trade(paper_root: Path, symbol: str = "AIG",
                           entry_price: float = 80.0,
                           range_low: float = 70.0,
                           range_high: float = 90.0) -> str:
    """Create and save a paper trade, return its paper_trade_id."""
    trade = create_paper_trade(
        selection_run_id="r1",
        selection_date="2026-08-05",
        symbol=symbol,
        entry_price=entry_price,
        range_low=range_low,
        range_high=range_high,
    )
    save_paper_trades([trade], date_str="2026-08-05")
    return trade.paper_trade_id


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Snapshot creation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSnapshotCreation:
    def test_snapshot_from_open_trade(self, monkeypatch, tmp_path: Path):
        """compute_snapshot must produce a valid PositionSnapshot with identity fields."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        trade_id = _create_and_save_trade(trading)
        trades = load_paper_trades(status="OPEN")
        assert len(trades) == 1

        # Mock the OHLCV fetch
        mock_bars = _make_mock_bars(entry_price=80.0, n_bars=10)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (mock_bars, "test"))

        snap = compute_snapshot(trades[0])

        assert snap.paper_trade_id == trade_id
        assert snap.symbol == "AIG"
        assert snap.entry_price == 80.0
        assert snap.range_low == 70.0
        assert snap.range_high == 90.0
        assert snap.snapshot_date != ""
        assert snap.paper_tracker_version == pt.PAPER_TRACKER_VERSION

    def test_snapshot_has_research_observations(self, monkeypatch, tmp_path: Path):
        """PositionSnapshot must include exit_signal, exit_recommendation,
        and trade_status_observation — observations only."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading)
        trades = load_paper_trades(status="OPEN")

        mock_bars = _make_mock_bars(entry_price=80.0, n_bars=10)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (mock_bars, "test"))

        snap = compute_snapshot(trades[0])

        # Within range → no exit signal
        assert snap.exit_signal is None
        assert snap.exit_recommendation == "HOLD"
        assert snap.trade_status_observation == "OPEN"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Current price
# ═══════════════════════════════════════════════════════════════════════════════

class TestCurrentPrice:
    def test_current_price_is_last_close(self, monkeypatch, tmp_path: Path):
        """current_price must be the last bar's close."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading)
        trades = load_paper_trades(status="OPEN")

        bars = _make_mock_bars(entry_price=80.0, n_bars=5)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])
        assert snap.current_price == bars[-1]["close"]
        assert snap.price_source == "test"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Unrealized return
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnrealizedReturn:
    def test_return_formula(self, monkeypatch, tmp_path: Path):
        """unrealized_return_pct = (current_price - entry_price) / entry_price * 100."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading, entry_price=100.0)
        trades = load_paper_trades(status="OPEN")

        # Last close = 110 → return = +10%
        bars = _make_mock_bars(entry_price=110.0, n_bars=3)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])
        assert snap.unrealized_return_pct is not None
        assert snap.unrealized_return_pct == pytest.approx(10.0, abs=0.5)

    def test_negative_return(self, monkeypatch, tmp_path: Path):
        """Negative return must be computed correctly."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading, entry_price=100.0)
        trades = load_paper_trades(status="OPEN")

        bars = _make_mock_bars(entry_price=90.0, n_bars=3)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])
        assert snap.unrealized_return_pct is not None
        assert snap.unrealized_return_pct < 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. MFE/MAE since entry
# ═══════════════════════════════════════════════════════════════════════════════

class TestMFEMAE:
    def test_mfe_uses_max_high(self, monkeypatch, tmp_path: Path):
        """MFE must use the highest high across all bars since entry."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading, entry_price=80.0)
        trades = load_paper_trades(status="OPEN")

        bars = _make_mock_bars(entry_price=80.0, n_bars=5, trend=0.01, high_extra=5.0)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])

        max_high = max(b["high"] for b in bars)
        expected_mfe = round((max_high - 80.0) / 80.0 * 100.0, 4)
        assert snap.mfe_since_entry_pct is not None
        assert snap.mfe_since_entry_pct >= 0
        assert snap.mfe_since_entry_pct == expected_mfe

    def test_mae_uses_min_low(self, monkeypatch, tmp_path: Path):
        """MAE must use the lowest low across all bars since entry."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading, entry_price=80.0)
        trades = load_paper_trades(status="OPEN")

        bars = _make_mock_bars(entry_price=80.0, n_bars=5, trend=-0.01, low_extra=-5.0)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])

        min_low = min(b["low"] for b in bars)
        expected_mae = round((min_low - 80.0) / 80.0 * 100.0, 4)
        assert snap.mae_since_entry_pct is not None
        assert snap.mae_since_entry_pct <= 0
        assert snap.mae_since_entry_pct == expected_mae


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Range breakdown
# ═══════════════════════════════════════════════════════════════════════════════

class TestRangeBreakdown:
    def test_close_below_range_low_detected(self, monkeypatch, tmp_path: Path):
        """close < range_low → range_breakdown + exit_signal."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading, entry_price=60.0, range_low=50.0, range_high=80.0)
        trades = load_paper_trades(status="OPEN")

        # Bars closing at ~45 — below range_low of 50
        bars = _make_mock_bars(entry_price=45.0, n_bars=5, trend=-0.01,
                               high_extra=1.0, low_extra=-2.0)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])
        assert snap.range_breakdown is True
        assert snap.exit_signal == "breakdown"
        assert snap.exit_recommendation == "REVIEW_EXIT"
        assert snap.trade_status_observation == "SHOULD_CLOSE"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Range breakout
# ═══════════════════════════════════════════════════════════════════════════════

class TestRangeBreakout:
    def test_close_above_range_high_detected(self, monkeypatch, tmp_path: Path):
        """close > range_high → range_breakout + exit_signal."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading, entry_price=80.0, range_low=60.0, range_high=90.0)
        trades = load_paper_trades(status="OPEN")

        # Bars closing at ~95 — above range_high of 90
        bars = _make_mock_bars(entry_price=95.0, n_bars=5, trend=0.01,
                               high_extra=2.0, low_extra=-1.0)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])
        assert snap.range_breakout is True
        assert snap.exit_signal == "breakout"
        assert snap.exit_recommendation == "REVIEW_EXIT"
        assert snap.trade_status_observation == "SHOULD_CLOSE"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. No exit signal within range
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoExitSignal:
    def test_within_range_no_exit(self, monkeypatch, tmp_path: Path):
        """Price staying within range → no exit_signal."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading, entry_price=80.0, range_low=70.0, range_high=90.0)
        trades = load_paper_trades(status="OPEN")

        bars = _make_mock_bars(entry_price=80.0, n_bars=10, trend=0.0)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap = compute_snapshot(trades[0])
        assert snap.exit_signal is None
        assert snap.exit_recommendation == "HOLD"
        assert snap.trade_status_observation == "OPEN"
        assert snap.range_breakdown is False
        assert snap.range_breakout is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. No data handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoDataHandling:
    def test_empty_bars_handled(self, monkeypatch, tmp_path: Path):
        """Empty OHLCV response → snapshot with no crash, price_source='none'."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading)
        trades = load_paper_trades(status="OPEN")

        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: ([], "none"))

        snap = compute_snapshot(trades[0])
        assert snap.price_source in ("none", "")
        assert snap.current_price is None
        assert snap.exit_recommendation == "HOLD"
        # Must not crash on missing data


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Idempotent snapshot
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    def test_same_input_produces_same_snapshot(self, monkeypatch, tmp_path: Path):
        """Computing twice with same data → identical results (except recorded_at)."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading)
        trades = load_paper_trades(status="OPEN")

        bars = _make_mock_bars(entry_price=80.0, n_bars=5)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        snap1 = compute_snapshot(trades[0])
        snap2 = compute_snapshot(trades[0])

        assert snap1.current_price == snap2.current_price
        assert snap1.mfe_since_entry_pct == snap2.mfe_since_entry_pct
        assert snap1.mae_since_entry_pct == snap2.mae_since_entry_pct
        assert snap1.exit_signal == snap2.exit_signal
        assert snap1.exit_recommendation == snap2.exit_recommendation

    def test_rerun_skips_existing_snapshots(self, monkeypatch, tmp_path: Path):
        """Running tracking twice → second run skips existing snapshots."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading)
        bars = _make_mock_bars(entry_price=80.0, n_bars=5)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        r1 = run_position_tracking()
        r2 = run_position_tracking()

        assert r1["snapshots"] >= 1
        assert r2["snapshots"] == 0  # all skipped
        assert r2["skipped"] >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Daily summary aggregation
# ═══════════════════════════════════════════════════════════════════════════════

class TestDailySummary:
    def test_summary_has_required_fields(self, monkeypatch, tmp_path: Path):
        """DailySummary must contain all aggregate fields."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading)
        bars = _make_mock_bars(entry_price=80.0, n_bars=5)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        run_position_tracking()

        summaries = load_daily_summaries()
        assert len(summaries) >= 1

        s = summaries[0]
        assert s.total_open_trades >= 1
        assert s.breakdown_count is not None
        assert s.breakout_count is not None
        assert s.review_exit_count is not None
        assert s.sector_breakdown is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Atomic write
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_no_tmp_files(self, monkeypatch, tmp_path: Path):
        """No .tmp-* files left behind after tracking run."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        _create_and_save_trade(trading)
        bars = _make_mock_bars(entry_price=80.0, n_bars=5)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        run_position_tracking()

        tmp_files = list(tracking.rglob("*.tmp-*"))
        assert len(tmp_files) == 0, f"Found leftover tmp files: {tmp_files}"


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Empty trades
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyTrades:
    def test_no_open_trades_returns_empty(self, monkeypatch, tmp_path: Path):
        """No OPEN trades → valid empty output."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        result = run_position_tracking()
        assert result["total_open_trades"] == 0
        assert result["snapshots"] == 0

    def test_snapshots_load_empty(self, monkeypatch, tmp_path: Path):
        """load_snapshots with no data → empty list."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        snaps = load_snapshots()
        assert snaps == []


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Trade status NOT modified (safety boundary)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradeStatusNotModified:
    def test_trade_stays_open_after_tracking(self, monkeypatch, tmp_path: Path):
        """Running tracking must NOT change the OPEN status of Phase 5A trades."""
        trading, tracking = _redirect_roots(monkeypatch, tmp_path)

        trade_id = _create_and_save_trade(trading)

        # Bars that trigger breakdown
        bars = _make_mock_bars(entry_price=40.0, n_bars=5, trend=-0.02,
                               high_extra=1.0, low_extra=-3.0)
        monkeypatch.setattr(pt, "_fetch_position_bars",
                            lambda sym, from_date: (bars, "test"))

        run_position_tracking()

        # Verify trade is STILL OPEN
        trades = load_paper_trades(status="OPEN")
        assert len(trades) == 1
        assert trades[0].paper_trade_id == trade_id
        assert trades[0].status == "OPEN"

        # Snapshot should have the breakdown OBSERVATION
        snaps = load_snapshots()
        breakdown_snaps = [s for s in snaps if s.paper_trade_id == trade_id]
        if breakdown_snaps:
            assert breakdown_snaps[0].exit_signal == "breakdown"
            # But the TRADE status is unchanged
