"""Tests for Research Analytics Dashboard Integration — Phase 3A.

Verifies:
  1. Payload loads valid analytics files correctly
  2. Missing files → available=False
  3. Corrupt JSON → available=False (no crash)
  4. /api/status contains research_analytics key
  5. Dashboard does NOT execute backfill/recompute
  6. Existing status fields unchanged
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_performance_summary(**overrides) -> dict:
    rec = {
        "total_selections": 120,
        "completed_outcomes": 110,
        "win_rate": 0.58,
        "average_return_5d_pct": 1.2,
        "average_return_21d_pct": 3.4,
        "average_mfe_21d_pct": 8.2,
        "average_mae_21d_pct": -2.1,
        "range_success_rate": 0.42,
        "return_21d_positive_count": 64,
        "return_21d_negative_count": 46,
        "analytics_version": "research_analytics.v1",
        "generated_at": "2026-08-05T12:00:00+00:00",
    }
    rec.update(overrides)
    return rec


def _make_feature_analysis() -> dict:
    return {
        "features": {
            "gap_rate": {
                "mean_all": 0.004,
                "mean_successful": 0.002,
                "mean_failed": 0.008,
                "min": 0.0,
                "max": 0.04,
                "correlation_with_return_21d": -0.42,
                "correlation_abs": 0.42,
                "correlation_direction": "negative",
                "samples": 100,
            },
            "atr_pct": {
                "mean_all": 2.5,
                "mean_successful": 2.8,
                "mean_failed": 2.1,
                "min": 1.0,
                "max": 6.0,
                "correlation_with_return_21d": 0.31,
                "correlation_abs": 0.31,
                "correlation_direction": "positive",
                "samples": 100,
            },
            "trend_score": {
                "mean_all": 55.0,
                "mean_successful": 60.0,
                "mean_failed": 48.0,
                "min": 0.0,
                "max": 100.0,
                "correlation_with_return_21d": 0.15,
                "correlation_abs": 0.15,
                "correlation_direction": "positive",
                "samples": 100,
            },
        },
        "analytics_version": "research_analytics.v1",
        "generated_at": "2026-08-05T12:00:00+00:00",
    }


def _make_sector_analysis() -> dict:
    return {
        "sectors": {
            "Financial Services": {
                "samples": 50,
                "success_rate": 0.64,
                "avg_return_21d_pct": 4.2,
                "avg_mfe_21d_pct": 9.1,
                "avg_mae_21d_pct": -1.8,
            },
            "Healthcare": {
                "samples": 30,
                "success_rate": 0.50,
                "avg_return_21d_pct": 2.5,
                "avg_mfe_21d_pct": 7.0,
                "avg_mae_21d_pct": -2.5,
            },
            "Consumer Discretionary": {
                "samples": 20,
                "success_rate": 0.40,
                "avg_return_21d_pct": 0.8,
                "avg_mfe_21d_pct": 6.5,
                "avg_mae_21d_pct": -3.2,
            },
        },
        "unique_sectors": 3,
        "analytics_version": "research_analytics.v1",
        "generated_at": "2026-08-05T12:00:00+00:00",
    }


def _write_analytics_files(analytics_dir: Path) -> None:
    analytics_dir.mkdir(parents=True, exist_ok=True)
    (analytics_dir / "performance_summary.json").write_text(
        json.dumps(_make_performance_summary(), ensure_ascii=False))
    (analytics_dir / "feature_analysis.json").write_text(
        json.dumps(_make_feature_analysis(), ensure_ascii=False))
    (analytics_dir / "sector_analysis.json").write_text(
        json.dumps(_make_sector_analysis(), ensure_ascii=False))


def _redirect_analytics_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect the analytics directory to a temp path."""
    import src.dashboard.combined as dash
    new_dir = tmp_path / "analytics"
    monkeypatch.setattr(
        dash, "PROJECT_DIR",
        tmp_path,
        raising=False,
    )
    return new_dir


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Payload loads valid files
# ═══════════════════════════════════════════════════════════════════════════════

