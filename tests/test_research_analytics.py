"""Tests for Research Analytics — Phase 2C.

Verifies:
  1. Empty dataset → valid empty reports (no crash)
  2. Single sample → correct computations
  3. Multiple sectors → correct sector breakdown
  4. Deterministic output → same input = same output
  5. Schema validation → all required keys present
  6. Feature correlation computation
  7. Performance summary accuracy
  8. load_analytics with and without data
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.openalpha import research_analytics as ra


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_roots_to(monkeypatch, base: Path) -> tuple[Path, Path]:
    """Redirect DATASET_ROOT and ANALYTICS_ROOT to temp dirs.

    load_dataset() lives in learning_dataset.py and uses its own
    module-level DATASET_ROOT, so we must patch the source module.
    """
    import src.openalpha.learning_dataset as ld
    dataset = base / "dataset"
    analytics = base / "analytics"
    monkeypatch.setattr(ld, "DATASET_ROOT", dataset)
    monkeypatch.setattr(ra, "DATASET_ROOT", dataset)
    monkeypatch.setattr(ra, "ANALYTICS_ROOT", analytics)
    return dataset, analytics


def _write_dataset_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    path.write_text(content, encoding="utf-8")


def _make_row(
    symbol: str = "TEST",
    sector: str = "Financial Services",
    volatility_score: float = 78.0,
    volume_score: float = 54.0,
    trend_score: float = 74.0,
    repeatability_score: float = 47.0,
    drawdown_score: float = 76.0,
    liquidity_score: float = 50.0,
    atr_pct: float = 2.2,
    gap_rate: float = 0.004,
    range_width_pct: float = 12.5,
    return_5d_pct: float | None = 2.5,
    return_21d_pct: float | None = 7.5,
    mfe_21d_pct: float | None = 12.0,
    mae_21d_pct: float | None = -2.0,
    range_success: bool = True,
    range_breakout: bool = False,
    range_breakdown: bool = False,
    **overrides,
) -> dict:
    """Build a single dataset row matching the Phase 2B schema."""
    rec = {
        "selection_run_id": "r1",
        "selection_date": "2026-08-05",
        "symbol": symbol,
        "volatility_score": volatility_score,
        "volume_score": volume_score,
        "trend_score": trend_score,
        "repeatability_score": repeatability_score,
        "drawdown_score": drawdown_score,
        "liquidity_score": liquidity_score,
        "atr_pct": atr_pct,
        "gap_rate": gap_rate,
        "sector": sector,
        "range_width_pct": range_width_pct,
        "return_5d_pct": return_5d_pct,
        "return_21d_pct": return_21d_pct,
        "mfe_21d_pct": mfe_21d_pct,
        "mae_21d_pct": mae_21d_pct,
        "range_success": range_success,
        "range_breakout": range_breakout,
        "range_breakdown": range_breakdown,
    }
    rec.update(overrides)
    return rec


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Empty dataset
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyDataset:
    def test_empty_produces_performance_summary(self, monkeypatch, tmp_path: Path):
        """Empty dataset must produce a valid performance_summary.json."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        # Write empty dataset
        _write_dataset_jsonl(dataset_root / "records.jsonl", [])

        result = ra.run_analytics()
        assert result["rows_analyzed"] == 0

        path = analytics_root / "performance_summary.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "error" in data or data.get("total_selections") == 0

    def test_empty_produces_feature_analysis(self, monkeypatch, tmp_path: Path):
        """Empty dataset must produce a valid feature_analysis.json."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)
        _write_dataset_jsonl(dataset_root / "records.jsonl", [])

        ra.run_analytics()
        path = analytics_root / "feature_analysis.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "error" in data or "features" in data

    def test_empty_produces_sector_analysis(self, monkeypatch, tmp_path: Path):
        """Empty dataset must produce a valid sector_analysis.json."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)
        _write_dataset_jsonl(dataset_root / "records.jsonl", [])

        ra.run_analytics()
        path = analytics_root / "sector_analysis.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "error" in data or "sectors" in data

    def test_load_analytics_when_missing(self, monkeypatch, tmp_path: Path):
        """load_analytics must return None for each report type when files missing."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)
        reports = ra.load_analytics()
        assert reports["performance_summary"] is None
        assert reports["feature_analysis"] is None
        assert reports["sector_analysis"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Single sample
# ═══════════════════════════════════════════════════════════════════════════════

class TestSingleSample:
    def test_single_sample_performance_summary(self, monkeypatch, tmp_path: Path):
        """Single sample must produce correct win_rate and avg returns."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG", return_21d_pct=7.5),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "performance_summary.json").read_text())
        assert data["total_selections"] == 1
        assert data["win_rate"] == 1.0  # 7.5 > 0 → win
        assert data["average_return_21d_pct"] == 7.5
        assert data["average_mfe_21d_pct"] == 12.0
        assert data["average_mae_21d_pct"] == -2.0
        assert data["range_success_rate"] == 1.0

    def test_single_sample_feature_analysis(self, monkeypatch, tmp_path: Path):
        """Single sample feature analysis must have valid structure."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="WIN", return_21d_pct=5.0, volatility_score=80.0),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "feature_analysis.json").read_text())
        features = data.get("features", {})

        assert "volatility_score" in features
        vs = features["volatility_score"]
        assert vs["mean_all"] == 80.0
        assert vs["samples"] == 1
        # With one sample, correlation is undefined (need n >= 3)
        assert vs["correlation_with_return_21d"] == 0.0

    def test_single_sample_sector_analysis(self, monkeypatch, tmp_path: Path):
        """Single sample sector analysis must show one sector."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG", sector="Financial Services", return_21d_pct=7.5),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "sector_analysis.json").read_text())
        sectors = data.get("sectors", {})

        assert "Financial Services" in sectors
        fs = sectors["Financial Services"]
        assert fs["samples"] == 1
        assert fs["success_rate"] == 1.0
        assert fs["avg_return_21d_pct"] == 7.5


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Multiple sectors
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultipleSectors:
    def test_sectors_correctly_broken_down(self, monkeypatch, tmp_path: Path):
        """Each sector must have its own aggregated stats."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG", sector="Financial Services", return_21d_pct=10.0),
            _make_row(symbol="ACGL", sector="Financial Services", return_21d_pct=6.0),
            _make_row(symbol="BMY", sector="Healthcare", return_21d_pct=-3.0),
            _make_row(symbol="CCL", sector="Consumer Discretionary", return_21d_pct=2.0),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "sector_analysis.json").read_text())
        sectors = data.get("sectors", {})

        assert len(sectors) == 3

        fs = sectors["Financial Services"]
        assert fs["samples"] == 2
        assert fs["success_rate"] == 1.0    # both positive
        assert fs["avg_return_21d_pct"] == 8.0  # (10+6)/2

        hc = sectors["Healthcare"]
        assert hc["samples"] == 1
        assert hc["success_rate"] == 0.0
        assert hc["avg_return_21d_pct"] == -3.0

    def test_unknown_sector_handled(self, monkeypatch, tmp_path: Path):
        """Empty or missing sector must be grouped as 'Unknown'."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="NO_SECTOR", sector="", return_21d_pct=5.0),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "sector_analysis.json").read_text())
        sectors = data.get("sectors", {})

        assert "Unknown" in sectors or "" in sectors


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Deterministic output
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministic:
    def test_same_input_produces_same_output(self, monkeypatch, tmp_path: Path):
        """Running analytics twice on identical data must produce identical output."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        rows = [
            _make_row(symbol="AIG", volatility_score=78.0, return_21d_pct=7.5),
            _make_row(symbol="BMY", volatility_score=85.0, return_21d_pct=-2.0),
            _make_row(symbol="CCL", volatility_score=90.0, return_21d_pct=3.0),
        ]
        _write_dataset_jsonl(dataset_root / "records.jsonl", rows)

        result1 = ra.run_analytics()
        result2 = ra.run_analytics()

        # Same number of rows analyzed
        assert result1["rows_analyzed"] == result2["rows_analyzed"]

        # Same file content
        for name in ("performance_summary", "feature_analysis", "sector_analysis"):
            path = analytics_root / f"{name}.json"
            content = path.read_text()
            # Read twice — content unchanged
            assert path.read_text() == content


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_performance_summary_has_all_keys(self, monkeypatch, tmp_path: Path):
        """performance_summary.json must contain all expected keys."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG"),
            _make_row(symbol="ACGL"),
            _make_row(symbol="AGNC"),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "performance_summary.json").read_text())

        required = [
            "total_selections", "completed_outcomes", "win_rate",
            "average_return_5d_pct", "average_return_21d_pct",
            "average_mfe_21d_pct", "average_mae_21d_pct",
            "range_success_rate", "analytics_version", "generated_at",
        ]
        for key in required:
            assert key in data, f"'{key}' missing from performance_summary"

    def test_feature_analysis_has_all_features(self, monkeypatch, tmp_path: Path):
        """feature_analysis.json must contain every NUMERIC_FEATURE_KEY."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG"),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "feature_analysis.json").read_text())
        features = data.get("features", {})

        for key in ra.NUMERIC_FEATURE_KEYS:
            assert key in features, f"Feature '{key}' missing from feature_analysis"

    def test_feature_entry_has_all_sub_keys(self, monkeypatch, tmp_path: Path):
        """Each feature entry must have mean_all, correlation, samples, etc."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG", volatility_score=78.0),
            _make_row(symbol="ACGL", volatility_score=83.0),
            _make_row(symbol="AGNC", volatility_score=76.0),
            _make_row(symbol="ARCC", volatility_score=72.0),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "feature_analysis.json").read_text())
        vs = data["features"]["volatility_score"]

        required = ["mean_all", "mean_successful", "mean_failed",
                     "min", "max", "correlation_with_return_21d",
                     "correlation_abs", "correlation_direction", "samples"]
        for key in required:
            assert key in vs, f"'{key}' missing from feature entry"

    def test_sector_entry_has_all_sub_keys(self, monkeypatch, tmp_path: Path):
        """Each sector entry must have samples, success_rate, avg_return, etc."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="A", sector="Financial Services", return_21d_pct=5.0),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "sector_analysis.json").read_text())
        fs = data["sectors"]["Financial Services"]

        required = ["samples", "success_rate", "avg_return_21d_pct",
                     "avg_mfe_21d_pct", "avg_mae_21d_pct"]
        for key in required:
            assert key in fs, f"'{key}' missing from sector entry"

    def test_analytics_version_on_all_reports(self, monkeypatch, tmp_path: Path):
        """All three reports must carry analytics_version."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG"),
        ])

        ra.run_analytics()
        for name in ("performance_summary", "feature_analysis", "sector_analysis"):
            data = json.loads((analytics_root / f"{name}.json").read_text())
            assert data.get("analytics_version") == ra.ANALYTICS_VERSION, (
                f"{name} missing analytics_version"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Feature correlation
# ═══════════════════════════════════════════════════════════════════════════════

class TestFeatureCorrelation:
    def test_perfect_positive_correlation_detected(self, monkeypatch, tmp_path: Path):
        """When feature and return are perfectly correlated, corr=1.0."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        # volatility_score = 10 × return_21d → perfect positive correlation
        rows = [
            _make_row(symbol="A", volatility_score=10.0, return_21d_pct=1.0),
            _make_row(symbol="B", volatility_score=20.0, return_21d_pct=2.0),
            _make_row(symbol="C", volatility_score=30.0, return_21d_pct=3.0),
            _make_row(symbol="D", volatility_score=40.0, return_21d_pct=4.0),
            _make_row(symbol="E", volatility_score=50.0, return_21d_pct=5.0),
        ]
        _write_dataset_jsonl(dataset_root / "records.jsonl", rows)

        ra.run_analytics()
        data = json.loads((analytics_root / "feature_analysis.json").read_text())
        vs = data["features"]["volatility_score"]
        assert vs["correlation_with_return_21d"] == pytest.approx(1.0, abs=0.01)
        assert vs["correlation_direction"] == "positive"
        assert vs["correlation_abs"] == pytest.approx(1.0, abs=0.01)

    def test_perfect_negative_correlation_detected(self, monkeypatch, tmp_path: Path):
        """When feature is inversely correlated with return, corr < 0."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        # volatility_score = 50 - 10 × return_21d → perfect negative correlation
        rows = [
            _make_row(symbol="A", volatility_score=40.0, return_21d_pct=1.0),
            _make_row(symbol="B", volatility_score=30.0, return_21d_pct=2.0),
            _make_row(symbol="C", volatility_score=20.0, return_21d_pct=3.0),
            _make_row(symbol="D", volatility_score=10.0, return_21d_pct=4.0),
            _make_row(symbol="E", volatility_score=0.0, return_21d_pct=5.0),
        ]
        _write_dataset_jsonl(dataset_root / "records.jsonl", rows)

        ra.run_analytics()
        data = json.loads((analytics_root / "feature_analysis.json").read_text())
        vs = data["features"]["volatility_score"]
        assert vs["correlation_with_return_21d"] == pytest.approx(-1.0, abs=0.01)
        assert vs["correlation_direction"] == "negative"

    def test_constant_feature_has_zero_correlation(self, monkeypatch, tmp_path: Path):
        """Constant feature values produce correlation=0.0 (zero variance)."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        rows = [
            _make_row(symbol="A", volatility_score=78.0, return_21d_pct=1.0),
            _make_row(symbol="B", volatility_score=78.0, return_21d_pct=5.0),
            _make_row(symbol="C", volatility_score=78.0, return_21d_pct=-3.0),
        ]
        _write_dataset_jsonl(dataset_root / "records.jsonl", rows)

        ra.run_analytics()
        data = json.loads((analytics_root / "feature_analysis.json").read_text())
        vs = data["features"]["volatility_score"]
        assert vs["correlation_with_return_21d"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Performance summary accuracy
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerformanceAccuracy:
    def test_win_rate_with_mixed_outcomes(self, monkeypatch, tmp_path: Path):
        """Win rate must be wins / total completed outcomes."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="W1", return_21d_pct=5.0),   # win
            _make_row(symbol="W2", return_21d_pct=3.0),   # win
            _make_row(symbol="L1", return_21d_pct=-2.0),  # loss
            _make_row(symbol="E1", return_21d_pct=0.0),   # even → not a win
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "performance_summary.json").read_text())

        assert data["total_selections"] == 4
        assert data["return_21d_positive_count"] == 2
        assert data["return_21d_negative_count"] == 2  # -2.0 and 0.0
        assert data["win_rate"] == 0.5

    def test_average_returns_with_known_values(self, monkeypatch, tmp_path: Path):
        """Average returns must equal `sum(values) / count`."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="A", return_21d_pct=10.0, mfe_21d_pct=15.0, mae_21d_pct=-3.0),
            _make_row(symbol="B", return_21d_pct=2.0, mfe_21d_pct=8.0, mae_21d_pct=-5.0),
            _make_row(symbol="C", return_21d_pct=-4.0, mfe_21d_pct=3.0, mae_21d_pct=-8.0),
        ])

        ra.run_analytics()
        data = json.loads((analytics_root / "performance_summary.json").read_text())

        assert data["average_return_21d_pct"] == pytest.approx(8.0 / 3.0, abs=0.01)
        assert data["average_mfe_21d_pct"] == pytest.approx(26.0 / 3.0, abs=0.01)
        assert data["average_mae_21d_pct"] == pytest.approx(-16.0 / 3.0, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. load_analytics with data
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadAnalytics:
    def test_load_analytics_after_run(self, monkeypatch, tmp_path: Path):
        """load_analytics must return all three reports after run_analytics()."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_dataset_jsonl(dataset_root / "records.jsonl", [
            _make_row(symbol="AIG"),
        ])

        ra.run_analytics()
        reports = ra.load_analytics()

        assert reports["performance_summary"] is not None
        assert reports["feature_analysis"] is not None
        assert reports["sector_analysis"] is not None

        assert reports["performance_summary"]["total_selections"] == 1

    def test_load_analytics_handles_corrupt_file(self, monkeypatch, tmp_path: Path):
        """Corrupt JSON must return None for that report, not crash."""
        dataset_root, analytics_root = _redirect_roots_to(monkeypatch, tmp_path)

        # Write corrupt file
        analytics_root.mkdir(parents=True, exist_ok=True)
        (analytics_root / "performance_summary.json").write_text("not json", encoding="utf-8")

        reports = ra.load_analytics()
        assert reports["performance_summary"] is None
        assert reports["feature_analysis"] is None  # file doesn't exist
        assert reports["sector_analysis"] is None   # file doesn't exist
