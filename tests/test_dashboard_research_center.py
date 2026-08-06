"""Tests for Research Center Dashboard — Phase 6D.

Verifies:
  1. /research-center page loads
  2. /api/research/center returns valid JSON
  3. Missing files → available=False
  4. Corrupt JSON → handled
  5. Existing dashboard routes unchanged
  6. No computation during request
  7. API status has new keys
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


# 1. Page loads
class TestPageLoads:
    def test_research_center_page_returns_200(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-center")
            assert r.status_code == 200
            assert b"Research Center" in r.data

    def test_page_has_nav_links(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/research-center")
            assert b"Dashboard" in r.data
            assert b"Paper Research" in r.data


# 2. API returns valid JSON
class TestAPICenter:
    def test_center_api_returns_ok(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            r = c.get("/api/research/center")
            assert r.status_code == 200
            d = r.get_json()
            assert d["ok"] is True
            for k in ["research_report", "benchmark", "walk_forward",
                       "regime", "paper_research", "paper_tracking"]:
                assert k in d

    def test_all_sections_have_available(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            d = c.get("/api/research/center").get_json()
            for k in ["research_report", "benchmark", "walk_forward",
                       "regime", "paper_research"]:
                assert "available" in d[k], f"'{k}' missing 'available'"


# 3. Missing files → available=False
class TestMissingFiles:
    def test_all_missing_is_available_false(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        p = dash._research_report_payload()
        assert p["available"] is False
        assert "reason" in p


# 4. Corrupt JSON
class TestCorruptJSON:
    def test_corrupt_report_no_crash(self, monkeypatch, tmp_path: Path):
        base = _redirect_project(monkeypatch, tmp_path)
        rd = base / "artifacts" / "learning" / "research_report"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "research_report.json").write_text("not json {{{")

        import src.dashboard.combined as dash
        p = dash._research_report_payload()
        assert p["available"] is False


# 5. Existing dashboard unchanged
class TestExistingRoutes:
    def test_index_still_works(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            assert c.get("/").status_code == 200

    def test_api_status_still_works(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        with dash.app.test_client() as c:
            assert c.get("/api/status").status_code == 200


# 6. No computation imports
class TestNoComputation:
    def test_no_research_benchmark_import(self):
        import src.dashboard.combined as dash
        src = Path(dash.__file__).read_text()
        assert "from src.openalpha.research_benchmark" not in src

    def test_no_regime_analysis_import(self):
        import src.dashboard.combined as dash
        src = Path(dash.__file__).read_text()
        assert "from src.openalpha.regime_analysis" not in src

    def test_no_research_report_import(self):
        import src.dashboard.combined as dash
        src = Path(dash.__file__).read_text()
        assert "from src.openalpha.research_report" not in src


# 7. API status has new keys
class TestAPIStatusNewKeys:
    def test_new_keys_present(self, monkeypatch, tmp_path: Path):
        _redirect_project(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        p = dash._api_status_payload()
        for k in ["research_report", "research_benchmark", "research_center_regime"]:
            assert k in p, f"'{k}' missing from /api/status"
            assert "available" in p[k]
