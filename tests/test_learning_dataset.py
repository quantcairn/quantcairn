"""Tests for Learning Dataset Builder — Phase 2B.

Verifies:
  1. Ledger + outcome join produces correct feature/label rows
  2. Missing outcome → skipped (not crashed)
  3. Duplicate (run_id, symbol) prevention
  4. Dataset schema validation (features + labels present)
  5. Summary statistics computation
  6. Non-SUCCESS status records filtered out
  7. Missing return_21d_pct → filtered out
  8. Empty dataset produces valid empty summary
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.openalpha import learning_dataset as ld


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_roots_to(monkeypatch, base: Path) -> tuple[Path, Path, Path]:
    """Redirect LEDGER_ROOT, OUTCOMES_ROOT, DATASET_ROOT to temp dirs."""
    ledger = base / "selection_ledger"
    outcomes = base / "selection_outcomes"
    dataset = base / "dataset"
    monkeypatch.setattr(ld, "LEDGER_ROOT", ledger)
    monkeypatch.setattr(ld, "OUTCOMES_ROOT", outcomes)
    monkeypatch.setattr(ld, "DATASET_ROOT", dataset)
    return ledger, outcomes, dataset


def _write_json(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_selection(symbol: str, run_id: str = "r1", date: str = "2026-08-05",
                    sector: str = "Financial Services", **overrides) -> dict:
    """Build a realistic SelectionRecord dict."""
    rec = {
        "selection_run_id": run_id,
        "selection_date": date,
        "symbol": symbol,
        "formal_rank": 1,
        "score": 82.6,
        "base_score": 80.3,
        "sector": sector,
        "recommended_strategy": "range_swing",
        "data_source": "live",
        "run_mode": "FULL",
        "execution_mode": "RESEARCH",
        "candidate_type": "RESEARCH_ONLY",
        "entry_reference_price": 80.0,
        "range_low": 70.0,
        "range_high": 90.0,
        "range_width_pct": 12.5,
        "atr_pct": 2.2,
        "gap_rate": 0.004,
        "feature_volatility_score": 78.0,
        "feature_volume_score": 54.0,
        "feature_trend_score": 74.0,
        "feature_repeatability_score": 47.0,
        "feature_drawdown_safety_score": 76.0,
        "feature_liquidity_score": 50.0,
        "selector_version": "0.12.16",
        "scoring_logic_version": "sv1",
        "composition_logic_version": "cv1",
        "universe_version": "uv1",
        "recorded_at": "2026-08-05T00:00:00+00:00",
    }
    rec.update(overrides)
    return rec


def _make_outcome(symbol: str, run_id: str = "r1", date: str = "2026-08-05",
                  status: str = "SUCCESS", **overrides) -> dict:
    """Build a realistic OutcomeRecord dict with complete forward metrics."""
    rec = {
        "selection_run_id": run_id,
        "selection_date": date,
        "symbol": symbol,
        "formal_rank": 1,
        "entry_reference_price": 80.0,
        "entry_price_type": "selection_reference",
        "range_low": 70.0,
        "range_high": 90.0,
        "price_5d": 82.0,
        "price_21d": 86.0,
        "return_5d_pct": 2.5,
        "return_21d_pct": 7.5,
        "mfe_21d_pct": 12.0,
        "mae_21d_pct": -2.0,
        "bars_available": 22,
        "bars_required_5d": 5,
        "bars_required_21d": 21,
        "range_touch_support": True,
        "range_touch_resistance": True,
        "range_breakdown": False,
        "range_breakout": False,
        "range_outcome": "SUCCESS",
        "status": status,
        "status_detail": "",
        "data_provider": "yfinance",
        "evaluation_version": "backfill.v1",
        "backfilled_at": "2026-09-01T00:00:00+00:00",
        "content_hash": "abc123def456",
    }
    rec.update(overrides)
    return rec


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Ledger + Outcome Join
# ═══════════════════════════════════════════════════════════════════════════════

class TestJoin:
    def test_successful_join_produces_row(self, monkeypatch, tmp_path: Path):
        """A matched ledger + outcome pair must produce a valid dataset row."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        # Write selection
        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1", date="2026-08-05")],
        )
        # Write matching outcome
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("AIG", run_id="r1", date="2026-08-05", status="SUCCESS")],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 1
        assert summary["skipped_missing_outcome"] == 0

        # Verify the row content
        rows = ld.load_dataset()
        assert len(rows) == 1
        r = rows[0]
        # Features
        assert r["symbol"] == "AIG"
        assert r["volatility_score"] == 78.0
        assert r["volume_score"] == 54.0
        assert r["trend_score"] == 74.0
        assert r["repeatability_score"] == 47.0
        assert r["drawdown_score"] == 76.0
        assert r["liquidity_score"] == 50.0
        assert r["atr_pct"] == 2.2
        assert r["gap_rate"] == 0.004
        assert r["sector"] == "Financial Services"
        assert r["range_width_pct"] == 12.5
        # Labels
        assert r["return_5d_pct"] == 2.5
        assert r["return_21d_pct"] == 7.5
        assert r["mfe_21d_pct"] == 12.0
        assert r["mae_21d_pct"] == -2.0
        assert r["range_success"] is True
        assert r["range_breakout"] is False
        assert r["range_breakdown"] is False

    def test_multiple_symbols_in_same_run(self, monkeypatch, tmp_path: Path):
        """Multiple candidates in the same run must each produce a row."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [
                _make_selection("AIG", run_id="r1", sector="Financial Services"),
                _make_selection("BMY", run_id="r1", sector="Healthcare"),
                _make_selection("CCL", run_id="r1", sector="Consumer Discretionary"),
            ],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [
                _make_outcome("AIG", run_id="r1"),
                _make_outcome("BMY", run_id="r1"),
                _make_outcome("CCL", run_id="r1"),
            ],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 3

        rows = ld.load_dataset()
        symbols = {r["symbol"] for r in rows}
        assert symbols == {"AIG", "BMY", "CCL"}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Missing outcome handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingOutcome:
    def test_selection_without_outcome_is_skipped(self, monkeypatch, tmp_path: Path):
        """A selection with no matching outcome file must be counted as skipped."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1")],
        )
        # Do NOT write outcome file

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 0
        assert summary["skipped_missing_outcome"] == 1

    def test_selection_with_outcome_missing_symbol_is_skipped(self, monkeypatch, tmp_path: Path):
        """When outcome file exists but the specific symbol is missing → skipped."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1")],
        )
        # Outcome file exists but for a DIFFERENT symbol
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("OTHER", run_id="r1")],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 0
        assert summary["skipped_missing_outcome"] == 1

    def test_corrupt_outcome_file_is_handled(self, monkeypatch, tmp_path: Path):
        """A corrupt outcome JSON must not crash the build."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1")],
        )
        # Write invalid JSON
        outcome_file = outcomes_root / "2026-08-05" / "r1.json"
        outcome_file.parent.mkdir(parents=True, exist_ok=True)
        outcome_file.write_text("not valid json {{{", encoding="utf-8")

        summary = ld.build_learning_dataset()
        # Corrupt file → None from _read_json → treated like missing outcome
        assert summary["skipped_missing_outcome"] >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Duplicate prevention
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicatePrevention:
    def test_same_run_id_and_symbol_not_duplicated(self, monkeypatch, tmp_path: Path):
        """The same (run_id, symbol) pair must only appear once."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        # Write the SAME selection twice in the file (simulating a bug elsewhere)
        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [
                _make_selection("AIG", run_id="r1"),
                _make_selection("AIG", run_id="r1"),  # duplicate
            ],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("AIG", run_id="r1")],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 1
        assert summary["skipped_duplicate"] == 1

    def test_duplicate_across_different_runs_is_allowed(self, monkeypatch, tmp_path: Path):
        """Same symbol in different runs must each produce a row."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1", date="2026-08-05")],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("AIG", run_id="r1", date="2026-08-05")],
        )
        _write_json(
            ledger_root / "runs" / "2026-08-06" / "r2.json",
            [_make_selection("AIG", run_id="r2", date="2026-08-06")],
        )
        _write_json(
            outcomes_root / "2026-08-06" / "r2.json",
            [_make_outcome("AIG", run_id="r2", date="2026-08-06")],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 2
        assert summary["skipped_duplicate"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Dataset schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_all_feature_keys_present(self, monkeypatch, tmp_path: Path):
        """Every dataset row must contain all FEATURE_KEYS."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1")],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("AIG", run_id="r1")],
        )

        ld.build_learning_dataset()
        rows = ld.load_dataset()
        assert len(rows) == 1
        r = rows[0]

        for key in ld.FEATURE_KEYS:
            assert key in r, f"Feature key '{key}' missing from dataset row"

    def test_all_label_keys_present(self, monkeypatch, tmp_path: Path):
        """Every dataset row must contain all LABEL_KEYS."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1")],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("AIG", run_id="r1")],
        )

        ld.build_learning_dataset()
        rows = ld.load_dataset()
        r = rows[0]

        for key in ld.LABEL_KEYS:
            assert key in r, f"Label key '{key}' missing from dataset row"

    def test_score_fallback_when_feature_prefix_missing(self, monkeypatch, tmp_path: Path):
        """When feature_* keys are missing, fall back to unprefixed keys."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        # Use old-style keys without feature_ prefix
        sel = _make_selection("AIG", run_id="r1")
        del sel["feature_volatility_score"]
        del sel["feature_volume_score"]
        del sel["feature_trend_score"]
        sel["volatility_score"] = 70.0
        sel["volume_score"] = 50.0
        sel["trend_fit_score"] = 60.0

        _write_json(ledger_root / "runs" / "2026-08-05" / "r1.json", [sel])
        _write_json(outcomes_root / "2026-08-05" / "r1.json",
                   [_make_outcome("AIG", run_id="r1")])

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 1
        rows = ld.load_dataset()
        assert rows[0]["volatility_score"] == 70.0
        assert rows[0]["volume_score"] == 50.0
        assert rows[0]["trend_score"] == 60.0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Summary statistics
# ═══════════════════════════════════════════════════════════════════════════════

class TestSummaryStatistics:
    def test_summary_has_required_fields(self, monkeypatch, tmp_path: Path):
        """dataset_summary.json must contain all required fields."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("AIG", run_id="r1")],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("AIG", run_id="r1", return_21d_pct=7.5)],
        )

        ld.build_learning_dataset()
        summary = ld.load_summary()

        assert summary is not None
        assert summary["total_samples"] == 1
        assert "success_rate" in summary
        assert "average_return_5d" in summary
        assert "average_return_21d" in summary
        assert "average_mfe_21d" in summary
        assert "average_mae_21d" in summary
        assert "sector_distribution" in summary
        assert "feature_statistics" in summary
        assert "label_statistics" in summary
        assert summary["date_range"]["earliest"] == "2026-08-05"

    def test_sector_distribution_is_correct(self, monkeypatch, tmp_path: Path):
        """Sector counts must match input data."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [
                _make_selection("AIG", run_id="r1", sector="Financial Services"),
                _make_selection("BMY", run_id="r1", sector="Healthcare"),
                _make_selection("ACGL", run_id="r1", sector="Financial Services"),
            ],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [
                _make_outcome("AIG", run_id="r1"),
                _make_outcome("BMY", run_id="r1"),
                _make_outcome("ACGL", run_id="r1"),
            ],
        )

        ld.build_learning_dataset()
        summary = ld.load_summary()
        sectors = summary["sector_distribution"]
        assert sectors.get("Financial Services") == 2
        assert sectors.get("Healthcare") == 1

    def test_feature_statistics_have_mean_min_max(self, monkeypatch, tmp_path: Path):
        """Each numeric feature must have mean, min, max computed."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [
                _make_selection("AIG", run_id="r1",
                                feature_volatility_score=70.0,
                                feature_volume_score=50.0,
                                feature_trend_score=60.0),
                _make_selection("BMY", run_id="r1",
                                feature_volatility_score=90.0,
                                feature_volume_score=80.0,
                                feature_trend_score=40.0),
            ],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [
                _make_outcome("AIG", run_id="r1"),
                _make_outcome("BMY", run_id="r1"),
            ],
        )

        ld.build_learning_dataset()
        summary = ld.load_summary()
        fs = summary["feature_statistics"]

        vol_stats = fs.get("volatility_score", {})
        assert vol_stats["mean"] == 80.0  # (70+90)/2
        assert vol_stats["min"] == 70.0
        assert vol_stats["max"] == 90.0


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Non-SUCCESS status filtering
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatusFiltering:
    def test_failed_data_outcome_is_excluded(self, monkeypatch, tmp_path: Path):
        """OutcomeRecord with status=FAILED_DATA must be excluded."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [
                _make_selection("GOOD", run_id="r1"),
                _make_selection("BAD", run_id="r1"),
            ],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [
                _make_outcome("GOOD", run_id="r1", status="SUCCESS"),
                _make_outcome("BAD", run_id="r1", status="FAILED_DATA"),
            ],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 1
        assert summary["skipped_not_success"] == 1

    def test_insufficient_history_outcome_is_excluded(self, monkeypatch, tmp_path: Path):
        """OutcomeRecord with status=INSUFFICIENT_HISTORY must be excluded."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [_make_selection("SHORT", run_id="r1")],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [_make_outcome("SHORT", run_id="r1", status="INSUFFICIENT_HISTORY")],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 0
        assert summary["skipped_not_success"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Missing return_21d filtering
# ═══════════════════════════════════════════════════════════════════════════════

class TestReturn21Filtering:
    def test_missing_return_21d_is_excluded(self, monkeypatch, tmp_path: Path):
        """Outcome with status=SUCCESS but return_21d_pct=None must be excluded."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        _write_json(
            ledger_root / "runs" / "2026-08-05" / "r1.json",
            [
                _make_selection("HAS21", run_id="r1"),
                _make_selection("NO21", run_id="r1"),
            ],
        )
        _write_json(
            outcomes_root / "2026-08-05" / "r1.json",
            [
                _make_outcome("HAS21", run_id="r1", return_21d_pct=7.5),
                _make_outcome("NO21", run_id="r1", return_21d_pct=None),
            ],
        )

        summary = ld.build_learning_dataset()
        assert summary["total_samples"] == 1
        assert summary["skipped_no_21d"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Empty dataset
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyDataset:
    def test_empty_dataset_produces_valid_summary(self, monkeypatch, tmp_path: Path):
        """Empty dataset must produce a summary with zero counts, not crash."""
        ledger_root, outcomes_root, dataset_root = _redirect_roots_to(monkeypatch, tmp_path)

        # No data at all — no ledger runs, no outcomes
        summary = ld.build_learning_dataset()

        assert summary["total_samples"] == 0
        assert "success_rate" in summary
        assert "error" in summary  # error message about no data

    def test_empty_dataset_no_records_file(self, monkeypatch, tmp_path: Path):
        """When there are no rows, no records.jsonl should be created."""
        _redirect_roots_to(monkeypatch, tmp_path)

        ld.build_learning_dataset()
        rows = ld.load_dataset()
        assert rows == []

    def test_load_summary_returns_none_when_missing(self, monkeypatch, tmp_path: Path):
        """load_summary must return None when no summary file exists."""
        _redirect_roots_to(monkeypatch, tmp_path)

        summary = ld.load_summary()
        assert summary is None
