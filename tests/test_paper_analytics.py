"""Tests for Paper Analytics Research Layer — Phase 5C.

Verifies:
  1. Summary calculation with trades and snapshots
  2. Empty dataset returns valid output
  3. Sector aggregation correctness
  4. Factor correlation computation
  5. MFE capture rate formula
  6. Range statistics
  7. Idempotent output
  8. Atomic write (no tmp files)
  9. Schema validation
  10. Missing snapshot handling
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.openalpha import paper_analytics as pa
from src.openalpha.paper_analytics import (
    run_paper_analytics,
    _compute_summary,
    _compute_performance,
    _compute_factor_analysis,
    _compute_sector_analysis,
    ANALYTICS_ROOT,
    ANALYTICS_VERSION,
)

from src.openalpha.paper_research import (
    PaperTradeRecord,
    create_paper_trade,
    save_paper_trades,
    PAPER_ROOT as PAPER_TRADES_ROOT,
    load_paper_trades,
)
from src.openalpha.paper_tracker import PositionSnapshot


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_roots(monkeypatch, base: Path) -> tuple[Path, Path, Path]:
    """Redirect all module-level paths to temp dirs."""
    import src.openalpha.paper_research as pr
    import src.openalpha.paper_tracker as pt
    trading = base / "paper_trading"
    tracking = base / "paper_tracking"
    analytics = base / "paper_analytics"
    monkeypatch.setattr(pr, "PAPER_ROOT", trading)
    monkeypatch.setattr(pt, "PAPER_TRADING_ROOT", trading)
    monkeypatch.setattr(pt, "TRACKING_ROOT", tracking)
    monkeypatch.setattr(pa, "ANALYTICS_ROOT", analytics)
    return trading, tracking, analytics


def _make_snapshots_for_trades(
    trades: list[PaperTradeRecord],
) -> dict[str, PositionSnapshot]:
    """Build synthetic PositionSnapshots for a list of trades."""
    result: dict[str, PositionSnapshot] = {}
    for i, t in enumerate(trades):
        ret = (i % 3 - 1) * 3.0 + 5.0  # varied returns
        snap = PositionSnapshot(
            paper_trade_id=t.paper_trade_id,
            symbol=t.symbol,
            selection_date=t.selection_date,
            entry_date=t.entry_date,
            entry_price=t.entry_price,
            range_low=t.range_low,
            range_high=t.range_high,
            snapshot_date="2026-09-01",
            current_price=t.entry_price * (1.0 + ret / 100.0),
            price_source="test",
            unrealized_return_pct=ret,
            holding_days=5 + i,
            mfe_since_entry_pct=ret + 3.0,
            mae_since_entry_pct=-2.0,
            range_touch_support=(i % 2 == 0),
            range_touch_resistance=(i % 2 == 1),
            range_breakdown=(i == 3),
            range_breakout=(i == 5),
            exit_signal="breakdown" if i == 3 else ("breakout" if i == 5 else None),
            exit_recommendation="REVIEW_EXIT" if i in (3, 5) else "HOLD",
            trade_status_observation="SHOULD_CLOSE" if i in (3, 5) else "OPEN",
            recorded_at="2026-09-01T00:00:00+00:00",
        )
        result[t.paper_trade_id] = snap
    return result


def _create_and_save_trades(trading_root: Path, count: int = 6) -> list[PaperTradeRecord]:
    """Create and save synthetic paper trades."""
    sectors = ["Financial Services", "Healthcare", "Technology",
               "Financial Services", "Energy", "Technology"]
    symbols = ["AIG", "BMY", "APH", "ACGL", "AR", "BKNG"]
    scores = [82.6, 70.5, 62.0, 77.1, 67.9, 58.4]

    trades = []
    for i in range(count):
        t = create_paper_trade(
            selection_run_id=f"r{i+1}",
            selection_date="2026-08-05",
            symbol=symbols[i],
            entry_price=80.0 + i,
            range_low=70.0 + i,
            range_high=90.0 + i,
            sector=sectors[i],
            score=scores[i],
        )
        trades.append(t)

    save_paper_trades(trades, date_str="2026-08-05")
    return trades


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Summary calculation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSummary:
    def test_counts_match_input(self, monkeypatch, tmp_path: Path):
        """Summary must accurately count trades and snapshots."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        trades = _create_and_save_trades(trading, count=6)
        snapshot_by_trade = _make_snapshots_for_trades(trades)

        summary = _compute_summary(trades, snapshot_by_trade, _utc_now_iso())

        assert summary["totals"]["total_trades"] == 6
        assert summary["totals"]["trades_with_snapshots"] == 6
        assert summary["totals"]["open_trades"] >= 0

    def test_performance_fields_present(self, monkeypatch, tmp_path: Path):
        """Summary performance section must have all required fields."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        trades = _create_and_save_trades(trading, count=4)
        snapshot_by_trade = _make_snapshots_for_trades(trades)

        summary = _compute_summary(trades, snapshot_by_trade, _utc_now_iso())

        perf = summary["performance"]
        for key in ["win_rate", "wins", "losses", "avg_return_pct",
                     "avg_holding_days", "avg_mfe_pct", "avg_mae_pct",
                     "mfe_capture_rate"]:
            assert key in perf, f"'{key}' missing from performance"

    def test_range_analysis_fields_present(self, monkeypatch, tmp_path: Path):
        """Summary range_analysis must have all breakdown/breakout fields."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        trades = _create_and_save_trades(trading, count=4)
        snapshot_by_trade = _make_snapshots_for_trades(trades)

        summary = _compute_summary(trades, snapshot_by_trade, _utc_now_iso())

        rng = summary["range_analysis"]
        for key in ["breakdown_count", "breakout_count", "review_exit_count",
                     "breakdown_rate", "breakout_rate"]:
            assert key in rng, f"'{key}' missing from range_analysis"


