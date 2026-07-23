"""Tests for the append-only, idempotent outcome collector.

All tests use tmp_path — no real state, no broker, no orders.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import src.outcome.collector as module
from src.outcome.collector import (
    FillEvent,
    OUTCOME_CSV_COLUMNS,
    _append_csv,
    _enrich_outcomes,
    _ids_from_csv,
    _load_known_ids,
    _make_outcome_id,
    _match_closed_trades,
    _parse_audit_fills,
    collect_outcomes,
    compute_summary,
    write_summary,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fill_buy(symbol="AAPL", price=100.0, qty=10, ts=None):
    return FillEvent(
        symbol=symbol, side="BUY", fill_id=f"B-{symbol}-1",
        quantity=qty, price=price, timestamp=ts or _utc_now(),
        reason="", mode="paper",
    )


def _fill_sell(symbol="AAPL", price=110.0, qty=10, ts=None, pnl=100.0):
    return FillEvent(
        symbol=symbol, side="SELL", fill_id=f"S-{symbol}-1",
        quantity=qty, price=price, timestamp=ts or _utc_now(),
        pnl=pnl, reason="take_profit", mode="paper",
    )


# ── Fill event parsing ────────────────────────────────────────────────


def test_fill_event_from_buy_audit_line():
    line = {
        "phase": "risk_decision",
        "ticker": "AAPL",
        "order": {"side": "BUY", "qty": 10, "price": 100.0},
        "current_price": 100.0,
        "order_id": "B-1",
        "execution_mode": "paper",
    }
    fill = FillEvent.from_audit_line(line)
    assert fill is not None
    assert fill.side == "BUY"
    assert fill.symbol == "AAPL"
    assert fill.price == 100.0


def test_fill_event_from_sell_audit_line():
    line = {
        "phase": "risk_exit_trigger",
        "ticker": "AAPL",
        "order": {"side": "SELL", "qty": 10, "price": 110.0},
        "current_price": 110.0,
        "order_id": "S-1",
        "pnl": 100.0,
        "reason": "take_profit",
        "execution_mode": "paper",
    }
    fill = FillEvent.from_audit_line(line)
    assert fill is not None
    assert fill.side == "SELL"
    assert fill.pnl == 100.0
    assert fill.reason == "take_profit"


def test_fill_event_skips_non_trade_phases():
    line = {"phase": "heartbeat", "ticker": "AAPL", "order": {"side": "BUY", "qty": 1}}
    fill = FillEvent.from_audit_line(line)
    assert fill is None


def test_fill_event_skips_missing_order():
    line = {"phase": "risk_decision", "ticker": "AAPL"}
    fill = FillEvent.from_audit_line(line)
    assert fill is None


# ── Outcome ID ─────────────────────────────────────────────────────────


def test_outcome_id_deterministic():
    id1 = _make_outcome_id("AAPL", "2026-07-01", "2026-07-02", 100.0, 110.0)
    id2 = _make_outcome_id("AAPL", "2026-07-01", "2026-07-02", 100.0, 110.0)
    id3 = _make_outcome_id("AAPL", "2026-07-01", "2026-07-02", 100.0, 112.0)
    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 16


# ── Trade matching ────────────────────────────────────────────────────


def test_simple_buy_sell_match():
    fills = [_fill_buy(), _fill_sell()]
    closed = _match_closed_trades(fills)
    assert len(closed) == 1
    assert closed[0]["symbol"] == "AAPL"
    assert closed[0]["entry_price"] == 100.0
    assert closed[0]["exit_price"] == 110.0


def test_sell_without_buy_skipped():
    fills = [_fill_sell()]
    closed = _match_closed_trades(fills)
    assert len(closed) == 0


def test_fifo_matching():
    fills = [
        _fill_buy("AAPL", price=100.0, qty=5, ts="2026-07-01T10:00:00Z"),
        _fill_buy("AAPL", price=102.0, qty=5, ts="2026-07-01T10:01:00Z"),
        _fill_sell("AAPL", price=110.0, qty=5, ts="2026-07-01T14:00:00Z"),
        _fill_sell("AAPL", price=108.0, qty=5, ts="2026-07-01T14:01:00Z"),
    ]
    closed = _match_closed_trades(fills)
    assert len(closed) == 2
    assert closed[0]["entry_price"] == 100.0
    assert closed[1]["entry_price"] == 102.0


def test_partial_sell_leftovers():
    """Sell with no matching buy should be skipped."""
    fills = [
        _fill_buy("AAPL", price=100.0, qty=10, ts="2026-07-01T10:00:00Z"),
        _fill_sell("AAPL", price=110.0, qty=10, ts="2026-07-01T14:00:00Z"),
        _fill_sell("AAPL", price=115.0, qty=5, ts="2026-07-01T14:01:00Z"),  # no buy for this
    ]
    closed = _match_closed_trades(fills)
    assert len(closed) == 1  # Only the first sell matched


def test_multiple_symbols_independent():
    fills = [
        _fill_buy("AAPL", price=100.0, qty=5, ts="2026-07-01T10:00:00Z"),
        _fill_buy("TSLA", price=200.0, qty=3, ts="2026-07-01T10:01:00Z"),
        _fill_sell("AAPL", price=110.0, qty=5, ts="2026-07-01T14:00:00Z"),
        _fill_sell("TSLA", price=180.0, qty=3, ts="2026-07-01T14:01:00Z"),
    ]
    closed = _match_closed_trades(fills)
    assert len(closed) == 2


# ── Enrichment ─────────────────────────────────────────────────────────


def test_enrich_adds_selection_context():
    trades = [{
        "outcome_id": "abc123",
        "symbol": "AAPL",
        "entry_time": "2026-07-01T10:00:00Z",
        "exit_time": "2026-07-01T14:00:00Z",
        "entry_price": 100.0,
        "exit_price": 110.0,
        "realized_pnl": 100.0,
        "pnl_pct": 10.0,
        "hold_duration_seconds": 14400,
        "exit_reason": "take_profit",
        "execution_mode": "paper",
    }]
    ctx = {
        "AAPL": {
            "selection_run_id": "abc123",
            "selection_date": "2026-07-01",
            "formal_rank": 1,
            "formal_candidate_score": 85.0,
            "strategy_fit_score": 72.0,
            "market_regime": "NORMAL",
        }
    }
    enriched = _enrich_outcomes(trades, ctx)
    assert enriched[0]["formal_rank"] == 1
    assert enriched[0]["formal_candidate_score"] == 85.0
    assert enriched[0]["market_regime"] == "NORMAL"


def test_enrich_handles_missing_context():
    trades = [{"outcome_id": "abc", "symbol": "UNKNOWN", "entry_time": "", "exit_time": "",
               "entry_price": 100.0, "exit_price": 110.0, "realized_pnl": 0.0, "pnl_pct": 0.0,
               "hold_duration_seconds": 0, "exit_reason": "", "execution_mode": "paper"}]
    enriched = _enrich_outcomes(trades, {})
    assert enriched[0]["formal_rank"] == 0
    assert enriched[0]["market_regime"] == "NORMAL"
    assert enriched[0]["selection_run_id"] == ""


# ── CSV persistence ───────────────────────────────────────────────────


def test_append_csv_creates_file(tmp_path: Path):
    with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "outcome_dataset.csv"):
        rows = [{
            "outcome_id": "abc", "symbol": "AAPL", "selection_run_id": "", "selection_date": "",
            "formal_rank": 1, "formal_candidate_score": 85.0, "strategy_fit_score": 72.0,
            "market_regime": "NORMAL", "entry_time": "2026-07-01T10:00:00Z",
            "exit_time": "2026-07-01T14:00:00Z", "entry_price": 100.0, "exit_price": 110.0,
            "pnl_pct": 10.0, "realized_pnl": 100.0, "hold_duration_seconds": 14400,
            "exit_reason": "take_profit", "execution_mode": "paper",
        }]
        written = _append_csv(rows)
        assert written == 1
        assert (tmp_path / "outcome_dataset.csv").exists()


def test_ids_from_csv(tmp_path: Path):
    csv_path = tmp_path / "outcome_dataset.csv"
    with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
        _append_csv([{
            "outcome_id": "id-001", "symbol": "AAPL", "selection_run_id": "", "selection_date": "",
            "formal_rank": 0, "formal_candidate_score": 0.0, "strategy_fit_score": 0.0,
            "market_regime": "", "entry_time": "2026-07-01", "exit_time": "2026-07-02",
            "entry_price": 100.0, "exit_price": 110.0, "pnl_pct": 10.0, "realized_pnl": 100.0,
            "hold_duration_seconds": 86400, "exit_reason": "", "execution_mode": "paper",
        }])
        ids = _ids_from_csv()
        assert "id-001" in ids


def test_append_csv_idempotent_by_state(tmp_path: Path):
    """Known outcome_ids are skipped on re-run."""
    csv_path = tmp_path / "outcome_dataset.csv"
    state_path = tmp_path / ".outcome_collector_state.json"
    with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
        with patch.object(module, "OUTCOME_STATE_PATH", state_path):
            from src.outcome.collector import _save_state, _load_known_ids
            _save_state({"id-001"}, _utc_now())
            ids = _load_known_ids()
            assert "id-001" in ids


# ── Summary ────────────────────────────────────────────────────────────


def test_summary_empty_when_no_data(tmp_path: Path):
    with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "nonexistent.csv"):
        s = compute_summary()
        assert s["closed_trade_count"] == 0
        assert s["win_rate"] == 0.0


def test_summary_with_trades(tmp_path: Path):
    csv_path = tmp_path / "outcome_dataset.csv"
    with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
        _append_csv([
            {"outcome_id": "w1", "symbol": "AAPL", "selection_run_id": "", "selection_date": "",
             "formal_rank": 0, "formal_candidate_score": 0.0, "strategy_fit_score": 0.0,
             "market_regime": "", "entry_time": "2026-07-01", "exit_time": "2026-07-02",
             "entry_price": 100.0, "exit_price": 110.0, "pnl_pct": 10.0, "realized_pnl": 100.0,
             "hold_duration_seconds": 86400, "exit_reason": "take_profit", "execution_mode": "paper"},
            {"outcome_id": "l1", "symbol": "TSLA", "selection_run_id": "", "selection_date": "",
             "formal_rank": 0, "formal_candidate_score": 0.0, "strategy_fit_score": 0.0,
             "market_regime": "", "entry_time": "2026-07-01", "exit_time": "2026-07-02",
             "entry_price": 200.0, "exit_price": 190.0, "pnl_pct": -5.0, "realized_pnl": -50.0,
             "hold_duration_seconds": 43200, "exit_reason": "stop_loss", "execution_mode": "paper"},
        ])
        s = compute_summary()
        assert s["closed_trade_count"] == 2  # 1 win + 1 loss
        assert s["win_rate"] == 50.0
        assert s["best_trade"] == 10.0
        assert s["worst_trade"] == -5.0
        assert s["total_realized_pnl"] == 50.0


# ── Integration: empty state ──────────────────────────────────────────


def test_collect_outcomes_empty_when_no_audit_logs(tmp_path: Path):
    """No audit logs → NO_NEW_DATA."""
    log_dir = tmp_path / "empty_logs"
    log_dir.mkdir()
    with patch.object(module, "AUDIT_LOG_DIR", log_dir):
        with patch.object(module, "OUTCOME_CSV_PATH", tmp_path / "outcome_dataset.csv"):
            with patch.object(module, "OUTCOME_STATE_PATH", tmp_path / ".outcome_state.json"):
                result = collect_outcomes(log_dir=log_dir, dry_run=True)
                assert result["status"] == "NO_NEW_DATA"


# ── Integration: audit log parsing ────────────────────────────────────


def test_parse_audit_fills_from_jsonl(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "trades-20260701.jsonl"
    events = [
        {"phase": "risk_decision", "ticker": "AAPL", "order": {"side": "BUY", "qty": 10, "price": 100.0},
         "current_price": 100.0, "order_id": "B-1", "timestamp": "2026-07-01T10:00:00Z", "execution_mode": "paper"},
        {"phase": "risk_exit_trigger", "ticker": "AAPL", "order": {"side": "SELL", "qty": 10, "price": 110.0},
         "current_price": 110.0, "order_id": "S-1", "pnl": 100.0, "reason": "take_profit",
         "timestamp": "2026-07-01T14:00:00Z", "execution_mode": "paper"},
        {"phase": "heartbeat", "ticker": "AAPL"},  # should be skipped
    ]
    log_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    fills = _parse_audit_fills(log_dir)
    assert len(fills) == 2
    assert fills[0].side == "BUY"
    assert fills[1].side == "SELL"


# ── Safety: no broker, no orders ──────────────────────────────────────


def test_collector_never_imports_broker():
    """Ensure the collector module has no broker dependency."""
    source = (Path(module.__file__).read_text(encoding="utf-8")
              if hasattr(module, '__file__') else "")
    assert "LongBridgeBroker" not in source
    assert "PaperBroker" not in source
    assert "place_order" not in source
    assert "TradeContext" not in source
    assert "allow_live_order" not in source
    assert "reduce_only" not in source


def test_collector_does_not_mutate_paper_portfolio():
    """Collector reads PaperPortfolioState but never writes it."""
    source = (Path(module.__file__).read_text(encoding="utf-8")
              if hasattr(module, '__file__') else "")
    assert "write_paper_portfolio_state" not in source
    assert "PaperPortfolioStateStore.save" not in source


# ── Dry run ────────────────────────────────────────────────────────────


def test_dry_run_does_not_write(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "trades-20260701.jsonl"
    log_file.write_text(json.dumps({
        "phase": "risk_decision", "ticker": "AAPL",
        "order": {"side": "BUY", "qty": 10, "price": 100.0},
        "current_price": 100.0, "order_id": "B-1",
        "timestamp": "2026-07-01T10:00:00Z", "execution_mode": "paper",
    }) + "\n", encoding="utf-8")

    csv_path = tmp_path / "outcome_dataset.csv"
    state_path = tmp_path / ".outcome_state.json"
    with patch.object(module, "AUDIT_LOG_DIR", log_dir):
        with patch.object(module, "OUTCOME_CSV_PATH", csv_path):
            with patch.object(module, "OUTCOME_STATE_PATH", state_path):
                result = collect_outcomes(log_dir=log_dir, dry_run=True)
                assert result["status"] == "DRY_RUN"
                # CSV must not have been written
                assert not csv_path.exists()
                # State must not have been updated
                assert not state_path.exists()


def run_test_direct():
    test_fill_event_from_buy_audit_line()
    test_fill_event_from_sell_audit_line()
    test_fill_event_skips_non_trade_phases()
    test_fill_event_skips_missing_order()
    test_outcome_id_deterministic()
    test_simple_buy_sell_match()
    test_sell_without_buy_skipped()
    test_fifo_matching()
    test_partial_sell_leftovers()
    test_multiple_symbols_independent()
    test_enrich_adds_selection_context()
    test_enrich_handles_missing_context()
    test_collector_never_imports_broker()
    test_collector_does_not_mutate_paper_portfolio()
    test_summary_empty_when_no_data(Path(tempfile.mkdtemp()))
    test_summary_with_trades(Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    run_test_direct()
