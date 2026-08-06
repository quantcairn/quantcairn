"""Tests for Research Report Dashboard page.

Verifies:
  1. /research-report page returns 200
  2. Page contains key titles (Executive Summary, Benchmark, etc.)
  3. Nav links are present
  4. API data missing → graceful fallback (no crash)
  5. Existing routes (/paper-research, /research-center, /api/status) unaffected
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _redirect_project(monkeypatch, base: Path):
    monkeypatch.setattr("src.dashboard.combined.PROJECT_DIR", base)
    return base


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Page loads
# ---------------------------------------------------------------------------

class TestPageLoads:
    def test_research_report_page_returns_200(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-report")
            assert r.status_code == 200
            assert b"Research Report" in r.data

    def test_page_has_nav_links(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-report")
            assert b"Dashboard" in r.data
            assert b"Paper Research" in r.data
            assert b"Research Center" in r.data

    def test_page_has_key_sections(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-report")
            # Key section headings must appear in the HTML
            assert b"Executive Summary" in r.data
            assert b"Research Benchmark" in r.data
            assert b"Regime Analysis" in r.data
            assert b"Paper Research Summary" in r.data
            assert b"Risks" in r.data
            assert b"Recommendations" in r.data


# ---------------------------------------------------------------------------
# 2. Graceful fallback with no research data
# ---------------------------------------------------------------------------

class TestGracefulFallback:
    def test_page_renders_with_no_data(self, monkeypatch, tmp_path: Path):
        """Page must render with a 200 even when zero research artifacts exist."""
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-report")
            assert r.status_code == 200
            assert b"Research Report" in r.data

    def test_page_renders_with_corrupt_json(self, monkeypatch, tmp_path: Path):
        """Corrupt research data must not crash the page render."""
        base = _redirect_project(monkeypatch, tmp_path)
        _write_json(
            base / "artifacts" / "learning" / "research_report" / "research_report.json",
            {"invalid": "structure"},
        )
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-report")
            assert r.status_code == 200

    def test_payload_available_false_when_no_data(self, monkeypatch, tmp_path: Path):
        """Research report payload returns available=False with no data directory."""
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        p = dash._research_report_payload()
        assert p["available"] is False
        assert "reason" in p


# ---------------------------------------------------------------------------
# 3. Existing routes unaffected
# ---------------------------------------------------------------------------

class TestExistingRoutesUnaffected:
    def test_paper_research_still_works(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/paper-research")
            assert r.status_code == 200
            assert b"Paper Research" in r.data

    def test_research_center_still_works(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-center")
            assert r.status_code == 200
            assert b"Research Center" in r.data

    def test_main_index_still_works(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/")
            assert r.status_code == 200

    def test_api_status_still_works(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/api/status")
            assert r.status_code == 200
            d = r.get_json()
            assert d["ok"] is True

    def test_api_center_still_works(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/api/research/center")
            assert r.status_code == 200
            d = r.get_json()
            assert d["ok"] is True


# ---------------------------------------------------------------------------
# 4. Main index nav buttons
# ---------------------------------------------------------------------------

class TestMainIndexNav:
    def test_nav_has_paper_research_button(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/")
            assert r.status_code == 200
            assert b'<a href="/paper-research">Paper Research</a>' in r.data

    def test_nav_has_research_report_button(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/")
            assert r.status_code == 200
            assert b'<a href="/research-report">Research Report</a>' in r.data

    def test_nav_has_research_center_button(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/")
            assert r.status_code == 200
            assert b'<a href="/research-center">Research Center</a>' in r.data


# ---------------------------------------------------------------------------
# 5. Benchmark payload
# ---------------------------------------------------------------------------

class TestBenchmarkPayload:
    def test_missing_benchmark_returns_available_false(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        p = dash._research_benchmark_payload()
        assert p["available"] is False
        assert "reason" in p

    def test_valid_benchmark_returns_available_true(self, monkeypatch, tmp_path: Path):
        base = _redirect_project(monkeypatch, tmp_path)
        _write_json(
            base / "artifacts" / "learning" / "research_benchmark" / "benchmark_summary.json",
            {
                "latest_snapshot": {
                    "composite_grade": "A",
                    "composite_score": 0.85,
                    "coverage_score": 0.75,
                    "selection_win_rate": 0.62,
                    "stability_score": 0.85,
                    "paper_win_rate": 0.58,
                },
                "history": [
                    {"benchmark_date": "2026-08-01", "composite_grade": "A", "composite_score": 0.85},
                ],
            },
        )
        import src.dashboard.combined as dash
        p = dash._research_benchmark_payload()
        assert p["available"] is True
        assert p["composite_grade"] == "A"
        assert p["composite_score"] == 0.85
        assert len(p["history"]) == 1


# ---------------------------------------------------------------------------
# 6. Research report payload with valid data
# ---------------------------------------------------------------------------

class TestResearchReportPayload:
    def test_valid_report_returns_data(self, monkeypatch, tmp_path: Path):
        base = _redirect_project(monkeypatch, tmp_path)
        _write_json(
            base / "artifacts" / "learning" / "research_report" / "research_report.json",
            {
                "executive_summary": {
                    "research_status": "READY",
                    "composite_grade": "A",
                    "sources_available": 4,
                    "key_findings": ["Selection win rate above 60%", "Walk-forward stable"],
                },
                "risks": [
                    {"type": "LOW_SAMPLE_SIZE", "severity": "MEDIUM", "description": "Less than 50 samples"},
                ],
                "recommendations": ["Collect more paper trading data"],
                "generated_at": "2026-08-06T12:00:00Z",
            },
        )
        import src.dashboard.combined as dash
        p = dash._research_report_payload()
        assert p["available"] is True
        assert p["research_status"] == "READY"
        assert p["composite_grade"] == "A"
        assert p["sources_available"] == 4
        assert len(p["key_findings"]) == 2
        assert len(p["risks"]) == 1
        assert p["risks"][0]["severity"] == "MEDIUM"
        assert len(p["recommendations"]) == 1