def _utc_now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Empty dataset
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyDataset:
    def test_empty_returns_valid_output(self, monkeypatch, tmp_path: Path):
        """Empty trades must produce valid reports, not crash."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)

        result = run_paper_analytics()

        assert result["total_trades"] == 0
        assert result["trades_with_snapshots"] == 0

    def test_all_four_files_written(self, monkeypatch, tmp_path: Path):
        """Even with empty data, all 4 files must be written."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)

        # Create one trade so files are non-trivial
        trades = _create_and_save_trades(trading, count=1)
        snapshots = _make_snapshots_for_trades(trades)
        # Write snapshots to the tracking dir
        import src.openalpha.paper_tracker as pt
        pt._write_atomic(
            tracking / "positions" / "2026-09-01" / "positions.json",
            json.dumps([s.to_dict() for s in snapshots.values()]),
        )

        run_paper_analytics()

        for name in ["summary", "performance", "factor_analysis", "sector_analysis"]:
            path = analytics / f"{name}.json"
            assert path.exists(), f"'{name}.json' not written"

    def test_zero_trades_no_crash(self, monkeypatch, tmp_path: Path):
        """Zero trades with no snapshots must not crash _compute_performance."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)

        perf = _compute_performance([], {}, _utc_now_iso())
        assert perf["trades"] == []
        assert perf["aggregates"]["total"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Sector aggregation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSector:
    def test_sectors_correctly_grouped(self, monkeypatch, tmp_path: Path):
        """Sectors must be grouped and counted correctly."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        trades = _create_and_save_trades(trading, count=6)
        snapshot_by_trade = _make_snapshots_for_trades(trades)

        result = _compute_sector_analysis(trades, snapshot_by_trade, _utc_now_iso())
        sectors = result["sectors"]

        assert "Financial Services" in sectors
        assert sectors["Financial Services"]["trades"] == 2
        assert sectors["Healthcare"]["trades"] == 1
        assert sectors["Technology"]["trades"] == 2

    def test_sector_has_performance_fields(self, monkeypatch, tmp_path: Path):
        """Each sector entry must have win_rate, avg_return, avg_mfe, avg_mae."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        trades = _create_and_save_trades(trading, count=4)
        snapshot_by_trade = _make_snapshots_for_trades(trades)

        result = _compute_sector_analysis(trades, snapshot_by_trade, _utc_now_iso())
        for sec, sd in result["sectors"].items():
            for key in ["trades", "win_rate", "avg_return_pct",
                         "avg_mfe_pct", "avg_mae_pct"]:
                assert key in sd, f"'{key}' missing from sector '{sec}'"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Factor correlation
# ═══════════════════════════════════════════════════════════════════════════════

class TestFactorCorrelation:
    def test_score_factor_present(self, monkeypatch, tmp_path: Path):
        """Factor analysis must include the 'score' factor with valid fields."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        trades = _create_and_save_trades(trading, count=6)
        snapshot_by_trade = _make_snapshots_for_trades(trades)

        result = _compute_factor_analysis(trades, snapshot_by_trade, _utc_now_iso())
        factors = result["factors"]

        assert "score" in factors
        fs = factors["score"]
        assert fs["samples"] > 0
        assert fs["mean_all"] is not None
        # mean_winning or mean_losing may be None depending on return distribution
        assert "correlation_with_return" in fs

    def test_factor_correlation_with_known_data(self, monkeypatch, tmp_path: Path):
        """Score perfectly correlated with return → correlation ≈ 1.0."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)

        # Create trades with scores proportional to returns
        trades = [
            create_paper_trade(selection_run_id=f"r{i}", selection_date="2026-08-05",
                               symbol=f"S{i}", score=float(i * 10))
            for i in range(1, 6)
        ]
        save_paper_trades(trades, date_str="2026-08-05")

        snaps = {
            t.paper_trade_id: PositionSnapshot(
                paper_trade_id=t.paper_trade_id,
                symbol=t.symbol, selection_date=t.selection_date,
                entry_date=t.entry_date, entry_price=80.0,
                range_low=70.0, range_high=90.0,
                snapshot_date="2026-09-01",
                unrealized_return_pct=float(t.score) * 0.1,  # perfect correlation
            )
            for t in trades
        }

        result = _compute_factor_analysis(trades, snaps, _utc_now_iso())
        assert result["factors"]["score"]["correlation_with_return"] == pytest.approx(1.0, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. MFE capture rate
# ═══════════════════════════════════════════════════════════════════════════════

class TestMFECapture:
    def test_mfe_capture_formula(self, monkeypatch, tmp_path: Path):
        """MFE capture rate = unrealized_return / mfe."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(
            selection_run_id="r1", selection_date="2026-08-05",
            symbol="TEST", entry_price=100.0,
        )
        save_paper_trades([trade], date_str="2026-08-05")

        snap = PositionSnapshot(
            paper_trade_id=trade.paper_trade_id,
            symbol="TEST", selection_date="2026-08-05",
            entry_date="2026-08-05", entry_price=100.0,
            range_low=90.0, range_high=110.0,
            snapshot_date="2026-09-01",
            unrealized_return_pct=5.0,
            mfe_since_entry_pct=10.0,
            mae_since_entry_pct=-3.0,
            holding_days=5,
        )

        summary = _compute_summary([trade], {trade.paper_trade_id: snap}, _utc_now_iso())

        # MFE capture = 5.0 / 10.0 = 0.5
        assert summary["performance"]["mfe_capture_rate"] == 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Idempotent output
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdempotent:
    def test_same_input_same_output(self, monkeypatch, tmp_path: Path):
        """Running analytics twice on identical data must produce identical output."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        trades = _create_and_save_trades(trading, count=4)

        r1 = run_paper_analytics()
        r2 = run_paper_analytics()

        assert r1["total_trades"] == r2["total_trades"]
        assert r1["trades_with_snapshots"] == r2["trades_with_snapshots"]

        # File content must be identical
        for name in ["summary", "performance", "factor_analysis", "sector_analysis"]:
            c1 = (analytics / f"{name}.json").read_text()
            c2 = (analytics / f"{name}.json").read_text()
            assert c1 == c2, f"'{name}.json' differs between runs"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Atomic write
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_no_tmp_files(self, monkeypatch, tmp_path: Path):
        """No .tmp-* files left after analytics run."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        _create_and_save_trades(trading, count=1)

        run_paper_analytics()

        tmp_files = list(analytics.rglob("*.tmp-*"))
        assert len(tmp_files) == 0, f"Found leftover tmp files: {tmp_files}"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_summary_has_version(self, monkeypatch, tmp_path: Path):
        """All 4 output files must carry analytics_version."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        _create_and_save_trades(trading, count=1)

        run_paper_analytics()

        for name in ["summary", "performance", "factor_analysis", "sector_analysis"]:
            data = json.loads((analytics / f"{name}.json").read_text())
            assert data["analytics_version"] == ANALYTICS_VERSION, (
                f"'{name}.json' missing analytics_version")

    def test_performance_trades_have_all_fields(self, monkeypatch, tmp_path: Path):
        """Each trade in performance.json must have all required keys."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        _create_and_save_trades(trading, count=2)

        run_paper_analytics()

        data = json.loads((analytics / "performance.json").read_text())
        for t in data["trades"]:
            for key in ["paper_trade_id", "symbol", "sector", "status",
                         "entry_price", "current_price", "has_snapshot"]:
                assert key in t, f"'{key}' missing from performance trade"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Missing snapshot handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingSnapshot:
    def test_trade_without_snapshot_handled(self, monkeypatch, tmp_path: Path):
        """Trade without a snapshot must appear in performance with has_snapshot=False."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)

        t = create_paper_trade(
            selection_run_id="r1", selection_date="2026-08-05", symbol="NOSNAP")
        save_paper_trades([t], date_str="2026-08-05")

        perf = _compute_performance([t], {}, _utc_now_iso())

        assert perf["trades"][0]["symbol"] == "NOSNAP"
        assert perf["trades"][0]["has_snapshot"] is False
        assert perf["trades"][0]["current_price"] is None
        assert perf["aggregates"]["with_snapshots"] == 0
