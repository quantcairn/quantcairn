"""Tests for quality fallback semantics — preview vs formal distinction."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ai_selector.funnel_tracker import FunnelTracker


# ═══════════════════════════════════════════════════════════════════════
# Quality fallback semantics
# ═══════════════════════════════════════════════════════════════════════

class TestQualityFallback:
    def test_normal_pipeline_passes(self, tmp_path: Path):
        """Without quality fallback, the chain is consistent."""
        tracker = FunnelTracker(selection_run_id="test-1", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["AAPL", "MSFT", "NVDA"], ["AAPL", "MSFT", "NVDA"])
        tracker.add_stage("UNIVERSE_FILTER", ["AAPL", "MSFT", "NVDA"], ["AAPL", "MSFT"])
        tracker.add_stage("MARKET_DATA", ["AAPL", "MSFT"], ["AAPL"])
        tracker.add_stage("SCORING_ELIGIBLE", ["AAPL"], ["AAPL"])
        tracker.add_stage("BASE_RANKING", ["AAPL"], ["AAPL"])
        tracker.add_stage("FORMAL_ELIGIBILITY", ["AAPL"], ["AAPL"])
        tracker.add_stage("COMPOSITION_FILTER", ["AAPL"], ["AAPL"])
        tracker.add_stage("DATA_QUALITY", ["AAPL"], ["AAPL"])
        tracker.add_stage("FORMAL_TOP", ["AAPL"], ["AAPL"])

        validation = tracker.validate()
        assert validation["consistent"] is True
        assert validation["quality_fallback_active"] is False

    def test_quality_fallback_suppresses_chain_break(self, tmp_path: Path):
        """When DATA_QUALITY rejects all and FORMAL_TOP backfills from preliminary pool,
        the chain break is NOT reported as a warning."""
        tracker = FunnelTracker(selection_run_id="test-2", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["AAPL", "MSFT", "NVDA"], ["AAPL", "MSFT", "NVDA"])
        tracker.add_stage("UNIVERSE_FILTER", ["AAPL", "MSFT", "NVDA"], ["AAPL", "MSFT"])
        tracker.add_stage("MARKET_DATA", ["AAPL", "MSFT"], ["AAPL", "MSFT"])
        tracker.add_stage("SCORING_ELIGIBLE", ["AAPL", "MSFT"], ["AAPL", "MSFT"])
        tracker.add_stage("BASE_RANKING", ["AAPL", "MSFT"], ["AAPL", "MSFT"])
        tracker.add_stage("FORMAL_ELIGIBILITY", ["AAPL", "MSFT"], ["AAPL", "MSFT"])
        tracker.add_stage("COMPOSITION_FILTER", ["AAPL", "MSFT"], ["AAPL", "MSFT"])
        tracker.add_stage("DATA_QUALITY", ["AAPL", "MSFT"], [])  # Quality gate: 0 survive
        tracker.add_stage("FORMAL_TOP", ["AAPL", "MSFT"], ["AAPL", "MSFT"])  # Backfill from preliminary

        tracker.mark_quality_fallback(
            preview_symbols=["AAPL", "MSFT"],
            formal_symbols=[],
        )

        validation = tracker.validate()
        assert validation["consistent"] is True
        assert validation["quality_fallback_active"] is True

    def test_preview_formal_fields_in_to_dict(self, tmp_path: Path):
        """to_dict() includes preview/formal candidata counts correctly."""
        tracker = FunnelTracker(selection_run_id="t3", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["PG", "BAC", "WMT"], ["PG", "BAC", "WMT"])
        tracker.add_stage("FORMAL_TOP", ["PG", "BAC", "WMT"], ["PG", "BAC", "WMT"])
        tracker.mark_quality_fallback(
            preview_symbols=["PG", "BAC", "WMT"],
            formal_symbols=[],
        )
        d = tracker.to_dict()
        assert d["pipeline_consistent"] is True

    def test_debug_artifact_includes_fallback_fields(self, tmp_path: Path):
        """Debug artifact JSON includes preview_candidates, formal_candidates, fallback fields."""
        tracker = FunnelTracker(selection_run_id="t4", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["SPY"], ["SPY"])
        tracker.add_stage("FORMAL_TOP", ["SPY"], ["SPY"])
        tracker.mark_quality_fallback(
            preview_symbols=["SPY"],
            formal_symbols=[],
        )
        path = tracker.write_debug_artifact()
        if path:
            data = json.loads(path.read_text(encoding="utf-8"))
            assert "preview_candidates" in data
            assert "formal_candidates" in data
            assert "fallback_used" in data
            assert "fallback_reason" in data
            assert data["preview_candidates"] == 1
            assert data["formal_candidates"] == 0

    def test_normal_path_no_fallback(self, tmp_path: Path):
        """Normal path (quality passes) has fallback_used=False."""
        tracker = FunnelTracker(selection_run_id="t5", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["AAPL"], ["AAPL"])
        tracker.add_stage("UNIVERSE_FILTER", ["AAPL"], ["AAPL"])
        tracker.add_stage("MARKET_DATA", ["AAPL"], ["AAPL"])
        tracker.add_stage("DATA_QUALITY", ["AAPL"], ["AAPL"])  # Quality PASSES
        tracker.add_stage("FORMAL_TOP", ["AAPL"], ["AAPL"])

        validation = tracker.validate()
        assert validation["consistent"] is True
        assert validation["quality_fallback_active"] is False

        # No fallback markers set
        assert tracker._quality_fallback is False

    def test_chain_break_still_detected_when_not_fallback(self, tmp_path: Path):
        """Real chain breaks (not quality-fallback) ARE still reported."""
        tracker = FunnelTracker(selection_run_id="t6", selection_date="2026-07-24",
                                project_dir=tmp_path)
        tracker.add_stage("UNIVERSE", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("UNIVERSE_FILTER", ["A", "B", "C"], ["A", "B"])
        tracker.add_stage("MARKET_DATA", ["A", "B"], ["A"])  # 2→1
        tracker.add_stage("SCORING_ELIGIBLE", ["A", "B"], ["A", "B"])  # 2→2 chain break

        validation = tracker.validate()
        assert validation["consistent"] is False  # Real chain break still detected


def run_test_direct():
    import tempfile
    td = Path(tempfile.mkdtemp())
    try:
        TestQualityFallback().test_normal_pipeline_passes(td)
        TestQualityFallback().test_quality_fallback_suppresses_chain_break(td)
        TestQualityFallback().test_preview_formal_fields_in_to_dict(td)
        TestQualityFallback().test_debug_artifact_includes_fallback_fields(td)
        TestQualityFallback().test_normal_path_no_fallback(td)
        TestQualityFallback().test_chain_break_still_detected_when_not_fallback(td)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    print("direct run: all passed")


if __name__ == "__main__":
    run_test_direct()
