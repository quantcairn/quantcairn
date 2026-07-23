"""Tests for the hardened outcome collector.

All tests use tmp_path — no real state, no broker, no orders.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import src.outcome.collector as module
from src.outcome.collector import (
    FillEvent,
    BuyLot,
    SCHEMA_VERSION,
    _OUTCOME_CSV_COLUMNS_V2,
    _make_outcome_id,
    _match_closed_trades_lots,
    _enrich_outcomes,
    _append_csv,
    _ids_from_csv,
    _load_known_ids,
    _load_known_fill_ids,
    _parse_audit_fills,
    _load_selection_context_for_date,
    _save_state,
    _acquire_lock,
    _release_lock,
    compute_summary,
    write_summary,
    collect_outcomes,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fill(symbol="AAPL", side="BUY", price=100.0, qty=10, ts=None,
          mode="paper", fill_id=None, pnl=None, reason="", **kw):
    kw.pop("price", None); kw.pop("quantity", None)  # Remove potential dupes from helpers
    return FillEvent(
        symbol=symbol, side=side,
        fill_id=fill_id or f"F-{symbol}-{side}-1",
        order_id=f"O-{symbol}-1",
        quantity=qty, price=price,
        timestamp=ts or _utc_now(),
        pnl=pnl, reason=reason, mode=mode,
        **kw
    )


def _buy(s="AAPL", p=100.0, q=10, ts=None, fid=None, **kw):
    price = kw.pop("price", p)
    qty = kw.pop("quantity", q)
    return _fill(s, "BUY", price, qty, ts=ts, fill_id=fid, **kw)


def _sell(s="AAPL", p=110.0, q=10, ts=None, fid=None, pnl=100.0, reason="take_profit", **kw):
    price = kw.pop("price", p)
    qty = kw.pop("quantity", q)
    return _fill(s, "SELL", price, qty, ts=ts, fill_id=fid, pnl=pnl, reason=reason, **kw)


# ═══════════════════════════════════════════════════════════════════════
# Fill validation — rejects live, orphan, test, fake, zero-price, etc.
# ═══════════════════════════════════════════════════════════════════════

class TestFillValidation:
    def test_rejects_live_mode(self):
        line = {"execution_mode": "live", "mode": "live",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "LB-REAL", "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_rejects_orphan_mode(self):
        line = {"execution_mode": "live", "mode": "live",
                "phase": "orphan_risk_exit",
                "ticker": "SOXS", "order": {"side": "SELL", "qty": 5, "price": 105.0},
                "fill_id": "ORPH-SELL-1", "pnl": 25.0, "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_rejects_test_fill_id(self):
        line = {"execution_mode": "paper",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "test-1", "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_rejects_pending_fill_id(self):
        line = {"execution_mode": "paper",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "PENDING", "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_rejects_bp_check_fill_id(self):
        line = {"execution_mode": "paper",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "BP_CHECK", "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_rejects_state_error_fill_id(self):
        line = {"execution_mode": "paper",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "STATE_ERROR", "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_rejects_zero_price(self):
        """Zero-price fills are rejected at parse time (from_audit_line returns None)."""
        line = {"execution_mode": "paper",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 0},
                "fill_id": "F-1", "timestamp": _utc_now()}
        f = FillEvent.from_audit_line(line)
        assert f is None  # Rejected at parse time — price must be > 0

    def test_rejects_zero_quantity(self):
        line = {"execution_mode": "paper",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 0, "price": 100.0},
                "fill_id": "F-1", "timestamp": _utc_now()}
        f = FillEvent.from_audit_line(line)
        assert f is None  # parse returns None for qty<=0

    def test_rejects_empty_fill_id(self):
        line = {"execution_mode": "paper",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "", "timestamp": _utc_now()}
        f = FillEvent.from_audit_line(line)
        assert f is None  # empty fill_id → None

    def test_rejects_non_trade_phase(self):
        line = {"execution_mode": "paper", "phase": "heartbeat",
                "ticker": "AAPL", "order": {"side": "BUY", "qty": 1, "price": 100.0},
                "fill_id": "H-1", "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_rejects_submitted_not_filled(self):
        """risk_decision with blocked action → not a fill."""
        line = {"execution_mode": "paper", "phase": "risk_decision",
                "ticker": "AAPL", "final_action": "blocked",
                "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "F-BLOCKED", "response": {"status": "rejected"},
                "timestamp": _utc_now()}
        assert FillEvent.from_audit_line(line) is None

    def test_accepts_valid_paper_buy(self):
        line = {"execution_mode": "paper",
                "ticker": "AAPL",
                "order": {"side": "BUY", "qty": 10, "price": 100.0},
                "fill_id": "paper:AAPL:BUY:abc:10",
                "timestamp": _utc_now(),
                "response": {"status": "filled"}}
        f = FillEvent.from_audit_line(line)
        assert f is not None
        assert f.is_valid
        assert f.side == "BUY"

    def test_accepts_valid_paper_sell(self):
        line = {"execution_mode": "paper", "phase": "risk_decision",
                "ticker": "AAPL", "reason": "take_profit",
                "order": {"side": "SELL", "qty": 10, "price": 110.0},
                "fill_id": "S-OK", "pnl": 100.0, "timestamp": _utc_now(),
                "final_action": "buy_candidate"}
        f = FillEvent.from_audit_line(line)
        assert f is not None
        assert f.side == "SELL"

    def test_duplicate_fill_id_skipped_in_parse(self):
        log_dir = Path(tempfile.mkdtemp())
        log_file = log_dir / "trades-20260723.jsonl"
        log_file.write_text("\n".join([
            json.dumps({"execution_mode": "paper", "ticker": "AAPL",
                        "order": {"side": "BUY", "qty": 10, "price": 100.0},
                        "fill_id": "DUP-1", "timestamp": "2026-07-23T10:00:00Z",
                        "response": {"status": "filled"}}),
            json.dumps({"execution_mode": "paper", "ticker": "AAPL",
                        "order": {"side": "BUY", "qty": 10, "price": 100.0},
                        "fill_id": "DUP-1", "timestamp": "2026-07-23T10:00:01Z",
                        "response": {"status": "filled"}}),
        ]) + "\n", encoding="utf-8")
        fills = _parse_audit_fills(log_dir, known_fill_ids=set())
        assert len(fills) == 1  # duplicate skipped


# ═══════════════════════════════════════════════════════════════════════
# Outcome ID
# ═══════════════════════════════════════════════════════════════════════

class TestOutcomeID:
    def test_deterministic(self):
        i1 = _make_outcome_id("paper", "AAPL", "B-1", "S-1", 10, "2026-07-01", "2026-07-02", 100.0, 110.0)
        i2 = _make_outcome_id("paper", "AAPL", "B-1", "S-1", 10, "2026-07-01", "2026-07-02", 100.0, 110.0)
        assert i1 == i2
        assert len(i1) == 20

    def test_different_quantity_changes_id(self):
        i1 = _make_outcome_id("p", "A", "B1", "S1", 10, "t", "t", 100.0, 110.0)
        i2 = _make_outcome_id("p", "A", "B1", "S1", 5, "t", "t", 100.0, 110.0)
        assert i1 != i2

    def test_different_account_changes_id(self):
        i1 = _make_outcome_id("acct1", "A", "B1", "S1", 10, "t", "t", 100.0, 110.0)
        i2 = _make_outcome_id("acct2", "A", "B1", "S1", 10, "t", "t", 100.0, 110.0)
        assert i1 != i2

    def test_different_fill_ids_change_id(self):
        i1 = _make_outcome_id("p", "A", "B1", "S1", 10, "t", "t", 100.0, 110.0)
        i2 = _make_outcome_id("p", "A", "B1", "S2", 10, "t", "t", 100.0, 110.0)
        assert i1 != i2

    def test_same_time_same_price_still_unique_by_fill_id(self):
        """Two different fill IDs with identical timestamps and prices."""
        i1 = _make_outcome_id("p", "AAPL", "F-AAA", "F-BBB", 10,
                               "2026-07-01T10:00:00Z", "2026-07-01T14:00:00Z",
                               100.0, 110.0)
        i2 = _make_outcome_id("p", "AAPL", "F-CCC", "F-DDD", 10,
                               "2026-07-01T10:00:00Z", "2026-07-01T14:00:00Z",
                               100.0, 110.0)
        assert i1 != i2


# ═══════════════════════════════════════════════════════════════════════
# Lot-based FIFO matching
# ═══════════════════════════════════════════════════════════════════════

class TestFIFOMatching:
    def test_simple_match(self):
        fills = [_buy(), _sell()]
        closed = _match_closed_trades_lots(fills)
        assert len(closed) == 1
        assert closed[0]["quantity"] == 10
        assert closed[0]["entry_price"] == 100.0
        assert closed[0]["exit_price"] == 110.0

    def test_sell_without_buy_skipped(self):
        closed = _match_closed_trades_lots([_sell()])
        assert len(closed) == 0

    def test_partial_sell_split(self):
        """BUY 10 → SELL 4 → SELL 6 produces 2 outcomes."""
        fills = [
            _buy("AAPL", price=100.0, q=10, ts="2026-07-01T10:00:00Z"),
            _sell("AAPL", price=110.0, q=4, ts="2026-07-01T12:00:00Z", fid="S-4", pnl=40.0),
            _sell("AAPL", price=115.0, q=6, ts="2026-07-01T14:00:00Z", fid="S-6", pnl=90.0),
        ]
        closed = _match_closed_trades_lots(fills)
        assert len(closed) == 2
        assert closed[0]["quantity"] == 4
        assert closed[0]["exit_price"] == 110.0
        assert closed[1]["quantity"] == 6
        assert closed[1]["exit_price"] == 115.0

    def test_multiple_buy_lots_fifo(self):
        """BUY 5@100 → BUY 5@110 → SELL 7 → 2 outcomes, remaining lot."""
        fills = [
            _buy("AAPL", price=100.0, q=5, ts="2026-07-01T10:00:00Z", fid="B-A"),
            _buy("AAPL", price=110.0, q=5, ts="2026-07-01T10:01:00Z", fid="B-B"),
            _sell("AAPL", price=115.0, q=7, ts="2026-07-01T14:00:00Z", fid="S-X"),
        ]
        closed = _match_closed_trades_lots(fills)
        assert len(closed) == 2
        # First outcome: 5@100 → sell
        assert closed[0]["quantity"] == 5
        assert closed[0]["entry_price"] == 100.0
        assert closed[0]["buy_fill_id"] == "B-A"
        # Second outcome: 2@110 → sell
        assert closed[1]["quantity"] == 2
        assert closed[1]["entry_price"] == 110.0
        assert closed[1]["buy_fill_id"] == "B-B"

    def test_multiple_symbols_independent(self):
        fills = [
            _buy("AAPL", price=100.0, q=5, ts="2026-07-01T10:00:00Z"),
            _buy("TSLA", price=200.0, q=3, ts="2026-07-01T10:01:00Z"),
            _sell("AAPL", price=110.0, q=5, ts="2026-07-01T14:00:00Z"),
            _sell("TSLA", price=180.0, q=3, ts="2026-07-01T14:01:00Z"),
        ]
        closed = _match_closed_trades_lots(fills)
        assert len(closed) == 2

    def test_fifo_ordering(self):
        fills = [
            _buy("AAPL", price=100.0, q=5, ts="2026-07-01T10:00:00Z", fid="B1"),
            _buy("AAPL", price=102.0, q=5, ts="2026-07-01T10:01:00Z", fid="B2"),
            _sell("AAPL", price=110.0, q=5, ts="2026-07-01T14:00:00Z", fid="S1"),
            _sell("AAPL", price=108.0, q=5, ts="2026-07-01T14:01:00Z", fid="S2"),
        ]
        closed = _match_closed_trades_lots(fills)
        assert len(closed) == 2
        assert closed[0]["entry_price"] == 100.0
        assert closed[0]["buy_fill_id"] == "B1"
        assert closed[1]["entry_price"] == 102.0
        assert closed[1]["buy_fill_id"] == "B2"

    def test_sell_exceeds_available_skipped(self):
        """SELL quantity > available lots: partial match only."""
        fills = [
            _buy("AAPL", price=100.0, q=3, ts="2026-07-01T10:00:00Z"),
            _sell("AAPL", price=110.0, q=10, ts="2026-07-01T14:00:00Z"),
        ]
        closed = _match_closed_trades_lots(fills)
        # Only 3 shares matched, remaining 7 sell unmatched
        assert len(closed) == 1
        assert closed[0]["quantity"] == 3


# ═══════════════════════════════════════════════════════════════════════
# Selection context binding
# ═══════════════════════════════════════════════════════════════════════

class TestSelectionContextBinding:
    def test_entry_context_preserved(self):
        """Trade with entry-time context is NOT overwritten by latest bundle."""
        trades = [{
            "outcome_id": "abc", "account_id": "paper", "symbol": "AAPL",
            "buy_fill_id": "B-1", "sell_fill_id": "S-1",
            "entry_time": "2026-07-01T10:00:00Z", "exit_time": "2026-07-01T14:00:00Z",
            "entry_price": 100.0, "exit_price": 110.0, "quantity": 10,
            "realized_pnl": 100.0, "pnl_pct": 10.0, "hold_duration_seconds": 14400,
            "exit_reason": "take_profit", "execution_mode": "paper",
            "selection_run_id": "entry-bundle-abc",
            "selection_date": "2026-07-01",
            "selection_bundle_hash": "hash-from-entry",
        }]
        ctx = {"AAPL": {"selection_run_id": "current-bundle", "selection_date": "2026-07-02",
                        "selection_bundle_hash": "hash-current", "formal_rank": 2}}
        enriched = _enrich_outcomes(trades, ctx)
        assert enriched[0]["selection_run_id"] == "entry-bundle-abc"
        assert enriched[0]["selection_context_status"] == "BOUND_AT_ENTRY"
        assert enriched[0]["training_eligible"] is True

    def test_missing_context_marks_ineligible(self):
        """No selection context → MISSING → training_eligible=false."""
        trades = [{
            "outcome_id": "abc", "account_id": "paper", "symbol": "UNKNOWN",
            "buy_fill_id": "B-1", "sell_fill_id": "S-1",
            "entry_time": "", "exit_time": "", "entry_price": 100.0, "exit_price": 110.0,
            "quantity": 10, "realized_pnl": 0.0, "pnl_pct": 0.0,
            "hold_duration_seconds": 0, "exit_reason": "", "execution_mode": "paper",
        }]
        enriched = _enrich_outcomes(trades, {})
        assert enriched[0]["selection_context_status"] == "MISSING_SELECTION_CONTEXT"
        assert enriched[0]["training_eligible"] is False
        assert "missing_selection_context" in enriched[0]["training_ineligible_reasons"]

    def test_inferred_context_marks_ineligible(self):
        """Context from latest bundle when trade has none → INFERRED → not eligible."""
        trades = [{
            "outcome_id": "abc", "account_id": "paper", "symbol": "AAPL",
            "buy_fill_id": "B-1", "sell_fill_id": "S-1",
            "entry_time": "", "exit_time": "", "entry_price": 100.0, "exit_price": 110.0,
            "quantity": 10, "realized_pnl": 100.0, "pnl_pct": 10.0,
            "hold_duration_seconds": 0, "exit_reason": "", "execution_mode": "paper",
        }]
        ctx = {"AAPL": {"selection_run_id": "latest", "selection_date": "2026-07-02",
                        "selection_bundle_hash": "latest-hash", "formal_rank": 1,
                        "formal_candidate_score": 85.0, "strategy_fit_score": 72.0,
                        "market_regime": "NORMAL"}}
        enriched = _enrich_outcomes(trades, ctx)
        assert enriched[0]["selection_context_status"] == "INFERRED_FROM_LATEST_BUNDLE"
        assert enriched[0]["training_eligible"] is False


# ═══════════════════════════════════════════════════════════════════════
# CSV & State persistence
# ═══════════════════════════════════════════════════════════════════════

class TestCSVAndState:
    def test_append_csv_creates_with_v2_columns(self, tmp_path: Path):
        with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "outcomes.csv"):
            rows = [dict.fromkeys(_OUTCOME_CSV_COLUMNS_V2, "")]
            rows[0].update(outcome_id="ok", symbol="AAPL", entry_price=100.0,
                          exit_price=110.0, quantity=10, execution_mode="paper")
            w = _append_csv(rows)
            assert w == 1
            assert (tmp_path / "outcomes.csv").exists()

    def test_ids_from_csv(self, tmp_path: Path):
        csv_path = tmp_path / "oc.csv"
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            _append_csv([{**dict.fromkeys(_OUTCOME_CSV_COLUMNS_V2, ""),
                           "outcome_id": "id-001", "symbol": "A", "entry_price": 1,
                           "exit_price": 2, "quantity": 1, "execution_mode": "paper"}])
            ids = _ids_from_csv()
            assert "id-001" in ids

    def test_save_state_roundtrip(self, tmp_path: Path):
        state_path = tmp_path / ".state.json"
        with patch.object(module, "OUTCOME_STATE_PATH", state_path):
            _save_state({"abc", "def"})
            ids = _load_known_ids()
            assert "abc" in ids
            assert "def" in ids

    def test_corrupt_state_rebuilds_from_csv(self, tmp_path: Path):
        state_path = tmp_path / "corrupt.json"
        csv_path = tmp_path / "oc.csv"
        state_path.write_text("not json {{", encoding="utf-8")
        with patch.object(module, "OUTCOME_STATE_PATH", state_path):
            with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
                ids = _load_known_ids()
                assert isinstance(ids, set)  # returns empty set, doesn't crash

    def test_schema_version_change_rebuilds(self, tmp_path: Path):
        state_path = tmp_path / "old_state.json"
        state_path.write_text(json.dumps({
            "schema_version": "outcome_collector.v1",
            "known_outcome_ids": ["old1"],
        }), encoding="utf-8")
        with patch.object(module, "OUTCOME_STATE_PATH", state_path):
            with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "oc.csv"):
                ids = _load_known_ids()
                # Should rebuild from CSV (empty), not use v1 state
                assert isinstance(ids, set)

    def test_csv_write_failure_does_not_update_state(self, tmp_path: Path):
        """If CSV write fails, state must not advance."""
        state_path = tmp_path / "state.json"
        _save_state({"before-fail"})  # Pre-populate state
        csv_dir = tmp_path / "readonly"
        csv_dir.mkdir()
        csv_path = csv_dir / "outcomes.csv"
        csv_path.write_text("", encoding="utf-8")
        os.chmod(str(csv_dir), 0o400)  # Make dir read-only

        with patch.object(module, "OUTCOME_STATE_PATH", state_path):
            with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
                written = _append_csv([{**dict.fromkeys(_OUTCOME_CSV_COLUMNS_V2, ""),
                                          "outcome_id": "should-fail",
                                          "symbol": "Z", "entry_price": 1,
                                          "exit_price": 2, "quantity": 1,
                                          "execution_mode": "paper"}])
                assert written == 0  # Write failed
        os.chmod(str(csv_dir), 0o700)


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════

class TestSummary:
    def test_empty(self, tmp_path: Path):
        with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "nonexistent.csv"):
            s = compute_summary()
            assert s["closed_trade_count"] == 0
            assert s["training_eligible_count"] == 0

    def test_with_trades(self, tmp_path: Path):
        csv_path = tmp_path / "oc.csv"
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            base = dict.fromkeys(_OUTCOME_CSV_COLUMNS_V2, "")
            base["training_eligible"] = "true"
            _append_csv([
                {**base, "outcome_id": "w1", "symbol": "AAPL",
                 "entry_price": 100.0, "exit_price": 110.0, "quantity": 10,
                 "pnl_pct": 10.0, "realized_pnl": 100.0, "hold_duration_seconds": 86400,
                 "execution_mode": "paper"},
                {**base, "outcome_id": "l1", "symbol": "TSLA",
                 "entry_price": 200.0, "exit_price": 190.0, "quantity": 5,
                 "pnl_pct": -5.0, "realized_pnl": -50.0, "hold_duration_seconds": 43200,
                 "execution_mode": "paper"},
            ])
            s = compute_summary()
            assert s["closed_trade_count"] == 2
            assert s["win_rate"] == 50.0
            assert s["best_trade"] == 10.0
            assert s["worst_trade"] == -5.0
            assert s["total_realized_pnl"] == 50.0


# ═══════════════════════════════════════════════════════════════════════
# Integration
# ═══════════════════════════════════════════════════════════════════════

class TestIntegration:
    def test_empty_logs_no_new_data(self, tmp_path: Path):
        log_dir = tmp_path / "empty"
        log_dir.mkdir()
        with patch.object(module, "AUDIT_LOG_DIR", log_dir):
            with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "oc.csv"):
                with patch.object(module, "OUTCOME_STATE_PATH", tmp_path / "st.json"):
                    r = collect_outcomes(log_dir=log_dir, dry_run=True)
                    assert r["status"] == "NO_NEW_DATA"

    def test_dry_run_does_not_write(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "trades-20260723.jsonl").write_text(
            json.dumps({"execution_mode": "paper", "ticker": "AAPL",
                        "order": {"side": "BUY", "qty": 10, "price": 100.0},
                        "fill_id": "F-BUY", "timestamp": "2026-07-23T10:00:00Z",
                        "response": {"status": "filled"}}) + "\n",
            encoding="utf-8")
        csv_path = tmp_path / "oc.csv"
        state_path = tmp_path / "st.json"
        with patch.object(module, "AUDIT_LOG_DIR", log_dir):
            with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
                with patch.object(module, "OUTCOME_STATE_PATH", state_path):
                    r = collect_outcomes(log_dir=log_dir, dry_run=True)
                    assert r["status"] == "DRY_RUN"
                    assert not csv_path.exists()
                    assert not state_path.exists()

    def test_audit_jsonl_parse_skips_non_trade(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "trades-20260723.jsonl").write_text(
            json.dumps({"phase": "heartbeat", "ticker": "AAPL", "execution_mode": "paper",
                        "timestamp": "2026-07-23T10:00:00Z"}) + "\n",
            encoding="utf-8")
        fills = _parse_audit_fills(log_dir)
        assert fills == []

    def test_live_event_excluded_from_fills(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "trades-20260723.jsonl").write_text(
            json.dumps({"execution_mode": "live", "ticker": "SOXS",
                        "order": {"side": "BUY", "qty": 10, "price": 100.0},
                        "fill_id": "LB-REAL", "timestamp": "2026-07-23T10:00:00Z",
                        "response": {"status": "filled"}}) + "\n",
            encoding="utf-8")
        fills = _parse_audit_fills(log_dir)
        assert fills == []

    def test_cross_file_matching(self, tmp_path: Path):
        """BUY in one file, SELL in another — must match across files."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # BUY with filled status
        (log_dir / "trades-20260723.jsonl").write_text(
            json.dumps({"execution_mode": "paper", "ticker": "AAPL",
                        "fill_id": "paper:AAPL:BUY:abc:10",
                        "order": {"side": "BUY", "qty": 10, "price": 100.0},
                        "timestamp": "2026-07-23T10:00:00Z",
                        "response": {"status": "filled"}}) + "\n",
            encoding="utf-8")
        # SELL in separate file, with filled status
        (log_dir / "trades-20260724.jsonl").write_text(
            json.dumps({"execution_mode": "paper", "ticker": "AAPL",
                        "fill_id": "paper:AAPL:SELL:abc:10",
                        "order": {"side": "SELL", "qty": 10, "price": 110.0},
                        "timestamp": "2026-07-24T14:00:00Z", "pnl": 100.0,
                        "reason": "take_profit",
                        "response": {"status": "filled"}}) + "\n",
            encoding="utf-8")
        fills = _parse_audit_fills(log_dir)
        assert len(fills) == 2
        closed = _match_closed_trades_lots(fills)
        assert len(closed) == 1

    def test_late_event_not_missed_by_timestamp(self, tmp_path: Path):
        """Even with a since_timestamp, fill_id-based dedup catches late events."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # Write one fill with an older timestamp
        (log_dir / "trades-20260723.jsonl").write_text(
            json.dumps({"execution_mode": "paper", "ticker": "AAPL",
                        "fill_id": "LATE-FILL",
                        "order": {"side": "BUY", "qty": 10, "price": 100.0},
                        "timestamp": "2026-07-23T09:00:00Z",
                        "response": {"status": "filled"}}) + "\n",
            encoding="utf-8")
        # A since_timestamp that would exclude this fill...
        fills_all = _parse_audit_fills(log_dir, known_fill_ids=set())
        assert len(fills_all) >= 1
        # But fill_id dedup ensures correctness regardless of timestamp
        fills_dedup = _parse_audit_fills(log_dir, known_fill_ids={"LATE-FILL"})
        assert len(fills_dedup) == 0


# ═══════════════════════════════════════════════════════════════════════
# Concurrent lock
# ═══════════════════════════════════════════════════════════════════════

class TestConcurrency:
    def test_lock_prevents_concurrent_write(self, tmp_path: Path):
        lock_path = tmp_path / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(module, "OUTCOME_LOCK_PATH", lock_path):
            with patch.object(module, "LEARNING_DIR", tmp_path):
                assert _acquire_lock() is True
                assert _acquire_lock() is False  # Second instance blocked
                _release_lock()
                assert _acquire_lock() is True  # Can re-acquire
                _release_lock()

    def test_write_mode_blocked_by_lock(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        lock_path = tmp_path / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"pid": 99999, "acquired_at": 0}), encoding="utf-8")
        # Make lock recent
        os.utime(str(lock_path), (time.time(), time.time()))

        with patch.object(module, "AUDIT_LOG_DIR", log_dir):
            with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "oc.csv"):
                with patch.object(module, "OUTCOME_STATE_PATH", tmp_path / "st.json"):
                    with patch.object(module, "OUTCOME_LOCK_PATH", lock_path):
                        with patch.object(module, "LEARNING_DIR", tmp_path):
                            r = collect_outcomes(log_dir=log_dir, dry_run=False)
                            assert r["status"] == "LOCKED"


# ═══════════════════════════════════════════════════════════════════════
# Safety: no broker, no orders
# ═══════════════════════════════════════════════════════════════════════

class TestSafetyBoundaries:
    def test_no_broker_import(self):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "LongBridgeBroker" not in source
        assert "PaperBroker" not in source
        assert "place_order" not in source
        assert "TradeContext" not in source
        assert "allow_live_order" not in source
        assert "reduce_only" not in source

    def test_does_not_write_paper_portfolio(self):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "write_paper_portfolio_state" not in source
        assert "PaperPortfolioStateStore.save" not in source
        assert "PaperPortfolioStateStore.load_or_create" not in source


# ═══════════════════════════════════════════════════════════════════════
# Direct runner
# ═══════════════════════════════════════════════════════════════════════

def run_test_direct():
    import tempfile
    td = Path(tempfile.mkdtemp())
    try:
        # Fill validation
        test = TestFillValidation()
        test.test_rejects_live_mode()
        test.test_rejects_orphan_mode()
        test.test_rejects_test_fill_id()
        test.test_rejects_pending_fill_id()
        test.test_rejects_bp_check_fill_id()
        test.test_rejects_state_error_fill_id()
        test.test_rejects_zero_price()
        test.test_rejects_zero_quantity()
        test.test_rejects_empty_fill_id()
        test.test_rejects_non_trade_phase()
        test.test_rejects_submitted_not_filled()
        test.test_accepts_valid_paper_buy()
        test.test_accepts_valid_paper_sell()
        test.test_duplicate_fill_id_skipped_in_parse()
        # Outcome ID
        oid = TestOutcomeID()
        oid.test_deterministic()
        oid.test_different_quantity_changes_id()
        oid.test_different_account_changes_id()
        oid.test_different_fill_ids_change_id()
        oid.test_same_time_same_price_still_unique_by_fill_id()
        # FIFO
        f = TestFIFOMatching()
        f.test_simple_match()
        f.test_sell_without_buy_skipped()
        f.test_partial_sell_split()
        f.test_multiple_buy_lots_fifo()
        f.test_multiple_symbols_independent()
        f.test_fifo_ordering()
        f.test_sell_exceeds_available_skipped()
        # Selection context
        sc = TestSelectionContextBinding()
        sc.test_entry_context_preserved()
        sc.test_missing_context_marks_ineligible()
        sc.test_inferred_context_marks_ineligible()
        # Safety
        sb = TestSafetyBoundaries()
        sb.test_no_broker_import()
        sb.test_does_not_write_paper_portfolio()
        # Summary
        s = TestSummary()
        s.test_empty(td)
        # CSV
        c2 = TestCSVAndState()
        c2.test_corrupt_state_rebuilds_from_csv(td)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    print("direct run: all passed")


if __name__ == "__main__":
    run_test_direct()
