"""Tests for Research Report Generator — Phase 6C.

Verifies:
  1. All sources available → full report generated
  2. Missing benchmark → report still generated from remaining sources
  3. Missing regime → regime section shows available=False
  4. Corrupt JSON → handled gracefully
  5. Insufficient data → status=INSUFFICIENT_DATA
  6. Risk generation from data
  7. Deterministic output
  8. Schema validation
  9. Atomic write
  10. Empty research data
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.openalpha import research_report as rr
from src.openalpha.research_report import (
    run_research_report,
    load_research_report,
    REPORT_ROOT,
    LEARNING_ROOT,
    _generate_risks,
    _determine_research_status,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_roots(monkeypatch, base: Path) -> Path:
    learning = base / "learning"
    report_dir = learning / "research_report"
    monkeypatch.setattr(rr, "LEARNING_ROOT", learning)
    monkeypatch.setattr(rr, "REPORT_ROOT", report_dir)
    return learning


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_benchmark(grade: str = "B", sel_win_rate: float = 0.62) -> dict:
    return {
        "latest_snapshot": {
            "composite_grade": grade,
            "composite_score": 0.62,
            "selection_win_rate": sel_win_rate,
            "selection_total_samples": 75,
            "coverage_score": 1.0,
        },
        "history": [],
    }


def _make_regime() -> dict:
    return {
        "available": True,
        "best_regime": "bull",
        "worst_regime": "bear",
        "regime_robustness": "MODERATE",
    }


def _make_walk_forward() -> dict:
    return {
        "overall_stability": 0.85,
        "total_periods": 3,
        "flags": [],
    }


def _make_selection() -> dict:
    return {
        "performance": {"win_rate": 0.62},
        "total_selections": 75,
    }


def _make_paper() -> dict:
    return {
        "totals": {"total_trades": 25},
        "performance": {"win_rate": 0.60, "avg_return_pct": 3.5},
    }


def _write_all_sources(learning_dir: Path) -> None:
    _write_json(learning_dir / "research_benchmark" / "benchmark_summary.json",
                _make_benchmark())
    _write_json(learning_dir / "regime_analysis" / "regime_summary.json",
                _make_regime())
    _write_json(learning_dir / "walk_forward" / "walk_forward_summary.json",
                _make_walk_forward())
    _write_json(learning_dir / "analytics" / "performance_summary.json",
                _make_selection())
    _write_json(learning_dir / "paper_analytics" / "summary.json",
                _make_paper())


# ═══════════════════════════════════════════════════════════════════════════════
# 1. All sources available
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllSources:
    def test_full_report_generated(self, monkeypatch, tmp_path: Path):
        """All 5 sources → status=READY, all sections populated."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        report = run_research_report()

        assert report["report_version"] == rr.REPORT_VERSION
        assert report["executive_summary"]["research_status"] == "READY"
        assert report["executive_summary"]["sources_available"] == 5
        assert len(report["executive_summary"]["key_findings"]) >= 2
        assert report["benchmark"] != {}
        assert report["validation"]["walk_forward"]["available"] is True
        assert report["regime_analysis"]["available"] is True
        assert report["paper_research"]["available"] is True

    def test_report_has_recommendations(self, monkeypatch, tmp_path: Path):
        """Report must include recommendations array."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        report = run_research_report()
        assert len(report["recommendations"]) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Missing benchmark
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingBenchmark:
    def test_report_generated_without_benchmark(self, monkeypatch, tmp_path: Path):
        """Missing benchmark → report still generated, benchmark section empty."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        # Write all EXCEPT benchmark
        _write_json(learning / "regime_analysis" / "regime_summary.json",
                    _make_regime())
        _write_json(learning / "walk_forward" / "walk_forward_summary.json",
                    _make_walk_forward())
        _write_json(learning / "analytics" / "performance_summary.json",
                    _make_selection())
        _write_json(learning / "paper_analytics" / "summary.json",
                    _make_paper())

        report = run_research_report()
        assert report["benchmark"] == {}
        assert report["executive_summary"]["sources_available"] == 4


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Missing regime
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingRegime:
    def test_regime_section_unavailable(self, monkeypatch, tmp_path: Path):
        """Missing regime → regime_analysis.available=False."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_json(learning / "research_benchmark" / "benchmark_summary.json",
                    _make_benchmark())

        report = run_research_report()
        assert report["regime_analysis"]["available"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Corrupt JSON
# ═══════════════════════════════════════════════════════════════════════════════

class TestCorruptJSON:
    def test_corrupt_one_source_others_ok(self, monkeypatch, tmp_path: Path):
        """Corrupt benchmark → that section empty, others OK."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        (learning / "research_benchmark").mkdir(parents=True, exist_ok=True)
        (learning / "research_benchmark" / "benchmark_summary.json").write_text("bad json")
        _write_json(learning / "regime_analysis" / "regime_summary.json",
                    _make_regime())

        report = run_research_report()
        assert report["report_version"] == rr.REPORT_VERSION
        assert report["regime_analysis"]["available"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Insufficient data status
# ═══════════════════════════════════════════════════════════════════════════════

class TestInsufficientData:
    def test_no_sources_is_insufficient(self, monkeypatch, tmp_path: Path):
        """Zero sources → status=INSUFFICIENT_DATA."""
        learning = _redirect_roots(monkeypatch, tmp_path)

        report = run_research_report()
        assert report["executive_summary"]["research_status"] == "INSUFFICIENT_DATA"

    def test_one_source_is_developing(self, monkeypatch, tmp_path: Path):
        """1 source → status=DEVELOPING."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_json(learning / "analytics" / "performance_summary.json",
                    _make_selection())

        report = run_research_report()
        assert report["executive_summary"]["research_status"] == "DEVELOPING"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Risk generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskGeneration:
    def test_low_sample_risk_fires(self):
        """Benchmark with <50 samples → LOW_SAMPLE_SIZE risk."""
        benchmark = {
            "latest_snapshot": {
                "selection_total_samples": 25,
                "selection_win_rate": 0.60,
            }
        }
        risks = _generate_risks(benchmark, None, None, None, None)
        low_sample = [r for r in risks if r["type"] == "LOW_SAMPLE_SIZE"]
        assert len(low_sample) >= 1
        assert low_sample[0]["severity"] == "MEDIUM"

    def test_weak_regime_risk_fires(self):
        """WEAK regime → HIGH severity REGIME_DEPENDENCY risk."""
        regime = {"regime_robustness": "WEAK"}
        risks = _generate_risks(None, regime, None, None, None)
        reg_risk = [r for r in risks if r["type"] == "REGIME_DEPENDENCY"]
        assert len(reg_risk) >= 1
        assert reg_risk[0]["severity"] == "HIGH"

    def test_no_risks_when_all_healthy(self):
        """No issues → empty risks."""
        benchmark = {"latest_snapshot": {"selection_total_samples": 200}}
        risks = _generate_risks(benchmark, None, None, None, None)
        assert len(risks) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Deterministic output
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministic:
    def test_same_input_same_output(self, monkeypatch, tmp_path: Path):
        """Running twice on same data → identical report (except generated_at)."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        r1 = run_research_report()
        r2 = run_research_report()

        assert r1["executive_summary"]["research_status"] == \
               r2["executive_summary"]["research_status"]
        assert r1["risks"] == r2["risks"]
        assert r1["recommendations"] == r2["recommendations"]


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_all_sections_present(self, monkeypatch, tmp_path: Path):
        """Report must contain all top-level sections."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        report = run_research_report()

        for key in ["report_version", "generated_at", "executive_summary",
                     "benchmark", "validation", "regime_analysis",
                     "paper_research", "risks", "recommendations"]:
            assert key in report, f"'{key}' missing from report"

    def test_executive_summary_has_all_fields(self, monkeypatch, tmp_path: Path):
        """executive_summary must have research_status, grade, key_findings."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        report = run_research_report()
        es = report["executive_summary"]
        for key in ["research_status", "composite_grade", "sources_available",
                     "key_findings"]:
            assert key in es, f"'{key}' missing from executive_summary"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Atomic write
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_no_tmp_files(self, monkeypatch, tmp_path: Path):
        """No .tmp-* files left behind."""
        learning = _redirect_roots(monkeypatch, tmp_path)
        _write_all_sources(learning)

        run_research_report()

        tmp_files = list(rr.REPORT_ROOT.rglob("*.tmp-*"))
        assert len(tmp_files) == 0, f"Found tmp files: {tmp_files}"


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Empty research data
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyData:
    def test_empty_produces_valid_report(self, monkeypatch, tmp_path: Path):
        """No data at all → valid report with INSUFFICIENT_DATA status."""
        learning = _redirect_roots(monkeypatch, tmp_path)

        report = run_research_report()
        assert report is not None
        assert report["report_version"] == rr.REPORT_VERSION
        assert report["executive_summary"]["research_status"] == "INSUFFICIENT_DATA"

    def test_report_written_to_disk(self, monkeypatch, tmp_path: Path):
        """Report file must be written even with no data."""
        learning = _redirect_roots(monkeypatch, tmp_path)

        run_research_report()
        loaded = load_research_report()
        assert loaded is not None
        assert loaded["executive_summary"]["research_status"] == "INSUFFICIENT_DATA"
