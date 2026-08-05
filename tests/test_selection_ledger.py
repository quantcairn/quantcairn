"""Tests for Selection Performance Ledger.

Verifies:
  1. Write / read round-trip
  2. Schema alignment with outcome collector
  3. Empty formal candidates → no-op
  4. Index update after write
  5. Atomic write (no partial files)
  6. Version provenance fields
  7. Failure isolation (doesn't crash on bad inputs)
  8. Immutable snapshot behaviour (no overwrite of existing records)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.openalpha import selection_ledger as ledger_mod


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_topk_item(ticker: str, rank: int) -> dict:
    """Build a realistic topk candidate dict matching run_selection output."""
    return {
        "ticker": ticker,
        "score": 82.6 - (rank - 1) * 2.0,
        "base_score": 80.3 - (rank - 1) * 2.0,
        "sector": "Financial Services",
        "recommended_strategy": "range_swing",
        "data_source": "live",
        "candidate_type": "RESEARCH_ONLY",
        "range_low": 70.0 + rank,
        "range_high": 90.0 + rank,
        "volatility_score": 78.0,
        "volume_score": 54.0,
        "trend_fit_score": 74.0,
        "repeatability_score": 47.0,
        "drawdown_safety_score": 76.0,
        "liquidity_score": 180_000_000.0,
        "metrics": {
            "price_midpoint": 80.0 + rank,
            "last_close": 79.5 + rank,
            "range_width_pct": 12.5,
            "atr_pct": 2.2,
            "gap_rate": 0.004,
        },
        "risk": {"stop_loss_pct": 1.5},
    }


def _redirect_ledger_to(monkeypatch, base: Path) -> tuple[Path, Path, Path]:
    """Redirect the ledger module's root / runs / index paths to *base*."""
    root = base / "selection_ledger"
    runs = root / "runs"
    index = root / "ledger_index.json"
    monkeypatch.setattr(ledger_mod, "LEDGER_ROOT", root, raising=False)
    monkeypatch.setattr(ledger_mod, "RUNS_DIR", runs, raising=False)
    monkeypatch.setattr(ledger_mod, "INDEX_PATH", index, raising=False)
    return root, runs, index


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Round-trip
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundTrip:
    def test_write_and_read(self, monkeypatch, tmp_path: Path):
        """Write 5 records, read them back, verify all fields intact."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item(t, i) for i, t in enumerate(["AIG", "ACGL", "AGNC", "ARCC", "ALLY"], 1)]
        formal = ["AIG", "ACGL", "AGNC", "ARCC", "ALLY"]

        result_path = ledger_mod.write_selection_snapshot(
            run_id="test-run-001",
            date="2026-08-05",
            topk=topk,
            formal_candidates=formal,
            run_mode="FULL",
            execution_mode="RESEARCH",
            candidate_type="RESEARCH_ONLY",
        )
        assert result_path is not None
        assert result_path.exists()

        records = ledger_mod.load_selection_history(
            since_date="2026-08-05", until_date="2026-08-05")
        assert len(records) == 5

        # Verify ordering: rank 1 first
        assert records[0].symbol == "AIG"
        assert records[0].formal_rank == 1
        assert records[4].symbol == "ALLY"
        assert records[4].formal_rank == 5

        # Verify field types and values
        r = records[0]
        assert r.score == 82.6
        assert r.base_score == 80.3
        assert isinstance(r.score, float)
        assert r.sector == "Financial Services"
        assert r.run_mode == "FULL"
        assert r.execution_mode == "RESEARCH"
        assert r.feature_volatility_score == 78.0
        assert r.feature_volume_score == 54.0
        assert r.feature_trend_score == 74.0
        assert r.range_low == 71.0
        assert r.atr_pct == 2.2
        assert r.gap_rate == 0.004

        # Forward-looking fields are None initially
        assert r.price_5d is None
        assert r.price_21d is None
        assert r.return_5d_pct is None
        assert r.return_21d_pct is None
        assert r.mfe_21d_pct is None
        assert r.mae_21d_pct is None
        assert r.range_success is None

        # Meta
        assert r.recorded_at != ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Schema alignment with outcome collector
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaAlignment:
    def test_feature_fields_align_with_outcome_collector(self):
        """Feature_* field names shared with outcome collector must match."""
        ledger_fields = {
            f.name for f in
            ledger_mod.SelectionRecord.__dataclass_fields__.values()  # type: ignore[attr-defined]
        }
        from src.outcome.collector import _OUTCOME_COLUMNS_V3
        outcome_fields = set(_OUTCOME_COLUMNS_V3)

        feature_prefix = "feature_"
        ledger_features = {f for f in ledger_fields if f.startswith(feature_prefix)}
        outcome_features = {f for f in outcome_fields if f.startswith(feature_prefix)}

        shared = ledger_features & outcome_features
        assert shared, "No shared feature_* fields between ledger and outcome collector"
        missing = ledger_features - outcome_features
        assert not missing, (
            f"Ledger feature fields not in outcome collector: {missing}"
        )

    def test_to_dict_from_dict_roundtrip(self):
        """to_dict() → from_dict() must be lossless for populated fields."""
        rec = ledger_mod.SelectionRecord(
            selection_run_id="r1",
            selection_date="2026-08-05",
            symbol="TEST",
            formal_rank=1,
            score=82.6,
            base_score=80.3,
            sector="Technology",
            recommended_strategy="range_swing",
            data_source="live",
            run_mode="FULL",
            execution_mode="RESEARCH",
            candidate_type="RESEARCH_ONLY",
            entry_reference_price=80.0,
            range_low=70.0,
            range_high=90.0,
            range_width_pct=12.5,
            atr_pct=2.2,
            gap_rate=0.004,
            feature_volatility_score=78.0,
            feature_volume_score=54.0,
            feature_trend_score=74.0,
            feature_repeatability_score=47.0,
            feature_drawdown_safety_score=76.0,
            feature_liquidity_score=180_000_000.0,
            selector_version="0.12.16",
            scoring_logic_version="scorer.v1",
            composition_logic_version="selector.v1",
            universe_version="managed-snapshot.v1",
            recorded_at="2026-08-05T00:00:00+00:00",
        )

        d = rec.to_dict()
        rec2 = ledger_mod.SelectionRecord.from_dict(d)
        assert rec2.symbol == rec.symbol
        assert rec2.score == rec.score
        assert rec2.feature_volatility_score == rec.feature_volatility_score
        assert rec2.selector_version == rec.selector_version
        # Forward-looking should default to None
        assert rec2.price_5d is None
        assert rec2.return_21d_pct is None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Empty candidates → no-op
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmptyCandidates:
    def test_empty_formal_candidates_returns_none(self, monkeypatch, tmp_path: Path):
        """Empty formal_candidates list must NOT create a file or directory."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        result = ledger_mod.write_selection_snapshot(
            run_id="test-empty",
            date="2026-08-05",
            topk=[],
            formal_candidates=[],
        )
        assert result is None
        assert not runs.exists()

    def test_none_formal_candidates_returns_none(self, monkeypatch, tmp_path: Path):
        """None formal_candidates must NOT create a file."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        result = ledger_mod.write_selection_snapshot(
            run_id="test-none",
            date="2026-08-05",
            topk=[],
            formal_candidates=None,  # type: ignore[arg-type]
        )
        assert result is None

    def test_topk_without_formal_matches_returns_none(self, monkeypatch, tmp_path: Path):
        """When no topk ticker matches any formal candidate → no-op."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        result = ledger_mod.write_selection_snapshot(
            run_id="test-nomatch",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["OTHER_SYMBOL"],
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Index update
# ═══════════════════════════════════════════════════════════════════════════════

