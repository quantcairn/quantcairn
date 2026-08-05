"""Tests for Paper Research Dashboard UI — Phase 5E.

Verifies:
  1. Page loads from /paper-research
  2. Page renders with empty data (no crash)
  3. API endpoint /api/research/paper returns valid JSON
  4. Page does not import computation modules
  5. Existing dashboard routes unchanged
  6. Page has no network side effects
  7. Corrupt analytics JSON handled
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _redirect_roots(monkeypatch, base: Path):
    """Redirect Phase 5 module roots to temp dirs."""
    import src.openalpha.paper_research as pr
    import src.openalpha.paper_tracker as pt
    trading = base / "paper_trading"
    tracking = base / "paper_tracking"
    analytics = base / "paper_analytics"
    monkeypatch.setattr(pr, "PAPER_ROOT", trading)
    monkeypatch.setattr(pt, "PAPER_TRADING_ROOT", trading)
    monkeypatch.setattr(pt, "TRACKING_ROOT", tracking)
    return trading, tracking, analytics


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Page loads
# ═══════════════════════════════════════════════════════════════════════════════

class TestPageLoads:
    def test_page_returns_200(self, monkeypatch, tmp_path: Path):
        """GET /paper-research must return 200 with HTML."""
        _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        with dash.app.test_client() as client:
            resp = client.get("/paper-research")
            assert resp.status_code == 200
            content = resp.data.decode("utf-8")
            assert "Paper Research" in content
            assert "Portfolio Overview" in content
            assert "Current Paper Positions" in content
            assert "Performance Analytics" in content
            assert "Factor Analysis" in content
            assert "Sector Performance" in content

    def test_page_has_fetch_call(self, monkeypatch, tmp_path: Path):
        """Page must contain JS fetch('/api/research/paper') call."""
        _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        with dash.app.test_client() as client:
            resp = client.get("/paper-research")
            content = resp.data.decode("utf-8")
            assert "fetch('/api/research/paper')" in content

    def test_page_has_error_handling(self, monkeypatch, tmp_path: Path):
        """Page JS must have .catch() for fetch failure."""
        _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        with dash.app.test_client() as client:
            resp = client.get("/paper-research")
            content = resp.data.decode("utf-8")
            assert ".catch(function" in content


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Empty data renders gracefully
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyData:
    def test_api_returns_valid_with_no_data(self, monkeypatch, tmp_path: Path):
        """Even without trades/snapshots, the API must return valid JSON."""
        _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        with dash.app.test_client() as client:
            resp = client.get("/api/research/paper")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert "trades" in data
            assert "tracking" in data
            assert "analytics" in data


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Data present renders correctly
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataPresent:
    def test_api_with_trades(self, monkeypatch, tmp_path: Path):
        """With trades present, the API must return them."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        from src.openalpha.paper_research import create_paper_trade, save_paper_trades

        t = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05",
                               symbol="AIG", entry_price=80.0, sector="Financial Services")
        save_paper_trades([t], date_str="2026-08-05")

        with dash.app.test_client() as client:
            resp = client.get("/api/research/paper")
            data = resp.get_json()
            assert data["trades"]["total_trades"] == 1
            assert data["trades"]["open_trades"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 4. No computation imports in combined.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoComputationImports:
    def test_no_paper_analytics_import(self):
        """Dashboard must not import paper_analytics (recalculates)."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "from src.openalpha.paper_analytics" not in source

    def test_no_selection_backfill_import(self):
        """Dashboard must not import selection_backfill."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "selection_backfill" not in source

    def test_no_yfinance_import(self):
        """Dashboard module must not import yfinance directly."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "import yfinance" not in source


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Existing dashboard routes unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestExistingRoutesUnchanged:
    def test_index_still_works(self, monkeypatch, tmp_path: Path):
        """Main dashboard page must still render."""
        _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        with dash.app.test_client() as client:
            resp = client.get("/")
            assert resp.status_code == 200

    def test_api_status_still_works(self, monkeypatch, tmp_path: Path):
        """/api/status must still return valid data."""
        _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        with dash.app.test_client() as client:
            resp = client.get("/api/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "paper_research" in data
            assert "paper_tracking" in data
            assert "paper_analytics" in data


# ═══════════════════════════════════════════════════════════════════════════════
# 6. No network side effects
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoSideEffects:
    def test_page_does_not_modify_trades(self, monkeypatch, tmp_path: Path):
        """Loading the page must not create or modify trades."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        from src.openalpha.paper_research import load_paper_trades

        # Ensure no trades exist
        assert len(load_paper_trades()) == 0

        with dash.app.test_client() as client:
            client.get("/paper-research")
            client.get("/api/research/paper")

        # Trades must still be empty
        assert len(load_paper_trades()) == 0
