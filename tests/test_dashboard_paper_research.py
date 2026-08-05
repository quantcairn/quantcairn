"""Tests for Paper Research Dashboard Integration — Phase 5D.

Verifies:
  1. Paper trades payload available
  2. Paper tracking payload available
  3. Paper analytics payload available
  4. /api/status contains paper keys
  5. /api/research/paper endpoint returns 200
  6. Existing keys unchanged
  7. Missing data → available=False
  8. No computation during request
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

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


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Paper trades payload
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaperTradesPayload:
    def test_available_true_with_trades(self, monkeypatch, tmp_path: Path):
        """Payload returns available=True when trades exist."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        from src.openalpha.paper_research import create_paper_trade, save_paper_trades
        t = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05",
                               symbol="AIG", entry_price=80.0, sector="Financial Services")
        save_paper_trades([t], date_str="2026-08-05")

        payload = dash._paper_research_payload()
        assert payload["available"] is True
        assert payload["total_trades"] == 1
        assert payload["open_trades"] == 1
        assert len(payload["trades"]) == 1

    def test_available_true_with_no_trades(self, monkeypatch, tmp_path: Path):
        """No trades → available=True, total=0 (not an error)."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        payload = dash._paper_research_payload()
        assert payload["available"] is True
        assert payload["total_trades"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Paper tracking payload
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaperTrackingPayload:
    def test_available_true_with_no_data(self, monkeypatch, tmp_path: Path):
        """Empty tracking → available=True (not an error)."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        payload = dash._paper_tracking_payload()
        assert payload["available"] is True
        assert payload["total_snapshots"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Paper analytics payload
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaperAnalyticsPayload:
    def test_available_false_with_no_data(self, monkeypatch, tmp_path: Path):
        """No analytics files → available=False."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        payload = dash._paper_analytics_payload()
        assert payload["available"] is False

    def test_available_true_with_data(self, monkeypatch, tmp_path: Path):
        """Valid analytics files → available=True."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash
        # Redirect PROJECT_DIR so the dashboard reads from our temp dir
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        analytics = tmp_path / "artifacts" / "learning" / "paper_analytics"
        analytics.mkdir(parents=True, exist_ok=True)
        _write_json(analytics / "summary.json", {
            "analytics_version": "paper_analytics.v1",
            "generated_at": "2026-08-05T00:00:00+00:00",
            "totals": {"total_trades": 10, "open_trades": 5},
            "performance": {"win_rate": 0.6, "avg_return_pct": 3.5},
            "range_analysis": {"breakdown_count": 2},
        })
        _write_json(analytics / "factor_analysis.json", {
            "factors": {"score": {"correlation_with_return": 0.42}},
        })

        payload = dash._paper_analytics_payload()
        assert payload["available"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 4. /api/status contains paper keys
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiStatusPaperKeys:
    def test_status_has_all_paper_keys(self, monkeypatch, tmp_path: Path):
        """/api/status must contain paper_research, paper_tracking, paper_analytics."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        payload = dash._api_status_payload()

        for key in ["paper_research", "paper_tracking", "paper_analytics"]:
            assert key in payload, f"'{key}' missing from /api/status"
            assert isinstance(payload[key], dict)
            assert "available" in payload[key]


# ═══════════════════════════════════════════════════════════════════════════════
# 5. /api/research/paper endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaperEndpoint:
    def test_endpoint_returns_200(self, monkeypatch, tmp_path: Path):
        """The /api/research/paper endpoint must return ok=True."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        with dash.app.test_client() as client:
            resp = client.get("/api/research/paper")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            for key in ["trades", "tracking", "analytics"]:
                assert key in data


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Existing keys unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestExistingKeys:
    def test_pre_existing_keys_present(self, monkeypatch, tmp_path: Path):
        """All pre-5D keys must still be in _api_status_payload."""
        trading, tracking, analytics = _redirect_roots(monkeypatch, tmp_path)
        import src.dashboard.combined as dash

        payload = dash._api_status_payload()

        for key in ["ok", "mode", "timestamp", "selection", "top_engines",
                     "dashboard", "research_analytics", "research_walk_forward",
                     "research_registry", "research_quality"]:
            assert key in payload, f"Pre-existing key '{key}' missing"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. No computation during request
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoComputation:
    def test_no_paper_analytics_import(self):
        """Dashboard must not import paper_analytics (recalculates)."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "from src.openalpha.paper_analytics" not in source

    def test_no_backfill_import(self):
        """Dashboard must not import selection_backfill."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "selection_backfill" not in source