class TestIndexUpdate:
    def test_index_created_on_first_write(self, monkeypatch, tmp_path: Path):
        """After first write, ledger_index.json must exist with the date."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        ledger_mod.write_selection_snapshot(
            run_id="idx-test-1",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["AIG"],
        )

        assert idx_path.exists()
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        assert "2026-08-05" in idx["dates"]
        assert "2026-08-05" in idx["runs"]
        assert "idx-test-1" in idx["runs"]["2026-08-05"]

    def test_index_accumulates_multiple_dates(self, monkeypatch, tmp_path: Path):
        """Multiple writes across different dates must all appear in index."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        for date in ["2026-08-03", "2026-08-04", "2026-08-05"]:
            topk = [_make_topk_item("AIG", 1)]
            ledger_mod.write_selection_snapshot(
                run_id=f"multi-{date}",
                date=date,
                topk=topk,
                formal_candidates=["AIG"],
            )

        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        assert len(idx["dates"]) == 3
        assert idx["dates"] == ["2026-08-03", "2026-08-04", "2026-08-05"]

    def test_index_no_duplicate_run_ids(self, monkeypatch, tmp_path: Path):
        """Writing the same run_id twice must not create duplicate entries."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        for _ in range(3):
            ledger_mod.write_selection_snapshot(
                run_id="dedup-run",
                date="2026-08-05",
                topk=topk,
                formal_candidates=["AIG"],
            )

        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        runs_list = idx["runs"]["2026-08-05"]
        assert runs_list.count("dedup-run") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Atomic write
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_no_tmp_files_left_behind(self, monkeypatch, tmp_path: Path):
        """After write, no .tmp-* files should remain."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        ledger_mod.write_selection_snapshot(
            run_id="atomic-test",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["AIG"],
        )

        date_dir = runs / "2026-08-05"
        tmp_files = list(date_dir.glob("*.tmp-*"))
        assert len(tmp_files) == 0, f"Found leftover tmp files: {tmp_files}"

    def test_output_is_valid_json(self, monkeypatch, tmp_path: Path):
        """Written file must be valid JSON with correct structure."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        path = ledger_mod.write_selection_snapshot(
            run_id="json-test",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["AIG"],
        )
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["symbol"] == "AIG"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Version provenance fields
# ═══════════════════════════════════════════════════════════════════════════════

class TestVersionFields:
    def test_all_version_fields_populated(self, monkeypatch, tmp_path: Path):
        """All four version fields must be non-empty strings."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        path = ledger_mod.write_selection_snapshot(
            run_id="ver-test",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["AIG"],
        )
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        rec = data[0]

        for field in ["selector_version", "scoring_logic_version",
                      "composition_logic_version", "universe_version"]:
            assert rec.get(field), f"{field} is empty or missing"
            assert isinstance(rec[field], str), f"{field} is not str: {type(rec[field])}"

    def test_selector_version_matches_package(self, monkeypatch, tmp_path: Path):
        """selector_version must match quantcairn.__version__."""
        from quantcairn import __version__
        _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        path = ledger_mod.write_selection_snapshot(
            run_id="pkg-ver-test",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["AIG"],
        )
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data[0]["selector_version"] == __version__


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Failure isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestFailureIsolation:
    def test_bad_topk_missing_ticker_does_not_crash(self, monkeypatch, tmp_path: Path):
        """topk items without 'ticker' key must not crash the ledger."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        result = ledger_mod.write_selection_snapshot(
            run_id="bad-topk",
            date="2026-08-05",
            topk=[{"no_ticker": True}],
            formal_candidates=["AIG"],
        )
        assert result is None

    def test_bad_topk_none_scores_do_not_crash(self, monkeypatch, tmp_path: Path):
        """None/invalid score values must not crash — use 0.0 defaults."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        item = {
            "ticker": "TEST",
            "score": None,
            "base_score": "not_a_number",
            "volatility_score": None,
            "volume_score": None,
            "trend_fit_score": None,
            "repeatability_score": None,
            "drawdown_safety_score": None,
            "liquidity_score": None,
            "metrics": {},
            "risk": {},
        }
        path = ledger_mod.write_selection_snapshot(
            run_id="none-scores",
            date="2026-08-05",
            topk=[item],
            formal_candidates=["TEST"],
        )
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        rec = data[0]
        assert rec["score"] == 0.0
        assert rec["base_score"] == 0.0
        assert rec["feature_volatility_score"] == 0.0
        assert rec["feature_volume_score"] == 0.0
        assert rec["feature_trend_score"] == 0.0
        assert rec["feature_repeatability_score"] == 0.0
        assert rec["feature_drawdown_safety_score"] == 0.0
        assert rec["feature_liquidity_score"] == 0.0

    def test_corrupt_index_is_rebuilt(self, monkeypatch, tmp_path: Path):
        """A corrupt ledger_index.json must be rebuilt, not crash."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        # Write garbage to index
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        idx_path.write_text("not valid json {{{", encoding="utf-8")

        # _load_index should recover
        idx = ledger_mod._load_index()
        assert idx["schema_version"] != ""

        # Write should still work
        topk = [_make_topk_item("AIG", 1)]
        result = ledger_mod.write_selection_snapshot(
            run_id="corrupt-idx",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["AIG"],
        )
        assert result is not None

    def test_missing_run_file_gracefully_skipped(self, monkeypatch, tmp_path: Path):
        """load_selection_history must skip missing run files, not crash."""
        root, runs, idx_path = _redirect_ledger_to(monkeypatch, tmp_path)

        # Write one run
        topk = [_make_topk_item("AIG", 1)]
        ledger_mod.write_selection_snapshot(
            run_id="present",
            date="2026-08-05",
            topk=topk,
            formal_candidates=["AIG"],
        )

        # Delete the run file to simulate corruption
        run_file = runs / "2026-08-05" / "present.json"
        run_file.unlink()

        # Loading must not crash — just skip the missing file
        records = ledger_mod.load_selection_history(since_date="2026-08-05")
        assert len(records) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Immutable snapshot behaviour
# ═══════════════════════════════════════════════════════════════════════════════

class TestImmutability:
    def test_write_twice_same_run_is_idempotent(self, monkeypatch, tmp_path: Path):
        """Writing the same run_id twice produces one clean file."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        path1 = ledger_mod.write_selection_snapshot(
            run_id="same-run", date="2026-08-05",
            topk=topk, formal_candidates=["AIG"],
        )
        path2 = ledger_mod.write_selection_snapshot(
            run_id="same-run", date="2026-08-05",
            topk=topk, formal_candidates=["AIG"],
        )
        assert path1 == path2
        assert path1 is not None
        assert path1.exists()
        data = json.loads(path1.read_text(encoding="utf-8"))
        assert len(data) == 1

    def test_recorded_at_is_iso_format(self, monkeypatch, tmp_path: Path):
        """recorded_at must be a valid ISO 8601 timestamp."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        topk = [_make_topk_item("AIG", 1)]
        path = ledger_mod.write_selection_snapshot(
            run_id="ts-test", date="2026-08-05",
            topk=topk, formal_candidates=["AIG"],
        )
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data[0]["recorded_at"]
        assert "T" in ts
        assert "+" in ts or "Z" in ts or ts.endswith("00:00")

    def test_load_latest_returns_most_recent(self, monkeypatch, tmp_path: Path):
        """load_latest_selection must return records from the newest date."""
        _redirect_ledger_to(monkeypatch, tmp_path)

        topk_old = [_make_topk_item("OLD", 1)]
        ledger_mod.write_selection_snapshot(
            run_id="old-run", date="2026-08-01",
            topk=topk_old, formal_candidates=["OLD"],
        )
        topk_new = [_make_topk_item("NEW", 1)]
        ledger_mod.write_selection_snapshot(
            run_id="new-run", date="2026-08-05",
            topk=topk_new, formal_candidates=["NEW"],
        )

        records = ledger_mod.load_latest_selection()
        assert len(records) == 1
        assert records[0].symbol == "NEW"
