"""Tests for Selection Backfill Engine — Phase 2A.

Verifies:
  1. Forward return calculation (5d, 21d)
  2. True path-based MFE/MAE from OHLCV bars
  3. Range outcome: support-then-resistance → SUCCESS
  4. Range breakdown detection
  5. Range breakout detection
  6. Insufficient history handling
  7. Missing data handling
  8. Idempotent rerun
  9. Atomic write (no partial files)
  10. Content hash and version consistency
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.openalpha import selection_backfill as backfill_mod
from src.openalpha.selection_backfill import (
    OutcomeRecord,
    _compute_content_hash,
    _compute_forward_metrics,
    _determine_range_outcome,
    _failed_record,
    _fetch_forward_bars,
    _write_atomic,
    backfill_selection_outcomes,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_ohlcv_bars(
    start_date: str,
    n_bars: int = 30,
    base_price: float = 80.0,
    trend: float = 0.05,
    high_extra: float = 1.0,
    low_extra: float = -1.0,
    *,
    include_weekends: bool = False,
) -> list[dict[str, float]]:
    """Build synthetic OHLCV bars starting from *start_date*.

    Parameters:
      n_bars: number of bars to generate (calendar days, skipping weekends)
      base_price: starting close
      trend: daily drift fraction
      high_extra: added to close for high
      low_extra: subtracted from close for low
      include_weekends: if True, include Sat/Sun bars (for edge case tests)
    """
    from src.utils.market_calendar import is_us_market_trading_day

    s = date.fromisoformat(start_date)
    bars: list[dict[str, float]] = []
    price = base_price

    d = s + timedelta(days=1)  # start day AFTER selection date
    generated = 0
    max_iter = n_bars * 3  # safety to prevent infinite loop on holiday weeks
    iterations = 0

    while generated < n_bars and iterations < max_iter:
        iterations += 1
        if not include_weekends and not is_us_market_trading_day(d):
            d += timedelta(days=1)
            continue

        price = price * (1.0 + trend + (generated % 5 - 2) * 0.002)
        bars.append({
            "date": d.isoformat(),
            "open": round(price * 0.998, 2),
            "high": round(price + high_extra, 2),
            "low": round(price + low_extra, 2),
            "close": round(price, 2),
            "volume": 1_000_000.0 + generated * 10000,
        })
        generated += 1
        d += timedelta(days=1)

    return bars


def _make_test_bars_with_support_then_resistance(
    start_date: str = "2026-08-05",
) -> list[dict[str, float]]:
    """Build bars where price touches support first, then resistance later.
    Entry at 80, support at 75, resistance at 85.
    """
    from src.utils.market_calendar import is_us_market_trading_day

    s = date.fromisoformat(start_date)
    bars: list[dict[str, float]] = []
    d = s + timedelta(days=1)

    # Bar 1-3: price drifts down toward support
    # Bar 4: touches support (low <= 75)
    # Bar 5-7: bounces back
    # Bar 8: touches resistance (high >= 85)

    scenario = [
        # (close, high_extra, low_extra) relative to base
        (79.0, 0.5, -1.0),   # bar 1
        (77.0, 0.5, -1.5),   # bar 2
        (76.0, 0.3, -2.0),   # bar 3: getting close to 75
        (76.5, 0.5, -3.0),   # bar 4: low = 73.5, touch support! (range_low=75)
        (78.0, 0.5, -1.0),   # bar 5: bouncing
        (80.0, 1.0, -0.5),   # bar 6
        (83.0, 1.5, -0.5),   # bar 7
        (84.0, 2.5, -0.5),   # bar 8: high = 86.5, touch resistance! (range_high=85)
        (82.0, 1.0, -1.0),   # bar 9
        (81.0, 0.5, -1.0),   # bar 10
    ]

    bar_count = 0
    bar_idx = 0
    max_iter = 200
    iterations = 0
    while bar_count < 30 and iterations < max_iter:
        iterations += 1
        if not is_us_market_trading_day(d):
            d += timedelta(days=1)
            continue

        if bar_idx < len(scenario):
            c, hx, lx = scenario[bar_idx]
        else:
            c = 82.0 + (bar_idx - 10) * 0.05
            hx = 1.0
            lx = -1.0

        bars.append({
            "date": d.isoformat(),
            "open": round(c * 0.998, 2),
            "high": round(c + hx, 2),
            "low": round(c + lx, 2),
            "close": round(c, 2),
            "volume": 1_000_000.0 + bar_count * 10000,
        })
        bar_count += 1
        bar_idx += 1
        d += timedelta(days=1)

    return bars


def _redirect_outcomes_to(monkeypatch, base: Path) -> Path:
    """Redirect OUTCOMES_ROOT to a temp directory."""
    new_root = base / "selection_outcomes"
    monkeypatch.setattr(backfill_mod, "OUTCOMES_ROOT", new_root)
    return new_root


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Forward return calculation
# ═══════════════════════════════════════════════════════════════════════════════

class TestReturnCalculation:
    def test_5d_return_from_known_data(self):
        """5-trading-day return must match manual calculation."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=10, base_price=80.0, trend=0.0)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="test",
        )

        assert outcome.price_5d is not None, "price_5d should be set"
        assert outcome.return_5d_pct is not None
        # With trend=0 and base=80, price should be near 80
        assert abs(outcome.return_5d_pct) < 10.0, (
            f"Expected small return, got {outcome.return_5d_pct}%"
        )
        # Verify formula: (price_5d - 80) / 80 * 100
        expected = round((outcome.price_5d - 80.0) / 80.0 * 100.0, 4)
        assert outcome.return_5d_pct == expected

    def test_21d_return_from_known_data(self):
        """21-trading-day return must match manual calculation."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=30, base_price=100.0, trend=0.001)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=100.0,
            selection_date_str="2026-08-05",
            range_low=90.0,
            range_high=110.0,
            provider="test",
        )

        assert outcome.price_21d is not None, "price_21d should be set"
        assert outcome.return_21d_pct is not None
        expected = round((outcome.price_21d - 100.0) / 100.0 * 100.0, 4)
        assert outcome.return_21d_pct == expected

    def test_21d_not_set_when_insufficient_bars(self):
        """When fewer than 21 trading days are available, 21d fields must be None
        but 5d fields may still be set if 5 bars exist."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=7, base_price=80.0)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="test",
        )

        # 7 bars is enough for 5d
        assert outcome.price_5d is not None
        # But not enough for 21d (need 21+ trading days in bars)
        assert outcome.price_21d is None
        assert outcome.return_21d_pct is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. True path-based MFE/MAE
