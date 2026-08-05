"""Tests for Paper Trading Research Bridge — Phase 5A.

Verifies:
  1. Round-trip: create, save, load
  2. Duplicate prevention
  3. Atomic write (no partial files)
  4. Missing ledger handling
  5. Deterministic output
  6. Status transitions
  7. Load with filters (date, status, symbol)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.openalpha import paper_research as pr
from src.openalpha.paper_research import (
    PaperTradeRecord,
    create_paper_trade,
    create_paper_trades_from_ledger,
    save_paper_trades,
    load_paper_trades,
    update_paper_trade,
    PAPER_ROOT,
    LEDGER_ROOT,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_roots(monkeypatch, base: Path) -> tuple[Path, Path]:
    paper = base / "paper_trading"
    ledger = base / "selection_ledger"
    monkeypatch.setattr(pr, "PAPER_ROOT", paper)
    monkeypatch.setattr(pr, "LEDGER_ROOT", ledger)
    return paper, ledger


def _write_json(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_ledger_entry(symbol: str = "AIG", run_id: str = "r1",
                       date: str = "2026-08-05", **overrides) -> dict:
    rec = {
        "selection_run_id": run_id,
        "selection_date": date,
        "symbol": symbol,
        "formal_rank": 1,
        "entry_reference_price": 80.0,
        "range_low": 70.0,
        "range_high": 90.0,
        "sector": "Financial Services",
        "score": 82.6,
        "recommended_strategy": "range_swing",
        **overrides,
    }
    return rec


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Round trip
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundTrip:
    def test_create_save_load(self, monkeypatch, tmp_path: Path):
        """Create a trade, save it, load it back — all fields intact."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(
            selection_run_id="r1",
            selection_date="2026-08-05",
            symbol="AIG",
            entry_price=80.0,
            range_low=70.0,
            range_high=90.0,
            sector="Financial Services",
            score=82.6,
            recommended_strategy="range_swing",
        )

        save_paper_trades([trade], date_str="2026-08-05")
        loaded = load_paper_trades(date_str="2026-08-05")

        assert len(loaded) == 1
        t = loaded[0]
        assert t.symbol == "AIG"
        assert t.status == "OPEN"
        assert t.entry_price == 80.0
        assert t.range_low == 70.0
        assert t.range_high == 90.0
        assert t.score == 82.6
        assert t.sector == "Financial Services"
        assert t.paper_research_version == pr.PAPER_RESEARCH_VERSION

    def test_multiple_trades_same_run(self, monkeypatch, tmp_path: Path):
        """Multiple trades from the same run must all be saved."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trades = [
            create_paper_trade(selection_run_id="r1", selection_date="2026-08-05",
                               symbol=s)  # type: ignore[arg-type]
            for s in ["AIG", "ACGL", "AGNC"]
        ]
        save_paper_trades(trades, date_str="2026-08-05")

        loaded = load_paper_trades(date_str="2026-08-05")
        symbols = {t.symbol for t in loaded}
        assert symbols == {"AIG", "ACGL", "AGNC"}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Duplicate prevention
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicatePrevention:
    def test_same_trade_id_not_duplicated(self, monkeypatch, tmp_path: Path):
        """Saving the same trade twice must not create a duplicate."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(
            selection_run_id="r1",
            selection_date="2026-08-05",
            symbol="AIG",
            entry_price=80.0,
        )

        save_paper_trades([trade], date_str="2026-08-05")
        save_paper_trades([trade], date_str="2026-08-05")

        loaded = load_paper_trades(date_str="2026-08-05")
        assert len(loaded) == 1

    def test_save_appends_new_trades(self, monkeypatch, tmp_path: Path):
        """New trades must be appended to existing file, not replace it."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        t1 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="A")
        t2 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="B")

        save_paper_trades([t1], date_str="2026-08-05")
        save_paper_trades([t2], date_str="2026-08-05")

        loaded = load_paper_trades(date_str="2026-08-05")
        assert len(loaded) == 2
        assert {t.symbol for t in loaded} == {"A", "B"}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Atomic write
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_no_tmp_files_left_behind(self, monkeypatch, tmp_path: Path):
        """After save, no .tmp-* files should remain."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="AIG")
        save_paper_trades([trade], date_str="2026-08-05")

        tmp_files = list(paper.rglob("*.tmp-*"))
        assert len(tmp_files) == 0, f"Found leftover tmp files: {tmp_files}"

    def test_output_is_valid_json(self, monkeypatch, tmp_path: Path):
        """Written file must be valid JSON array."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="AIG")
        save_paper_trades([trade], date_str="2026-08-05")

        path = paper / "2026-08-05" / "trades.json"
        data = json.loads(path.read_text())
        assert isinstance(data, list)
        assert data[0]["symbol"] == "AIG"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Missing ledger handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingLedger:
    def test_empty_ledger_returns_empty_list(self, monkeypatch, tmp_path: Path):
        """No ledger data → empty list, no crash."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trades = create_paper_trades_from_ledger()
        assert trades == []

    def test_load_with_no_files_returns_empty(self, monkeypatch, tmp_path: Path):
        """load_paper_trades with no files → empty list."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        loaded = load_paper_trades()
        assert loaded == []

    def test_create_from_ledger_populates_correctly(self, monkeypatch, tmp_path: Path):
        """Ledger entries must produce correct PaperTradeRecords."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        _write_json(ledger / "runs" / "2026-08-05" / "r1.json", [
            _make_ledger_entry("AIG", "r1", "2026-08-05"),
            _make_ledger_entry("ACGL", "r1", "2026-08-05"),
        ])

        trades = create_paper_trades_from_ledger()
        assert len(trades) == 2
        assert {t.symbol for t in trades} == {"AIG", "ACGL"}
        for t in trades:
            assert t.status == "OPEN"
            assert t.entry_price > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Deterministic output
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministic:
    def test_same_input_produces_same_trade(self):
        """Same create_paper_trade args must produce trades differing only in ID."""
        t1 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05",
                                symbol="AIG", entry_price=80.0, score=82.6)
        t2 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05",
                                symbol="AIG", entry_price=80.0, score=82.6)

        assert t1.symbol == t2.symbol
        assert t1.score == t2.score
        assert t1.entry_price == t2.entry_price
        assert t1.status == t2.status == "OPEN"
        # IDs are unique
        assert t1.paper_trade_id != t2.paper_trade_id


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Status transitions
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatusTransitions:
    def test_close_trade(self, monkeypatch, tmp_path: Path):
        """Update OPEN → CLOSED must set exit fields."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="AIG")
        save_paper_trades([trade], date_str="2026-08-05")

        updated = update_paper_trade(
            trade.paper_trade_id,
            status="CLOSED",
            exit_price=85.0,
            return_pct=6.25,
            holding_days=5,
            range_outcome="touch_both",
        )

        assert updated is not None
        assert updated.status == "CLOSED"
        assert updated.exit_price == 85.0
        assert updated.return_pct == 6.25
        assert updated.holding_days == 5
        assert updated.closed_at is not None

    def test_fail_trade(self, monkeypatch, tmp_path: Path):
        """Update OPEN → FAILED must set status_reason."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="AIG")
        save_paper_trades([trade], date_str="2026-08-05")

        updated = update_paper_trade(
            trade.paper_trade_id,
            status="FAILED",
            status_reason="breakdown_exit",
        )

        assert updated is not None
        assert updated.status == "FAILED"
        assert updated.status_reason == "breakdown_exit"

    def test_update_nonexistent_returns_none(self, monkeypatch, tmp_path: Path):
        """Updating a nonexistent trade_id must return None."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        result = update_paper_trade("nonexistent-id", status="CLOSED")
        assert result is None

    def test_invalid_status_rejected(self, monkeypatch, tmp_path: Path):
        """Invalid status must be rejected (return None)."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        trade = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="AIG")
        save_paper_trades([trade], date_str="2026-08-05")

        result = update_paper_trade(trade.paper_trade_id, status="INVALID")
        assert result is None

        # Trade must still be OPEN
        loaded = load_paper_trades(date_str="2026-08-05")
        assert loaded[0].status == "OPEN"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Load with filters
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadFilters:
    def test_filter_by_status(self, monkeypatch, tmp_path: Path):
        """load_paper_trades(status=...) must filter correctly."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        t1 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="A")
        t2 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="B")
        save_paper_trades([t1, t2], date_str="2026-08-05")

        update_paper_trade(t1.paper_trade_id, status="CLOSED", exit_price=85.0)

        open_trades = load_paper_trades(date_str="2026-08-05", status="OPEN")
        closed_trades = load_paper_trades(date_str="2026-08-05", status="CLOSED")

        assert len(open_trades) == 1
        assert open_trades[0].symbol == "B"
        assert len(closed_trades) == 1
        assert closed_trades[0].symbol == "A"

    def test_filter_by_symbol(self, monkeypatch, tmp_path: Path):
        """load_paper_trades(symbol=...) must filter correctly."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        t1 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="AIG")
        t2 = create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="ACGL")
        save_paper_trades([t1, t2], date_str="2026-08-05")

        result = load_paper_trades(date_str="2026-08-05", symbol="AIG")
        assert len(result) == 1
        assert result[0].symbol == "AIG"

    def test_load_all_dates(self, monkeypatch, tmp_path: Path):
        """load_paper_trades without date filter must return all dates."""
        paper, ledger = _redirect_roots(monkeypatch, tmp_path)

        save_paper_trades([
            create_paper_trade(selection_run_id="r1", selection_date="2026-08-05", symbol="A")
        ], date_str="2026-08-05")
        save_paper_trades([
            create_paper_trade(selection_run_id="r2", selection_date="2026-08-06", symbol="B")
        ], date_str="2026-08-06")

        all_trades = load_paper_trades()
        assert len(all_trades) == 2
        syms = {t.symbol for t in all_trades}
        assert syms == {"A", "B"}
