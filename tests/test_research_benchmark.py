"""Tests for Research Benchmark Framework — Phase 6A.

Verifies:
  1. All sources available → full composite score
  2. All sources missing → INSUFFICIENT_DATA
  3. Partial sources (2 of 4)
  4. Selection only (single source)
  5. Historical snapshots accumulate
  6. Corrupt JSON handling
  7. Coverage score calculation
  8. Grade boundaries
  9. Deterministic output
  10. Atomic write
  11. Schema validation
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.openalpha import research_benchmark as rb
from src.openalpha.research_benchmark import (
    BenchmarkSnapshot,
    run_benchmark,
    load_benchmark,
    BENCHMARK_ROOT,
    LEARNING_ROOT,
    _compute_composite,
    _grade,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_roots(monkeypatch, base: Path) -> Path:
    """Redirect LEARNING_ROOT and BENCHMARK_ROOT to temp dirs."""
    learning = base / "learning"
    benchmark = learning / "research_benchmark"
    monkeypatch.setattr(rb, "LEARNING_ROOT", learning)
    monkeypatch.setattr(rb, "BENCHMARK_ROOT", benchmark)
    return learning


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_selection_analytics(**overrides) -> dict:
    rec = {
        "analytics_version": "research_analytics.v1",
        "total_selections": 120,
        "performance": {
            "win_rate": 0.62,
            "avg_return_21d_pct": 4.5,
            "range_success_rate": 0.44,
        },
    }
    rec.update(overrides)
    return rec


def _make_walk_forward() -> dict:
    return {
        "overall_stability": 0.85,
        "total_periods": 3,
        "forward_win_rate_range": {"min": 0.52, "max": 0.68, "mean": 0.60},
    }


def _make_paper_analytics() -> dict:
    return {
        "totals": {"total_trades": 25},
        "performance": {
            "win_rate": 0.60,
            "avg_return_pct": 3.5,
            "avg_holding_days": 8.3,
            "mfe_capture_rate": 0.48,
        },
        "range_analysis": {
            "breakdown_rate": 0.12,
            "breakout_rate": 0.16,
        },
    }


def _make_registry() -> dict:
    return {
        "total_dataset_rows": 80,
        "missing_outcome_ratio": 0.25,
        "unique_sectors": 6,
        "regimes_represented": ["bull", "sideways", "bear"],
        "ml_readiness_status": "BORDERLINE",
    }


def _write_all_sources(learning_dir: Path) -> None:
    """Write all four data sources with realistic data."""
    _write_json(learning_dir / "analytics" / "performance_summary.json",
                _make_selection_analytics())
    _write_json(learning_dir / "walk_forward" / "walk_forward_summary.json",
                _make_walk_forward())
    _write_json(learning_dir / "paper_analytics" / "summary.json",
                _make_paper_analytics())
    _write_json(learning_dir / "research_history" / "research_quality_report.json",
                _make_registry())


# ═══════════════════════════════════════════════════════════════════════════════
# 1. All sources available
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllSources:
    def test_full_composite_score(self, monkeypatch, tmp_path: Path):
        """All 4 sources → coverage=1.0, composite computed, grade assigned."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        result = run_benchmark()

        assert result["coverage_score"] == 1.0
        assert result["selection_analytics_available"] is True
        assert result["walk_forward_available"] is True
        assert result["paper_analytics_available"] is True
        assert result["registry_available"] is True
        assert result["composite_score"] is not None
        assert result["composite_grade"] != "INSUFFICIENT_DATA"

    def test_all_metrics_present(self, monkeypatch, tmp_path: Path):
        """Benchmark must contain all metric groups."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        result = run_benchmark()

        assert result["selection_win_rate"] == 0.62
        assert result["stability_score"] == 0.85
        assert result["paper_win_rate"] == 0.60
        assert result["dataset_total_rows"] == 80


# ═══════════════════════════════════════════════════════════════════════════════
# 2. All sources missing
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllMissing:
    def test_no_sources_insufficient(self, monkeypatch, tmp_path: Path):
        """Zero sources → coverage=0, grade=INSUFFICIENT_DATA."""
        learning = _redirect_roots(monkeypatch, tmp_path)

        result = run_benchmark()

        assert result["coverage_score"] == 0.0
        assert result["composite_grade"] == "INSUFFICIENT_DATA"
        assert result["selection_analytics_available"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Partial sources
# ═══════════════════════════════════════════════════════════════════════════════

class TestPartialSources:
    def test_two_sources_computes_composite(self, monkeypatch, tmp_path: Path):
        """2 of 4 sources → coverage=0.5, composite computed."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_json(learning / "analytics" / "performance_summary.json",
                    _make_selection_analytics(win_rate=0.70))
        _write_json(learning / "paper_analytics" / "summary.json",
                    _make_paper_analytics())

        result = run_benchmark()

        assert result["coverage_score"] == 0.5
        assert result["composite_score"] is not None
        # selection(0.70*0.35) + paper(0.60*0.25) / (0.35+0.25) = 0.395/0.60 ≈ 0.658
        # selection(0.70*0.35) + paper(0.60*0.25) / (0.35+0.25) = 0.395/0.60 = 0.612
        assert result["composite_score"] == pytest.approx(0.612, abs=0.02)

    def test_three_sources(self, monkeypatch, tmp_path: Path):
        """3 of 4 sources → coverage=0.75."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_json(learning / "analytics" / "performance_summary.json",
                    _make_selection_analytics())
        _write_json(learning / "walk_forward" / "walk_forward_summary.json",
                    _make_walk_forward())
        _write_json(learning / "paper_analytics" / "summary.json",
                    _make_paper_analytics())

        result = run_benchmark()
        assert result["coverage_score"] == 0.75
        assert result["composite_score"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Selection only
# ═══════════════════════════════════════════════════════════════════════════════

class TestSelectionOnly:
    def test_single_source_insufficient(self, monkeypatch, tmp_path: Path):
        """1 source only → composite=INSUFFICIENT_DATA."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_json(learning / "analytics" / "performance_summary.json",
                    _make_selection_analytics())

        result = run_benchmark()
        assert result["composite_grade"] == "INSUFFICIENT_DATA"
        assert result["selection_analytics_available"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Historical snapshots accumulate
# ═══════════════════════════════════════════════════════════════════════════════

class TestHistoricalSnapshots:
    def test_two_runs_produce_two_snapshots(self, monkeypatch, tmp_path: Path):
        """Running benchmark writes a snapshot file."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        run_benchmark()
        snapshots_dir = rb.BENCHMARK_ROOT / "snapshots"
        files = list(snapshots_dir.glob("benchmark_*.json"))
        assert len(files) >= 1

    def test_summary_history_accumulates(self, monkeypatch, tmp_path: Path):
        """benchmark_summary.json must contain both latest_snapshot and history."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        run_benchmark()

        summary = load_benchmark()
        assert summary is not None
        assert "latest_snapshot" in summary
        assert "history" in summary
        history = summary.get("history") or []
        assert len(history) >= 1
        assert history[0]["benchmark_date"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Corrupt JSON handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestCorruptJSON:
    def test_corrupt_one_source_others_ok(self, monkeypatch, tmp_path: Path):
        """Corrupt analytics JSON → that source unavailable, others OK."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        # Corrupt selection analytics
        analytics_dir = learning / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        (analytics_dir / "performance_summary.json").write_text("not json {{{")

        # Other sources valid
        _write_json(learning / "walk_forward" / "walk_forward_summary.json",
                    _make_walk_forward())
        _write_json(learning / "paper_analytics" / "summary.json",
                    _make_paper_analytics())
        _write_json(learning / "research_history" / "research_quality_report.json",
                    _make_registry())

        result = run_benchmark()
        assert result["selection_analytics_available"] is False
        assert result["walk_forward_available"] is True
        assert result["coverage_score"] == 0.75
        assert result["composite_grade"] != "INSUFFICIENT_DATA"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Coverage calculation
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoverage:
    def test_three_of_four_is_75(self, monkeypatch, tmp_path: Path):
        """3/4 sources → coverage=0.75."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_json(learning / "analytics" / "performance_summary.json",
                    _make_selection_analytics())
        _write_json(learning / "walk_forward" / "walk_forward_summary.json",
                    _make_walk_forward())
        _write_json(learning / "research_history" / "research_quality_report.json",
                    _make_registry())

        result = run_benchmark()
        assert result["coverage_score"] == 0.75


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Grade boundaries
# ═══════════════════════════════════════════════════════════════════════════════

class TestGradeBoundaries:
    def test_grade_a(self):
        snap = BenchmarkSnapshot(
            selection_analytics_available=True, walk_forward_available=True,
            selection_win_rate=0.80, stability_score=0.80,
        )
        score = _compute_composite(snap)
        assert score >= 0.70
        assert _grade(score) == "A"

    def test_grade_b(self):
        snap = BenchmarkSnapshot(
            selection_analytics_available=True, walk_forward_available=True,
            selection_win_rate=0.62, stability_score=0.60,
        )
        score = _compute_composite(snap)
        assert 0.55 <= score < 0.70
        assert _grade(score) == "B"

    def test_grade_c(self):
        snap = BenchmarkSnapshot(
            selection_analytics_available=True, walk_forward_available=True,
            selection_win_rate=0.48, stability_score=0.45,
        )
        score = _compute_composite(snap)
        assert 0.40 <= score < 0.55
        assert _grade(score) == "C"

    def test_grade_d(self):
        snap = BenchmarkSnapshot(
            selection_analytics_available=True, walk_forward_available=True,
            selection_win_rate=0.32, stability_score=0.30,
        )
        score = _compute_composite(snap)
        assert 0.25 <= score < 0.40
        assert _grade(score) == "D"

    def test_grade_f(self):
        snap = BenchmarkSnapshot(
            selection_analytics_available=True, walk_forward_available=True,
            selection_win_rate=0.15, stability_score=0.20,
        )
        score = _compute_composite(snap)
        assert score < 0.25
        assert _grade(score) == "F"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Deterministic output
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministic:
    def test_same_input_same_output(self, monkeypatch, tmp_path: Path):
        """Running twice on same data → identical composite."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        r1 = run_benchmark()
        r2 = run_benchmark()

        assert r1["composite_score"] == r2["composite_score"]
        assert r1["composite_grade"] == r2["composite_grade"]
        assert r1["coverage_score"] == r2["coverage_score"]


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Atomic write
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_no_tmp_files(self, monkeypatch, tmp_path: Path):
        """No .tmp-* files left behind."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        run_benchmark()

        tmp_files = list(rb.BENCHMARK_ROOT.rglob("*.tmp-*"))
        assert len(tmp_files) == 0, f"Found tmp files: {tmp_files}"


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_all_fields_present(self, monkeypatch, tmp_path: Path):
        """BenchmarkSnapshot must contain all required fields."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        result = run_benchmark()

        required = [
            "benchmark_date", "benchmark_version",
            "selection_analytics_available", "walk_forward_available",
            "paper_analytics_available", "registry_available",
            "coverage_score",
            "selection_win_rate", "selection_avg_return_21d",
            "stability_score", "stability_periods",
            "paper_win_rate", "paper_avg_return",
            "dataset_total_rows", "dataset_missing_outcome_ratio",
            "dataset_unique_sectors", "ml_readiness_status",
            "composite_score", "composite_grade", "generated_at",
        ]
        for key in required:
            assert key in result, f"'{key}' missing from benchmark snapshot"

    def test_summary_has_latest_and_history(self, monkeypatch, tmp_path: Path):
        """benchmark_summary.json must have latest_snapshot and history."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        run_benchmark()
        summary = load_benchmark()

        assert summary is not None
        assert "latest_snapshot" in summary
        assert "history" in summary
        assert isinstance(summary["history"], list)