# ═══════════════════════════════════════════════════════════════════════════════

class TestMFEMAE:
    def test_mfe_uses_max_high(self):
        """MFE must use the highest high across all bars."""
        bars = _make_ohlcv_bars(
            "2026-08-05", n_bars=21, base_price=80.0,
            trend=0.002, high_extra=3.0, low_extra=-1.0,
        )

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=70.0,
            range_high=90.0,
            provider="test",
        )

        assert outcome.mfe_21d_pct is not None
        assert outcome.mfe_21d_pct >= 0.0, "MFE must be non-negative"
        # Manually compute expected MFE
        max_high = max(b["high"] for b in bars if b["date"] > "2026-08-05")
        expected_mfe = round((max_high - 80.0) / 80.0 * 100.0, 4)
        assert outcome.mfe_21d_pct == expected_mfe, (
            f"Expected MFE={expected_mfe}, got {outcome.mfe_21d_pct}"
        )

    def test_mae_uses_min_low(self):
        """MAE must use the lowest low across all bars."""
        bars = _make_ohlcv_bars(
            "2026-08-05", n_bars=21, base_price=80.0,
            trend=-0.001, high_extra=1.0, low_extra=-3.0,
        )

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=70.0,
            range_high=90.0,
            provider="test",
        )

        assert outcome.mae_21d_pct is not None
        assert outcome.mae_21d_pct <= 0.0, "MAE must be non-positive"
        min_low = min(b["low"] for b in bars if b["date"] > "2026-08-05")
        expected_mae = round((min_low - 80.0) / 80.0 * 100.0, 4)
        assert outcome.mae_21d_pct == expected_mae, (
            f"Expected MAE={expected_mae}, got {outcome.mae_21d_pct}"
        )

    def test_mfe_zero_when_entry_is_highest_high(self):
        """When entry is the highest point, MFE must be 0.0."""
        bars = _make_ohlcv_bars(
            "2026-08-05", n_bars=21, base_price=50.0,
            trend=-0.005, high_extra=0.5, low_extra=-2.0,
        )

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,  # entry ABOVE all future prices
            selection_date_str="2026-08-05",
            range_low=70.0,
            range_high=90.0,
            provider="test",
        )

        assert outcome.mfe_21d_pct == 0.0, (
            f"MFE should be 0 when entry is above all highs, got {outcome.mfe_21d_pct}"
        )

    def test_mae_zero_when_entry_is_lowest_low(self):
        """When entry is the lowest point, MAE must be 0.0."""
        bars = _make_ohlcv_bars(
            "2026-08-05", n_bars=21, base_price=120.0,
            trend=0.005, high_extra=2.0, low_extra=-0.5,
        )

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,  # entry BELOW all future prices
            selection_date_str="2026-08-05",
            range_low=70.0,
            range_high=90.0,
            provider="test",
        )

        assert outcome.mae_21d_pct == 0.0, (
            f"MAE should be 0 when entry is below all lows, got {outcome.mae_21d_pct}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Range outcome: support-then-resistance → SUCCESS
# ═══════════════════════════════════════════════════════════════════════════════

class TestRangeOutcome:
    def test_support_then_resistance_is_success(self):
        """When support is touched first, then resistance later → SUCCESS."""
        bars = _make_test_bars_with_support_then_resistance("2026-08-05")

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="test",
        )

        assert outcome.range_touch_support is True
        assert outcome.range_touch_resistance is True
        assert outcome.range_breakdown is False
        assert outcome.range_breakout is False
        assert outcome.range_outcome == "SUCCESS", (
            f"Expected SUCCESS, got {outcome.range_outcome}"
        )

    def test_resistance_then_support_is_not_success(self):
        """When resistance is touched first, then support → not SUCCESS."""
        from src.utils.market_calendar import is_us_market_trading_day

        s = date.fromisoformat("2026-08-05")
        bars = []
        d = s + timedelta(days=1)
        scenario = [
            (82.0, 4.0, -0.5),   # high=86 > range_high=85 → resistance first!
            (78.0, 0.5, -3.0),   # low=75 = range_low → support second
            (80.0, 1.0, -1.0),
        ]
        bar_idx = 0
        bar_count = 0
        iter_count = 0
        while bar_count < 21 and iter_count < 200:
            iter_count += 1
            if not is_us_market_trading_day(d):
                d += timedelta(days=1)
                continue
            if bar_idx < len(scenario):
                c, hx, lx = scenario[bar_idx]
            else:
                c, hx, lx = 81.0, 1.0, -1.0
            bars.append({
                "date": d.isoformat(),
                "open": round(c * 0.998, 2),
                "high": round(c + hx, 2),
                "low": round(c + lx, 2),
                "close": round(c, 2),
                "volume": 1_000_000.0,
            })
            bar_count += 1
            bar_idx += 1
            d += timedelta(days=1)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="test",
        )

        assert outcome.range_touch_support is True
        assert outcome.range_touch_resistance is True
        # Resistance-first should NOT be SUCCESS
        assert outcome.range_outcome in ("PARTIAL", "FAILED"), (
            f"Resistance-first should not be SUCCESS, got {outcome.range_outcome}"
        )

    def test_neither_touched_is_partial(self):
        """When neither support nor resistance is touched → PARTIAL."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=80.0, trend=0.0,
                                high_extra=1.0, low_extra=-1.0)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=60.0,   # far below
            range_high=100.0,  # far above
            provider="test",
        )

        assert outcome.range_touch_support is False
        assert outcome.range_touch_resistance is False
        assert outcome.range_outcome == "PARTIAL"

    def test_range_values_zero_produces_unknown(self):
        """When range_low or range_high is zero, range_outcome = UNKNOWN."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=10, base_price=80.0)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=0.0,
            range_high=0.0,
            provider="test",
        )

        assert outcome.range_outcome == "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# 4 & 5. Breakdown and breakout detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestBreakdownBreakout:
    def test_breakdown_detected(self):
        """When close < range_low → range_breakdown=True."""
        # Build bars with close prices clearly below range_low=65
        # and low prices even lower (so they show as breakdown, not just touch)
        bars = _make_ohlcv_bars("2026-08-05", n_bars=10, base_price=55.0, trend=-0.01,
                                high_extra=0.5, low_extra=-3.0)

        # Verify at least some bars have close < range_low (65)
        close_vals = [b["close"] for b in bars if b["date"] > "2026-08-05"]
        assert any(c < 65.0 for c in close_vals), f"No close < 65 in {close_vals[:5]}..."

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=65.0,
            range_high=100.0,
            provider="test",
        )

        assert outcome.range_breakdown is True, (
            f"Expected breakdown, got {outcome.range_breakdown}"
        )
        assert outcome.range_outcome == "FAILED", (
            f"Expected FAILED, got {outcome.range_outcome}"
        )

    def test_breakout_detected(self):
        """When close > range_high → range_breakout=True."""
        # Build bars with close prices clearly above range_high=85
        bars = _make_ohlcv_bars("2026-08-05", n_bars=10, base_price=95.0, trend=0.01,
                                high_extra=2.0, low_extra=-0.5)

        close_vals = [b["close"] for b in bars if b["date"] > "2026-08-05"]
        assert any(c > 85.0 for c in close_vals), f"No close > 85 in {close_vals[:5]}..."

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=50.0,
            range_high=85.0,
            provider="test",
        )

        assert outcome.range_breakout is True, (
            f"Expected breakout, got {outcome.range_breakout}"
        )
        assert outcome.range_outcome == "FAILED", (
            f"Expected FAILED, got {outcome.range_outcome}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Insufficient history
# ═══════════════════════════════════════════════════════════════════════════════

class TestInsufficientHistory:
    def test_too_few_bars_sets_insufficient_history(self):
        """Fewer than 5 trading-day bars → INSUFFICIENT_HISTORY."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=3, base_price=80.0)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="test",
        )

        assert outcome.status == "INSUFFICIENT_HISTORY"
        assert outcome.price_5d is None
        assert outcome.price_21d is None
        assert outcome.mfe_21d_pct is None
        assert outcome.range_outcome == "UNKNOWN"

    def test_returns_partial_when_5d_available_21d_not(self):
        """When 5d is available but 21d is not, 5d fields set, 21d fields None."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=10, base_price=80.0)

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="test",
        )

        # 10 trading days → 5d available, 21d not
        assert outcome.price_5d is not None
        assert outcome.return_5d_pct is not None
        assert outcome.price_21d is None
        assert outcome.return_21d_pct is None
        # MFE/MAE and range still computed from available bars
        assert outcome.mfe_21d_pct is not None
        assert outcome.mae_21d_pct is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Missing data
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingData:
    def test_empty_bars_produces_failed_data(self):
        """Empty bar list → FAILED_DATA status."""
        outcome = _compute_forward_metrics(
            bars=[],
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="none",
        )

        assert outcome.status == "FAILED_DATA"
        assert outcome.price_5d is None
        assert outcome.price_21d is None
        assert outcome.range_outcome == "UNKNOWN"

    def test_bars_all_before_selection_date(self):
        """Bars dated before selection date → FAILED_DATA."""
        bars = [
            {"date": "2026-08-01", "open": 79.0, "high": 81.0, "low": 78.0, "close": 80.0, "volume": 1e6},
            {"date": "2026-08-04", "open": 80.0, "high": 82.0, "low": 79.0, "close": 81.0, "volume": 1e6},
        ]

        outcome = _compute_forward_metrics(
            bars=bars,
            entry_price=80.0,
            selection_date_str="2026-08-05",
            range_low=75.0,
            range_high=85.0,
            provider="test",
        )

        assert outcome.status == "FAILED_DATA"
        assert "no bars after" in outcome.status_detail

    def test_failed_record_factory(self):
        """_failed_record() must produce a valid OutcomeRecord."""
        rec = _failed_record(
            "2026-08-05", 80.0, 75.0, 85.0, "test", "FAILED_DATA", "test error"
        )
        assert rec.status == "FAILED_DATA"
        assert rec.status_detail == "test error"
        assert rec.range_outcome == "UNKNOWN"
        assert rec.entry_reference_price == 80.0
        assert rec.data_provider == "test"
        assert rec.evaluation_version == "backfill.v1"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Idempotent rerun
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    def test_same_data_produces_same_hash(self):
        """Identical OHLCV data must produce identical content_hash."""
        bars1 = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=80.0, trend=0.001)
        bars2 = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=80.0, trend=0.001)

        outcome1 = _compute_forward_metrics(
            bars=bars1, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )
        outcome2 = _compute_forward_metrics(
            bars=bars2, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )

        # Stamp identity
        outcome1.selection_run_id = "r1"
        outcome1.symbol = "TEST"
        outcome1.formal_rank = 1
        outcome2.selection_run_id = "r1"
        outcome2.symbol = "TEST"
        outcome2.formal_rank = 1

        h1 = _compute_content_hash(outcome1)
        h2 = _compute_content_hash(outcome2)
        assert h1 == h2, f"Same data must produce same hash: {h1} vs {h2}"

    def test_different_data_produces_different_hash(self):
        """Different OHLCV data must produce different content_hash."""
        bars1 = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=80.0, trend=0.001)
        bars2 = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=100.0, trend=-0.002)

        outcome1 = _compute_forward_metrics(
            bars=bars1, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )
        outcome2 = _compute_forward_metrics(
            bars=bars2, entry_price=100.0, selection_date_str="2026-08-05",
            range_low=90.0, range_high=110.0, provider="test",
        )

        outcome1.selection_run_id = "r1"
        outcome1.symbol = "A"
        outcome1.formal_rank = 1
        outcome2.selection_run_id = "r2"
        outcome2.symbol = "B"
        outcome2.formal_rank = 1

        h1 = _compute_content_hash(outcome1)
        h2 = _compute_content_hash(outcome2)
        assert h1 != h2, "Different data must produce different hashes"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Atomic write and file integrity
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_no_tmp_files_after_write(self, tmp_path: Path):
        """After atomic write, no .tmp-* files remain."""
        path = tmp_path / "test.json"
        _write_atomic(path, '{"key": "value"}')

        tmp_files = list(tmp_path.glob("*.tmp-*"))
        assert len(tmp_files) == 0, f"Leftover tmp files: {tmp_files}"
        assert path.exists()
        assert json.loads(path.read_text()) == {"key": "value"}

    def test_write_overwrites_existing(self, tmp_path: Path):
        """Writing to an existing path replaces it cleanly."""
        path = tmp_path / "overwrite.json"
        _write_atomic(path, '{"version": 1}')
        _write_atomic(path, '{"version": 2}')

        data = json.loads(path.read_text())
        assert data == {"version": 2}

    def test_backfill_skips_existing_valid_file(self, monkeypatch, tmp_path: Path):
        """When outcome file already exists with valid hashes, skip it."""
        from src.openalpha.selection_ledger import SelectionRecord

        outcomes_root = _redirect_outcomes_to(monkeypatch, tmp_path)

        # Create a ledger record to backfill
        rec = SelectionRecord(
            selection_run_id="skip-run",
            selection_date="2026-08-05",
            symbol="SKIP",
            formal_rank=1,
            score=80.0, base_score=78.0,
            sector="Test", recommended_strategy="range_swing",
            data_source="live", run_mode="FULL", execution_mode="RESEARCH",
            candidate_type="RESEARCH_ONLY",
            entry_reference_price=80.0, range_low=75.0, range_high=85.0,
            range_width_pct=12.5, atr_pct=2.2, gap_rate=0.004,
            feature_volatility_score=78.0, feature_volume_score=54.0,
            feature_trend_score=74.0, feature_repeatability_score=47.0,
            feature_drawdown_safety_score=76.0, feature_liquidity_score=50.0,
            selector_version="0.12.16", scoring_logic_version="sv1",
            composition_logic_version="cv1", universe_version="uv1",
            recorded_at="2026-08-05T00:00:00+00:00",
        )

        # Pre-create a valid outcome file
        outcome_dir = outcomes_root / "2026-08-05"
        outcome_dir.mkdir(parents=True, exist_ok=True)
        outcome_path = outcome_dir / "skip-run.json"

        existing_outcome = OutcomeRecord(
            selection_run_id="skip-run",
            selection_date="2026-08-05",
            symbol="SKIP",
            formal_rank=1,
            entry_reference_price=80.0,
            entry_price_type="selection_reference",
            range_low=75.0,
            range_high=85.0,
            price_5d=82.0,
            price_21d=84.0,
            return_5d_pct=2.5,
            return_21d_pct=5.0,
            mfe_21d_pct=6.0,
            mae_21d_pct=-1.0,
            range_touch_support=True,
            range_touch_resistance=True,
            range_breakdown=False,
            range_breakout=False,
            range_outcome="SUCCESS",
            status="SUCCESS",
            data_provider="test",
            evaluation_version="backfill.v1",
            backfilled_at="2026-08-05T00:00:00+00:00",
        )
        existing_outcome.content_hash = _compute_content_hash(existing_outcome)

        outcome_path.write_text(
            json.dumps([existing_outcome.to_dict()], ensure_ascii=False, indent=2)
        )

        # Monkeypatch load_selection_history to return our record
        monkeypatch.setattr(
            backfill_mod, "load_selection_history",
            lambda **kw: [rec],
        )

        # Also patch _fetch_forward_bars so it's NOT called (skip path)
        fetch_called = [0]
        def counting_fetch(sym, from_date, calendar_days_forward=60):
            fetch_called[0] += 1
            return _make_ohlcv_bars(from_date, n_bars=21), "test"

        monkeypatch.setattr(backfill_mod, "_fetch_forward_bars", counting_fetch)

        result = backfill_selection_outcomes(since_date="2026-08-05")
        assert result["skipped"] == 1
        assert result["backfilled"] == 0
        # fetch should NOT have been called since file was skipped
        assert fetch_called[0] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Hash and version consistency
