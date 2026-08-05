"""Tests for Research Dashboard Observability — Phase 4B.

Verifies:
  1. Walk-forward payload loads valid data
  2. Walk-forward payload returns available=False when missing
  3. Walk-forward payload doesn't crash on corrupt JSON
  4. Registry payload counts
  5. Registry payload returns available=False when missing
  6. Quality payload includes ML readiness
  7. Quality payload returns available=False when missing
  8. /api/status contains all new keys
  9. /api/research/observability endpoint returns 200
  10. No computation modules imported during dashboard request
  11. Existing /api/status keys unchanged
  12. Backward compatibility — missing files don't break dashboard
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_project_dir(monkeypatch, base: Path) -> Path:
    """Redirect PROJECT_DIR to a temp directory."""
    import src.dashboard.combined as dash
    monkeypatch.setattr(dash, "PROJECT_DIR", base)
    return base


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Walk-forward payload
# ═══════════════════════════════════════════════════════════════════════════════

class TestWalkForwardPayload:
    def test_available_with_valid_data(self, monkeypatch, tmp_path: Path):
        """Valid JSON must produce available=True with correct fields."""
        base = _redirect_project_dir(monkeypatch, tmp_path)
        wf_dir = base / "artifacts" / "learning" / "walk_forward"

        _write_json(wf_dir / "walk_forward_summary.json", {
            "walk_forward_version": "walk_forward.v1",
            "generated_at": "2026-08-05T00:00:00+00:00",
            "total_periods": 3,
            "total_rows": 120,
            "overall_stability": 0.85,
            "forward_win_rate_range": {"min": 0.52, "max": 0.64, "mean": 0.58, "std": 0.06},
            "flags": [{"type": "LOW_SAMPLE_SIZE", "period": "P1", "message": "Low samples"}],
        })
        _write_json(wf_dir / "period_results.json", {
            "periods": [
                {"period_id": "P1", "train_samples": 60, "validation_samples": 20,
                 "forward_samples": 20, "forward_win_rate": 0.62,
                 "forward_avg_return_21d": 3.5},
                {"period_id": "P2", "train_samples": 60, "validation_samples": 20,
                 "forward_samples": 20, "forward_win_rate": 0.55,
                 "forward_avg_return_21d": 2.1},
            ]
        })

        import src.dashboard.combined as dash
        payload = dash._walk_forward_payload()

        assert payload["available"] is True
        assert payload["summary"]["total_periods"] == 3
        assert payload["summary"]["overall_stability"] == 0.85
        assert payload["summary"]["forward_win_rate_mean"] == 0.58
        assert len(payload["flags"]) == 1
        assert payload["flags"][0]["type"] == "LOW_SAMPLE_SIZE"
        assert len(payload["periods"]) == 2

    def test_missing_returns_unavailable(self, monkeypatch, tmp_path: Path):
        """No walk-forward directory → available=False."""
        base = _redirect_project_dir(monkeypatch, tmp_path)

        import src.dashboard.combined as dash
        payload = dash._walk_forward_payload()

        assert payload["available"] is False
        assert "reason" in payload

    def test_corrupt_json_no_crash(self, monkeypatch, tmp_path: Path):
        """Corrupt summary JSON must not crash — return available=False."""
        base = _redirect_project_dir(monkeypatch, tmp_path)
        wf_dir = base / "artifacts" / "learning" / "walk_forward"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "walk_forward_summary.json").write_text("not valid json {{{", encoding="utf-8")

        import src.dashboard.combined as dash
        payload = dash._walk_forward_payload()

        assert payload["available"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Registry payload
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegistryPayload:
    def test_counts_from_run_index(self, monkeypatch, tmp_path: Path):
        """Registry payload must correctly count runs, modes, and regimes."""
        base = _redirect_project_dir(monkeypatch, tmp_path)
        hist_dir = base / "artifacts" / "learning" / "research_history"

        _write_json(hist_dir / "run_index.json", [
            {"selection_run_id": "r1", "selection_date": "2026-08-01",
             "execution_mode": "RESEARCH"},
            {"selection_run_id": "r2", "selection_date": "2026-08-02",
             "execution_mode": "RESEARCH"},
            {"selection_run_id": "r3", "selection_date": "2026-08-03",
             "execution_mode": "PAPER"},
        ])
        _write_json(hist_dir / "dataset_tracker.json", {
            "ledger": {"runs": 2, "candidates": 8},
            "outcomes": {"runs": 1, "success": 3},
            "dataset": {"rows": 5, "snapshot_date": "2026-08-05"},
        })
        _write_json(hist_dir / "regime_tags.json", {
            "tags": [
                {"date": "2026-08-01", "regime": "bull"},
                {"date": "2026-08-02", "regime": "sideways"},
                {"date": "2026-08-03", "regime": "sideways"},
            ]
        })

        import src.dashboard.combined as dash
        payload = dash._research_registry_payload()

        assert payload["available"] is True
        assert payload["total_runs"] == 3
        assert payload["run_modes"] == {"RESEARCH": 2, "PAPER": 1}
        assert payload["date_range"]["earliest"] == "2026-08-01"
        assert payload["date_range"]["latest"] == "2026-08-03"
        assert payload["dataset_growth"]["ledger_runs"] == 2
        assert payload["dataset_growth"]["outcome_success"] == 3
        assert payload["dataset_growth"]["dataset_rows"] == 5
        assert payload["regimes"] == {"bull": 1, "sideways": 2}

    def test_missing_returns_unavailable(self, monkeypatch, tmp_path: Path):
        """No registry data → available=False."""
        base = _redirect_project_dir(monkeypatch, tmp_path)

        import src.dashboard.combined as dash
        payload = dash._research_registry_payload()

        assert payload["available"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Quality payload
# ═══════════════════════════════════════════════════════════════════════════════

class TestQualityPayload:
    def test_includes_ml_readiness(self, monkeypatch, tmp_path: Path):
        """Quality payload must include ML readiness status and thresholds."""
        base = _redirect_project_dir(monkeypatch, tmp_path)
        hist_dir = base / "artifacts" / "learning" / "research_history"

        _write_json(hist_dir / "research_quality_report.json", {
            "total_runs_recorded": 45,
            "total_dataset_rows": 80,
            "missing_outcome_ratio": 0.25,
            "average_sample_age_days": 12.5,
            "unique_sectors": 6,
            "regimes_represented": ["bull", "bear", "sideways"],
            "feature_availability": {"volatility_score": 1.0},
            "date_coverage_start": "2026-07-16",
            "date_coverage_end": "2026-08-05",
        })
        _write_json(hist_dir / "ml_readiness.json", {
            "status": "BORDERLINE",
            "reasons": ["min_selections: required=500, actual=45"],
            "recommendation": "Continue accumulating data.",
            "thresholds": {"min_selections": {"required": 500, "actual": 45, "passed": False}},
        })

        import src.dashboard.combined as dash
        payload = dash._research_quality_payload()

        assert payload["available"] is True
        assert payload["quality"]["total_runs_recorded"] == 45
        assert payload["quality"]["unique_sectors"] == 6
        assert payload["ml_readiness"]["status"] == "BORDERLINE"
        assert len(payload["ml_readiness"]["reasons"]) == 1

    def test_missing_returns_unavailable(self, monkeypatch, tmp_path: Path):
        """No quality data → available=False."""
        base = _redirect_project_dir(monkeypatch, tmp_path)

        import src.dashboard.combined as dash
        payload = dash._research_quality_payload()

        assert payload["available"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. /api/status contains new keys
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiStatusNewKeys:
    def test_status_has_all_three_new_keys(self):
        """_api_status_payload must contain the 3 new research keys."""
        import src.dashboard.combined as dash
        payload = dash._api_status_payload()

        for key in ["research_walk_forward", "research_registry", "research_quality"]:
            assert key in payload, f"'{key}' missing from /api/status"
            assert isinstance(payload[key], dict), f"'{key}' must be dict"
            assert "available" in payload[key], f"'{key}' must have 'available'"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. /api/research/observability endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestObservabilityEndpoint:
    def test_endpoint_returns_200(self):
        """The observability endpoint must return ok=True."""
        import src.dashboard.combined as dash
        with dash.app.test_client() as client:
            resp = client.get("/api/research/observability")
            assert resp.status_code == 200
            data = resp.get_json()

        assert data["ok"] is True
        for key in ["analytics", "walk_forward", "registry", "quality"]:
            assert key in data, f"'{key}' missing from observability endpoint"
            assert "available" in data[key]

    def test_endpoint_has_timestamp(self):
        """Observability endpoint must include ISO timestamp."""
        import src.dashboard.combined as dash
        with dash.app.test_client() as client:
            resp = client.get("/api/research/observability")
            data = resp.get_json()

        assert "timestamp" in data
        assert "T" in data["timestamp"]


# ═══════════════════════════════════════════════════════════════════════════════
# 6. No computation during request
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoComputationDuringRequest:
    def test_no_backfill_import(self):
        """Dashboard combined.py must not import selection_backfill."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "selection_backfill" not in source

    def test_no_walk_forward_module_import(self):
        """Dashboard must not import walk_forward (recalculates)."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "from src.openalpha.walk_forward" not in source

    def test_no_research_analytics_module_import(self):
        """Dashboard must not import research_analytics (recalculates)."""
        import src.dashboard.combined as dash
        source = Path(dash.__file__).read_text()
        assert "from src.openalpha.research_analytics" not in source


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Existing keys unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestExistingKeysUnchanged:
    def test_all_pre_existing_keys_present(self):
        """All pre-4B keys must still be in _api_status_payload."""
        import src.dashboard.combined as dash
        payload = dash._api_status_payload()

        required = [
            "ok", "mode", "timestamp", "selection", "top_engines",
            "dashboard", "shadow", "candidate_validation",
            "candidate_model_evaluation", "research_status",
            "learning_summary", "market_regime", "regime_shadow",
            "universe", "governance", "research_report", "ai_selection",
            "research_analytics",  # Phase 3A — still present
        ]
        for key in required:
            assert key in payload, f"Pre-existing key '{key}' missing"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Backward compatibility
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackwardCompatible:
    def test_missing_walk_forward_does_not_break_status(self):
        """Missing walk-forward files must not break /api/status."""
        import src.dashboard.combined as dash
        payload = dash._api_status_payload()

        # All research payloads should return available=False gracefully
        wf = payload.get("research_walk_forward", {})
        assert isinstance(wf, dict)
        assert "available" in wf
        # Must not crash regardless of available=True or False

    def test_missing_registry_does_not_break_status(self):
        """Missing registry files must not break /api/status."""
        import src.dashboard.combined as dash
        payload = dash._api_status_payload()

        reg = payload.get("research_registry", {})
        assert isinstance(reg, dict)
        assert "available" in reg

    def test_missing_quality_does_not_break_status(self):
        """Missing quality files must not break /api/status."""
        import src.dashboard.combined as dash
        payload = dash._api_status_payload()

        qual = payload.get("research_quality", {})
        assert isinstance(qual, dict)
        assert "available" in qual