class TestPayloadLoadsValidFiles:
    def test_available_true_with_all_files(self, monkeypatch, tmp_path: Path):
        """When all three analytics files exist, payload must return available=True."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        _write_analytics_files(analytics_dir)

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        assert payload["available"] is True
        assert "summary" in payload
        assert "top_features" in payload
        assert "sector_summary" in payload
        assert payload["analytics_version"] == "research_analytics.v1"

    def test_summary_has_expected_fields(self, monkeypatch, tmp_path: Path):
        """Summary dict must contain total_selections, win_rate, returns, range rate."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        _write_analytics_files(analytics_dir)

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        s = payload["summary"]
        assert s["total_selections"] == 120
        assert s["win_rate"] == 0.58
        assert s["average_return_21d_pct"] == 3.4
        assert s["range_success_rate"] == 0.42

    def test_top_features_sorted_by_abs_correlation(self, monkeypatch, tmp_path: Path):
        """Top features must be sorted by absolute correlation descending."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        _write_analytics_files(analytics_dir)

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        top = payload["top_features"]
        assert len(top) == 3
        # gap_rate has -0.42 (abs=0.42), atr_pct has 0.31, trend_score has 0.15
        assert top[0]["name"] == "gap_rate"
        assert top[0]["correlation"] == -0.42
        assert top[0]["direction"] == "negative"

    def test_sector_summary_has_all_sectors(self, monkeypatch, tmp_path: Path):
        """Sector summary must include all sectors with samples, success_rate, avg_return."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        _write_analytics_files(analytics_dir)

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        sectors = payload["sector_summary"]
        assert len(sectors) == 3
        # Sorted by samples descending
        assert sectors[0]["sector"] == "Financial Services"
        assert sectors[0]["samples"] == 50
        assert sectors[0]["success_rate"] == 0.64

    def test_partial_files_still_available(self, monkeypatch, tmp_path: Path):
        """Even if only one file exists, payload must return available=True
        with whatever data is present."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        # Only write performance_summary, not the others
        (analytics_dir / "performance_summary.json").write_text(
            json.dumps(_make_performance_summary()))

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        assert payload["available"] is True
        assert payload["summary"]["total_selections"] == 120
        # Features and sectors empty but no crash
        assert isinstance(payload["top_features"], list)
        assert isinstance(payload["sector_summary"], list)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Missing analytics files → available=False
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingFiles:
    def test_empty_directory_returns_unavailable(self, monkeypatch, tmp_path: Path):
        """When analytics directory doesn't exist → available=False."""
        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        assert payload["available"] is False
        assert "reason" in payload

    def test_no_files_returns_unavailable(self, monkeypatch, tmp_path: Path):
        """When analytics directory exists but is empty → available=False."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        assert payload["available"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Corrupt JSON → available=False (no crash)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCorruptJSON:
    def test_corrupt_performance_json_no_crash(self, monkeypatch, tmp_path: Path):
        """Corrupt JSON must not crash — return available=False."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        (analytics_dir / "performance_summary.json").write_text("not valid json {{{")

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        # Must not raise
        payload = dash._research_analytics_payload()
        assert payload["available"] is False

    def test_wrong_type_not_dict_no_crash(self, monkeypatch, tmp_path: Path):
        """JSON that parses to a list (not dict) must not crash."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        (analytics_dir / "performance_summary.json").write_text('[1, 2, 3]')

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        payload = dash._research_analytics_payload()
        # JSON that is a list → None from _read → treated as missing
        assert payload["available"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. /api/status contains research_analytics key
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiStatusContainsResearchAnalytics:
    def test_api_status_has_research_analytics_key(self):
        """_api_status_payload must return a dict containing 'research_analytics'."""
        import src.dashboard.combined as dash

        payload = dash._api_status_payload()
        assert "research_analytics" in payload, (
            "research_analytics key missing from /api/status payload"
        )

    def test_research_analytics_is_dict(self):
        """The research_analytics value must be a dict."""
        import src.dashboard.combined as dash

        payload = dash._api_status_payload()
        ra = payload.get("research_analytics")
        assert isinstance(ra, dict), (
            f"research_analytics must be dict, got {type(ra)}"
        )
        # Must have 'available' key
        assert "available" in ra


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Dashboard does NOT execute backfill/recompute
# ═══════════════════════════════════════════════════════════════════════════════

class TestDashboardDoesNotExecuteBackfill:
    def test_no_import_from_backfill(self):
        """The dashboard combined.py must not import selection_backfill."""
        import src.dashboard.combined as dash
        source = dash.__file__
        if source:
            content = Path(source).read_text()
            assert "selection_backfill" not in content, (
                "dashboard must not import selection_backfill"
            )

    def test_no_import_from_learning_dataset(self):
        """The dashboard must not import learning_dataset (rebuilds)."""
        import src.dashboard.combined as dash
        source = dash.__file__
        if source:
            content = Path(source).read_text()
            assert "learning_dataset" not in content, (
                "dashboard must not import learning_dataset"
            )

    def test_no_import_from_research_analytics(self):
        """The dashboard must not import the research_analytics module (recalculates).
        The function name '_research_analytics_payload' is fine — we check for
        the import statement, not the function definition."""
        import src.dashboard.combined as dash
        source = dash.__file__
        if source:
            content = Path(source).read_text()
            # Check for module import, not the function name
            assert "from src.openalpha.research_analytics" not in content, (
                "dashboard must not import research_analytics module"
            )
            assert "import research_analytics" not in content, (
                "dashboard must not import research_analytics module"
            )

    def test_no_yfinance_import_in_payload_function(self, monkeypatch, tmp_path: Path):
        """The _research_analytics_payload function must not touch yfinance."""
        analytics_dir = tmp_path / "artifacts" / "learning" / "analytics"
        _write_analytics_files(analytics_dir)

        import src.dashboard.combined as dash
        monkeypatch.setattr(dash, "PROJECT_DIR", tmp_path)

        # If this function somehow triggered yfinance, it would fail in CI
        # (no network).  Merely calling it must succeed.
        payload = dash._research_analytics_payload()
        assert payload["available"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Existing status fields unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestExistingFieldsUnchanged:
    def test_all_pre_existing_keys_still_present(self):
        """All keys that existed in _api_status_payload before Phase 3A
        must still be present."""
        import src.dashboard.combined as dash

        payload = dash._api_status_payload()

        # Keys that must exist (pre-3A payload structure)
        required_keys = [
            "ok", "mode", "timestamp",
            "selection", "top_engines",
            "dashboard", "shadow",
            "candidate_validation", "candidate_model_evaluation",
            "research_status", "learning_summary",
            "market_regime", "regime_shadow",
            "universe", "governance", "research_report",
            "ai_selection",
        ]

        for key in required_keys:
            assert key in payload, (
                f"Pre-existing key '{key}' missing from /api/status payload"
            )

    def test_selection_keys_unchanged(self):
        """Selection sub-dict must retain all its keys."""
        import src.dashboard.combined as dash

        payload = dash._api_status_payload()
        sel = payload.get("selection", {})

        selection_keys = [
            "synced", "selection_date",
            "selection_state_tickers", "top_config_tickers",
            "fallback_used", "reason", "presentation",
        ]
        for key in selection_keys:
            assert key in sel, (
                f"selection.{key} missing from /api/status payload"
            )

    def test_ai_selection_keys_unchanged(self):
        """ai_selection sub-dict must retain all its keys."""
        import src.dashboard.combined as dash

        payload = dash._api_status_payload()
        ai = payload.get("ai_selection", {})

        ai_keys = [
            "price_band", "execution_status", "result_quality",
            "research_admission", "fallback_used",
        ]
        for key in ai_keys:
            assert key in ai, (
                f"ai_selection.{key} missing from /api/status payload"
            )