# ═══════════════════════════════════════════════════════════════════════════════

class TestHashAndVersion:
    def test_evaluation_version_is_set(self):
        """All OutcomeRecords must carry evaluation_version."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=10, base_price=80.0)
        outcome = _compute_forward_metrics(
            bars=bars, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )
        assert outcome.evaluation_version == "backfill.v1"

    def test_content_hash_is_32_chars(self):
        """Content hash must be 32 hex characters (SHA-256 truncated)."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=80.0)
        outcome = _compute_forward_metrics(
            bars=bars, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )
        outcome.selection_run_id = "hash-test"
        outcome.symbol = "HASH"
        outcome.formal_rank = 1
        h = _compute_content_hash(outcome)
        assert len(h) == 32, f"Hash must be 32 chars, got {len(h)}"
        assert all(c in "0123456789abcdef" for c in h), f"Hash must be hex: {h}"

    def test_hash_excludes_meta_fields(self):
        """Content hash must NOT change when meta fields change."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=80.0)
        outcome = _compute_forward_metrics(
            bars=bars, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )
        outcome.selection_run_id = "hash-meta"
        outcome.symbol = "META"
        outcome.formal_rank = 1
        h1 = _compute_content_hash(outcome)

        # Change backfilled_at — hash must NOT change
        outcome.backfilled_at = "2026-08-10T00:00:00+00:00"
        h2 = _compute_content_hash(outcome)

        assert h1 == h2, "Content hash must exclude backfilled_at"

    def test_to_dict_from_dict_roundtrip(self):
        """OutcomeRecord to_dict/from_dict must be lossless."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=21, base_price=80.0)
        outcome = _compute_forward_metrics(
            bars=bars, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )
        outcome.selection_run_id = "round-trip"
        outcome.symbol = "RT"
        outcome.formal_rank = 1
        outcome.backfilled_at = "2026-08-05T00:00:00+00:00"
        outcome.content_hash = _compute_content_hash(outcome)

        d = outcome.to_dict()
        outcome2 = OutcomeRecord.from_dict(d)

        assert outcome2.selection_run_id == outcome.selection_run_id
        assert outcome2.symbol == outcome.symbol
        assert outcome2.price_5d == outcome.price_5d
        assert outcome2.return_21d_pct == outcome.return_21d_pct
        assert outcome2.mfe_21d_pct == outcome.mfe_21d_pct
        assert outcome2.mae_21d_pct == outcome.mae_21d_pct
        assert outcome2.range_outcome == outcome.range_outcome
        assert outcome2.content_hash == outcome.content_hash
        assert outcome2.evaluation_version == outcome.evaluation_version

    def test_entry_price_type_is_selection_reference(self):
        """All records must use 'selection_reference' as entry_price_type."""
        bars = _make_ohlcv_bars("2026-08-05", n_bars=10, base_price=80.0)
        outcome = _compute_forward_metrics(
            bars=bars, entry_price=80.0, selection_date_str="2026-08-05",
            range_low=75.0, range_high=85.0, provider="test",
        )
        assert outcome.entry_price_type == "selection_reference"
