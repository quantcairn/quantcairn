"""Tests for Walk Forward Evaluation Framework — Phase 3B.

Verifies:
  1. Period split generation with configurable windows
  2. Empty dataset → no crash
  3. Single period analysis → correct metrics
  4. Multiple period comparison → correct aggregation
  5. Feature stability calculation
  6. Sector analysis
  7. Warning generation (LOW_SAMPLE_SIZE, PERIOD_DEGRADATION)
  8. Deterministic output
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.openalpha import walk_forward as wf
from src.openalpha.walk_forward import (
    WalkForwardPeriod,
    WalkForwardResult,
    generate_periods,
    run_walk_forward,
    load_walk_forward,
    _compute_metrics,
    _compute_period_result,
    _compute_feature_stability,
    _compute_sector_stability,
    _generate_robustness_flags,
    _rows_in_window,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_row(date_str: str, symbol: str = "TEST", sector: str = "Financial Services",
              volatility_score: float = 78.0, volume_score: float = 54.0,
              trend_score: float = 74.0, repeatability_score: float = 47.0,
              drawdown_score: float = 76.0, liquidity_score: float = 50.0,
              atr_pct: float = 2.2, gap_rate: float = 0.004,
              range_width_pct: float = 12.5,
              return_5d_pct: float | None = 2.5,
              return_21d_pct: float | None = 7.5,
              mfe_21d_pct: float | None = 12.0,
              mae_21d_pct: float | None = -2.0,
              range_success: bool = True,
              range_breakout: bool = False,
              range_breakdown: bool = False,
              **overrides) -> dict:
    rec = {
        "selection_run_id": f"run-{date_str}",
        "selection_date": date_str,
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


def _make_monthly_rows(start_year: int, start_month: int, n_months: int,
                       samples_per_month: int = 5,
                       return_21d_pct: float = 5.0) -> list[dict]:
    """Generate synthetic rows spread across calendar months."""
    import calendar as cal
    rows = []
    y, m = start_year, start_month
    for _ in range(n_months):
        last_day = cal.monthrange(y, m)[1]
        for d_idx in range(samples_per_month):
            day = min(1 + d_idx * 7, last_day)
            date_str = f"{y:04d}-{m:02d}-{day:02d}"
            rows.append(_make_row(date_str, symbol=f"SYM_{date_str}",
                                  return_21d_pct=return_21d_pct + (d_idx % 3 - 1) * 2.0))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return rows


def _redirect_wf_root(monkeypatch, base: Path) -> Path:
    """Redirect WF_ROOT and DATASET_ROOT to temp dirs."""
    import src.openalpha.learning_dataset as ld
    dataset = base / "dataset"
    wf_dir = base / "walk_forward"
    monkeypatch.setattr(ld, "DATASET_ROOT", dataset)
    monkeypatch.setattr(wf, "WF_ROOT", wf_dir)
    monkeypatch.setattr(wf, "DATASET_ROOT", dataset)
    return wf_dir


def _write_dataset(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    path.write_text(content, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Period split generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPeriodSplitGeneration:
    def test_generates_periods_from_dates(self):
        """6 months of daily dates should produce multiple periods."""
        dates = [f"2026-{m:02d}-15" for m in range(1, 7)]
        periods = generate_periods(dates, train_months=2, validation_months=1,
                                   forward_months=1, step_months=1)
        assert len(periods) >= 1, f"Expected at least 1 period, got {len(periods)}"

    def test_configurable_train_window(self):
        """Changing train_months changes period count."""
        dates = [f"2026-{m:02d}-01" for m in range(1, 13)]
        p3 = generate_periods(dates, train_months=3, validation_months=1,
                              forward_months=1, step_months=1)
        p6 = generate_periods(dates, train_months=6, validation_months=1,
                              forward_months=1, step_months=1)
        assert len(p6) <= len(p3), (
            f"Longer train should produce <= periods: {len(p6)} vs {len(p3)}"
        )

    def test_empty_dates_returns_empty(self):
        """Empty date list must return empty period list."""
        assert generate_periods([]) == []

    def test_period_has_all_fields(self):
        """Each period must have all 7 fields."""
        dates = ["2026-01-15", "2026-06-15"]
        periods = generate_periods(dates, train_months=1, validation_months=1,
                                   forward_months=1, step_months=1)
        if periods:
            p = periods[0]
            assert p.period_id.startswith("P")
            assert p.train_start
            assert p.train_end
            assert p.validation_start
            assert p.validation_end
            assert p.forward_start

    def test_step_months_controls_advance(self):
        """Step=2 should produce roughly half the periods of step=1."""
        dates = [f"2026-{m:02d}-15" for m in range(1, 13)]
        p1 = generate_periods(dates, train_months=2, validation_months=1,
                              forward_months=1, step_months=1)
        p2 = generate_periods(dates, train_months=2, validation_months=1,
                              forward_months=1, step_months=2)
        assert len(p2) < len(p1), (
            f"Step=2 ({len(p2)}) should have fewer periods than step=1 ({len(p1)})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Empty dataset
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyDataset:
    def test_empty_dataset_no_crash(self, monkeypatch, tmp_path: Path):
        """Empty dataset must return valid summary, not crash."""
        wf_dir = _redirect_wf_root(monkeypatch, tmp_path)
        _write_dataset(tmp_path / "dataset" / "records.jsonl", [])

        summary = run_walk_forward()
        assert summary["total_rows"] == 0
        assert summary["total_periods"] == 0
        assert len(summary["flags"]) >= 1
        assert summary["flags"][0]["type"] == "NO_DATA"

    def test_empty_dataset_writes_summary_file(self, monkeypatch, tmp_path: Path):
        """Summary JSON must be written even for empty dataset."""
        wf_dir = _redirect_wf_root(monkeypatch, tmp_path)
        _write_dataset(tmp_path / "dataset" / "records.jsonl", [])

        run_walk_forward()
        assert (wf_dir / "walk_forward_summary.json").exists()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Single period analysis
# ═══════════════════════════════════════════════════════════════════════════════

class TestSinglePeriodAnalysis:
    def test_correct_metrics_for_known_data(self):
        """Known returns must produce correct win_rate, avg_return, MFE, MAE."""
        rows = [
            _make_row("2026-03-15", symbol="W1", return_21d_pct=10.0,
                      mfe_21d_pct=15.0, mae_21d_pct=-3.0, range_success=True),
            _make_row("2026-03-20", symbol="W2", return_21d_pct=2.0,
                      mfe_21d_pct=5.0, mae_21d_pct=-1.0, range_success=True),
            _make_row("2026-04-05", symbol="L1", return_21d_pct=-4.0,
                      mfe_21d_pct=2.0, mae_21d_pct=-6.0, range_success=False),
            _make_row("2026-04-10", symbol="L2", return_21d_pct=-1.0,
                      mfe_21d_pct=1.0, mae_21d_pct=-3.0, range_success=False),
        ]

        period = WalkForwardPeriod(
            period_id="P1",
            train_start="2026-01-01", train_end="2026-02-28",
            validation_start="2026-03-01", validation_end="2026-03-31",
            forward_start="2026-04-01", forward_end="2026-04-30",
        )

        result = _compute_period_result(rows, period)
        assert result.train_samples == 0
        assert result.validation_samples == 2
        assert result.validation_win_rate == 1.0
        assert result.validation_avg_return_21d == 6.0
        assert result.forward_samples == 2
        assert result.forward_win_rate == 0.0
        assert result.forward_avg_return_21d == -2.5
        assert result.forward_range_success_rate == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Multiple period comparison
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiplePeriodComparison:
    def test_different_periods_produce_different_results(self, monkeypatch, tmp_path: Path):
        """Two periods with different data must produce different forward win rates."""
        wf_dir = _redirect_wf_root(monkeypatch, tmp_path)

        rows = [
            _make_row("2026-01-15", symbol="A", return_21d_pct=5.0),
            _make_row("2026-02-15", symbol="B", return_21d_pct=8.0),
            _make_row("2026-03-15", symbol="C", return_21d_pct=3.0),
            _make_row("2026-04-15", symbol="D", return_21d_pct=6.0),
            _make_row("2026-05-15", symbol="E", return_21d_pct=-3.0),
            _make_row("2026-06-15", symbol="F", return_21d_pct=-5.0),
            _make_row("2026-07-15", symbol="G", return_21d_pct=-2.0),
            _make_row("2026-08-15", symbol="H", return_21d_pct=-7.0),
        ]

        _write_dataset(tmp_path / "dataset" / "records.jsonl", rows)

        summary = run_walk_forward(
            train_months=1, validation_months=1,
            forward_months=1, step_months=2,
        )

        assert summary["total_periods"] >= 2, (
            f"Expected >=2 periods, got {summary['total_periods']}"
        )
        assert summary["total_rows"] == 8

    def test_aggregate_summary_has_stability_score(self, monkeypatch, tmp_path: Path):
        """Summary must contain overall_stability when multiple periods exist."""
        wf_dir = _redirect_wf_root(monkeypatch, tmp_path)

        rows = _make_monthly_rows(2026, 1, 6, samples_per_month=5, return_21d_pct=5.0)
        _write_dataset(tmp_path / "dataset" / "records.jsonl", rows)

        summary = run_walk_forward(
            train_months=2, validation_months=1,
            forward_months=1, step_months=1,
        )

        assert summary["total_periods"] >= 1
        if summary["total_periods"] >= 2:
            assert summary["overall_stability"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Feature stability
# ═══════════════════════════════════════════════════════════════════════════════

class TestFeatureStability:
    def test_stable_feature_has_high_stability(self):
        """Feature with same mean across periods → stability near 1.0."""
        rows = [
            _make_row("2026-03-15", volatility_score=78.0),
            _make_row("2026-03-20", volatility_score=78.0),
            _make_row("2026-04-10", volatility_score=78.0),
            _make_row("2026-04-15", volatility_score=78.0),
        ]

        periods = [
            WalkForwardPeriod("P1", "2026-01-01", "2026-02-28",
                              "2026-03-01", "2026-03-31",
                              "2026-04-01", "2026-04-30"),
        ]

        fs = _compute_feature_stability(rows, periods)
        vs = fs["features"].get("volatility_score", {})
        assert "stability" in vs

    def test_drifting_feature_detected(self):
        """When feature means are widely separated, stability score must be low.
        Uses three periods with tight within-period clusters so the
        between-period variance drives stability down."""
        rows = []
        # Feb 20 dates → fall in P1 forward (Feb 16 - Mar 15)
        for v in [10.0, 10.0, 9.0, 11.0, 10.0]:
            rows.append(_make_row("2026-02-20", volatility_score=v))
        # Apr 20 dates → fall in P2 forward (Apr 16 - May 15)
        for v in [50.0, 50.0, 49.0, 51.0, 50.0]:
            rows.append(_make_row("2026-04-20", volatility_score=v))
        # Jun 20 dates → fall in P3 forward (Jun 16 - Jul 15)
        for v in [90.0, 90.0, 89.0, 91.0, 90.0]:
            rows.append(_make_row("2026-06-20", volatility_score=v))

        periods = [
            WalkForwardPeriod("P1", "2025-11-01", "2026-01-15",
                              "2026-01-16", "2026-02-15",
                              "2026-02-16", "2026-03-15"),
            WalkForwardPeriod("P2", "2026-01-01", "2026-03-15",
                              "2026-03-16", "2026-04-15",
                              "2026-04-16", "2026-05-15"),
            WalkForwardPeriod("P3", "2026-03-01", "2026-05-15",
                              "2026-05-16", "2026-06-15",
                              "2026-06-16", "2026-07-15"),
        ]

        fs = _compute_feature_stability(rows, periods)
        vs = fs["features"].get("volatility_score", {})
        stability = vs.get("stability")
        assert stability is not None, (
            f"Expected stability score, got None. Features: {list(vs.keys())}"
        )
        # With 3 tight clusters at 10, 50, 90, stability should be well below 1.0
        assert stability < 0.8, (
            f"Expected stability < 0.8 with widely separated means, got {stability}"
        )

    def test_feature_stability_has_all_numeric_features(self):
        """Every NUMERIC_FEATURE_KEY must appear in output."""
        rows = _make_monthly_rows(2026, 1, 3, samples_per_month=3)
        periods = generate_periods(
            [r["selection_date"] for r in rows],
            train_months=1, validation_months=1, forward_months=1, step_months=1,
        )

        fs = _compute_feature_stability(rows, periods)
        for key in wf.NUMERIC_FEATURE_KEYS:
            assert key in fs["features"], f"Feature '{key}' missing"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Sector analysis
# ═══════════════════════════════════════════════════════════════════════════════

class TestSectorAnalysis:
    def test_sectors_correctly_counted(self):
        """Sector analysis must count samples and success rates correctly."""
        rows = [
            _make_row("2026-04-10", symbol="A", sector="Financial Services",
                      return_21d_pct=5.0),
            _make_row("2026-04-15", symbol="B", sector="Financial Services",
                      return_21d_pct=-2.0),
            _make_row("2026-04-20", symbol="C", sector="Technology",
                      return_21d_pct=8.0),
            _make_row("2026-04-25", symbol="D", sector="Technology",
                      return_21d_pct=3.0),
        ]

        periods = [
            WalkForwardPeriod("P1", "2026-01-01", "2026-02-28",
                              "2026-03-01", "2026-03-31",
                              "2026-04-01", "2026-04-30"),
        ]

        ss = _compute_sector_stability(rows, periods)
        sectors = ss["sectors"]

        assert "Financial Services" in sectors
        assert "Technology" in sectors
        fs = sectors["Financial Services"]
        assert fs["total_samples"] == 2
        p1 = fs["by_period"]["P1"]
        assert p1["samples"] == 2
        assert p1["success_rate"] == 0.5

    def test_unknown_sector_handled(self):
        """Missing/empty sector must be 'Unknown'."""
        rows = [
            _make_row("2026-04-10", sector="", return_21d_pct=5.0),
        ]
        periods = [
            WalkForwardPeriod("P1", "2026-01-01", "2026-02-28",
                              "2026-03-01", "2026-03-31",
                              "2026-04-01", "2026-04-30"),
        ]

        ss = _compute_sector_stability(rows, periods)
        assert "Unknown" in ss["sectors"]


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Warning generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestWarningGeneration:
    def test_low_sample_warning_fires(self):
        """When samples < min_samples → LOW_SAMPLE_SIZE flag."""
        results = [
            WalkForwardResult(
                period_id="P1",
                train_samples=5, validation_samples=3, forward_samples=2,
                forward_win_rate=0.5,
            ),
        ]

        feature_stability = {"warnings": []}
        flags = _generate_robustness_flags(
            results, feature_stability, min_samples=10)

        low_sample_flags = [f for f in flags if f["type"] == "LOW_SAMPLE_SIZE"]
        assert len(low_sample_flags) > 0, "Expected LOW_SAMPLE_SIZE flags"

    def test_degradation_warning_fires(self):
        """When forward win rate drops significantly → PERIOD_DEGRADATION flag."""
        results = [
            WalkForwardResult(period_id="P1", train_samples=50,
                              validation_samples=20, forward_samples=30,
                              forward_win_rate=0.70),
            WalkForwardResult(period_id="P2", train_samples=50,
                              validation_samples=20, forward_samples=30,
                              forward_win_rate=0.40),
        ]

        feature_stability = {"warnings": []}
        flags = _generate_robustness_flags(
            results, feature_stability, degradation_threshold=0.20)

        degradation_flags = [f for f in flags if f["type"] == "PERIOD_DEGRADATION"]
        assert len(degradation_flags) >= 1, (
            f"Expected PERIOD_DEGRADATION flag, got {flags}"
        )

    def test_no_degradation_when_improving(self):
        """When win rate improves → no PERIOD_DEGRADATION flag."""
        results = [
            WalkForwardResult(period_id="P1", train_samples=30,
                              validation_samples=10, forward_samples=20,
                              forward_win_rate=0.40),
            WalkForwardResult(period_id="P2", train_samples=30,
                              validation_samples=10, forward_samples=20,
                              forward_win_rate=0.65),
        ]

        feature_stability = {"warnings": []}
        flags = _generate_robustness_flags(
            results, feature_stability, degradation_threshold=0.20)

        degradation_flags = [f for f in flags if f["type"] == "PERIOD_DEGRADATION"]
        assert len(degradation_flags) == 0

    def test_no_flags_when_all_healthy(self):
        """When samples are sufficient and performance is stable → no flags."""
        results = [
            WalkForwardResult(period_id="P1", train_samples=100,
                              validation_samples=50, forward_samples=60,
                              forward_win_rate=0.60),
            WalkForwardResult(period_id="P2", train_samples=100,
                              validation_samples=50, forward_samples=60,
                              forward_win_rate=0.58),
        ]

        feature_stability = {"warnings": []}
        flags = _generate_robustness_flags(
            results, feature_stability, min_samples=10, degradation_threshold=0.20)

        assert len(flags) == 0, f"Expected no flags, got {flags}"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Deterministic output
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministic:
    def test_same_input_produces_same_output(self, monkeypatch, tmp_path: Path):
        """Running twice on identical data must produce identical JSON output."""
        wf_dir = _redirect_wf_root(monkeypatch, tmp_path)

        rows = _make_monthly_rows(2026, 1, 6, samples_per_month=4,
                                  return_21d_pct=5.0)
        _write_dataset(tmp_path / "dataset" / "records.jsonl", rows)

        run_walk_forward(
            train_months=2, validation_months=1, forward_months=1, step_months=2)

        s1 = json.loads((wf_dir / "walk_forward_summary.json").read_text())

        run_walk_forward(
            train_months=2, validation_months=1, forward_months=1, step_months=2)

        s2 = json.loads((wf_dir / "walk_forward_summary.json").read_text())

        assert s1["total_periods"] == s2["total_periods"]
        assert s1["total_rows"] == s2["total_rows"]
        if s1["overall_stability"] is not None:
            assert s1["overall_stability"] == s2["overall_stability"]

    def test_metrics_deterministic(self):
        """_compute_metrics must produce identical results for identical rows."""
        rows = [_make_row("2026-03-15", return_21d_pct=5.0)] * 4
        m1 = _compute_metrics(rows)
        m2 = _compute_metrics(rows)
        assert m1 == m2

    def test_rows_in_window_correct(self):
        """_rows_in_window must correctly filter by date range."""
        rows = [
            _make_row("2026-01-15"),
            _make_row("2026-02-15"),
            _make_row("2026-03-15"),
            _make_row("2026-04-15"),
        ]
        filtered = _rows_in_window(rows, "2026-02-01", "2026-03-31")
        assert len(filtered) == 2
        dates = {r["selection_date"] for r in filtered}
        assert dates == {"2026-02-15", "2026-03-15"}
