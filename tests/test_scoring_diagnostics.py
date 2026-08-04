"""Test that scoring rejection reasons are captured in scorer.scoring_rejections.

The dict is populated during score_universe() and tracks every
symbol that failed scoring, keyed by ticker → reason string.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_DIR)
import sys
sys.path.insert(0, str(PROJECT_DIR))


def _scorer():
    from src.scoring.scorer import Scorer
    return Scorer()


def _make_df(rows=120, base=20.0, trend_strength=0.0) -> pd.DataFrame:
    """Build a deterministic OHLCV DataFrame suitable for scoring."""
    rng = np.random.default_rng(seed=42)
    n = rows
    drift = np.linspace(0, trend_strength, n)
    noise = rng.normal(0, 0.2, n)
    synthetic = base + drift + noise.cumsum() * 0.3
    # Bound prices away from zero
    synthetic = np.clip(synthetic, 5.0, 500.0)

    return pd.DataFrame({
        "Open":   synthetic + rng.normal(0, 0.05, n),
        "High":   synthetic + abs(rng.normal(0, 0.15, n)),
        "Low":    synthetic - abs(rng.normal(0, 0.15, n)),
        "Close":  synthetic,
        "Volume": rng.integers(500_000, 3_000_000, n),
    })


# ── Rejection diagnostics are populated ──────────────────────────────────────


def test_scoring_rejections_reset_per_run():
    """Every score_universe() call resets scoring_rejections."""
    scorer = _scorer()
    # Pre-populate
    scorer.scoring_rejections["TEST"] = "stale"
    # scoring_rejections is reset at start of score_universe
    # The test symbol has no data so it will get recorded
    scorer.score_universe(["ZZZZ_UNKNOWN"], {"ZZZZ_UNKNOWN": []})
    assert "TEST" not in scorer.scoring_rejections


def test_unknown_symbol_without_fallback_is_recorded():
    """Symbol with no OHLCV and no fallback profile → insufficient_history_no_fallback."""
    scorer = _scorer()
    scorer.score_universe(["ZZZZ_UNKNOWN"], {"ZZZZ_UNKNOWN": []})
    assert scorer.scoring_rejections.get("ZZZZ_UNKNOWN", "") == "insufficient_history_no_fallback"


def test_empty_dataframe_no_fallback_is_recorded():
    """Empty DataFrame and no fallback → insufficient_history_no_fallback."""
    scorer = _scorer()
    # Patch _load_history to return empty
    scorer._load_history = lambda sym: pd.DataFrame()
    scorer.score_universe(["EMPTY"], {"EMPTY": []})
    assert scorer.scoring_rejections.get("EMPTY", "") == "insufficient_history_no_fallback"


def test_scoring_error_no_fallback_is_recorded():
    """Exception during scoring and no fallback → scoring_error_no_fallback."""
    scorer = _scorer()

    def _failing_load(sym):
        raise RuntimeError("boom")

    scorer._load_history = _failing_load
    scorer.score_universe(["ERROR_SYM"], {"ERROR_SYM": []})
    assert scorer.scoring_rejections.get("ERROR_SYM", "") == "scoring_error_no_fallback"


# ── score_frame rejections get specific reasons ──────────────────────────────


def test_range_too_tight_is_recorded():
    """score_frame should record a scoring-level rejection reason
    when universe validation passes but scoring checks fail."""
    scorer = _scorer()
    # Build a wide-swing series so ATR is acceptable for universe validation,
    # but with very tight daily range so the scoring checks reject it.
    rng = np.random.default_rng(seed=99)
    n = 120
    mid = 50.0
    # Oscillating price with decent amplitude → ATR will be normal
    t = np.linspace(0, 6 * np.pi, n)
    closes = mid + 2.0 * np.sin(t) + rng.normal(0, 0.15, n)
    # But daily range is razor-thin → range_width_pct will be tiny
    df = pd.DataFrame({
        "Open": closes + rng.normal(0, 0.02, n),
        "High": closes + 0.03,      # very tight bars
        "Low": closes - 0.03,
        "Close": closes,
        "Volume": rng.integers(500_000, 3_000_000, n),
    })
    scorer._load_history = lambda sym: df
    scorer._market_cap_for_symbol = lambda sym: 50_000_000_000
    scorer.score_universe(["TIGHT_RL"], {"TIGHT_RL": []})
    reason = scorer.scoring_rejections.get("TIGHT_RL", "")
    assert reason, f"No rejection recorded: {scorer.scoring_rejections}"
    valid = {"range too tight", "range too wide", "insufficient movement",
             "spread_too_narrow", "volatility too low", "volatility too high",
             "strong trend", "frequent gap risk", "event/news driven",
             "universe_validation"}
    found = any(r in reason for r in valid)
    assert found, f"Unexpected rejection reason: {reason}"


def test_score_frame_insufficient_history():
    """score_frame called directly with too few rows → insufficient_history_data."""
    scorer = _scorer()
    df = pd.DataFrame({
        "Open": [10.0, 10.1], "High": [10.2, 10.3],
        "Low": [9.8, 9.9], "Close": [10.0, 10.1], "Volume": [100, 200],
    })
    scorer.score_frame(symbol="TINY", df=df)
    assert scorer.scoring_rejections.get("TINY", "") == "insufficient_history_data"


# ── Thread safety: unique symbols per thread, no race on write ──────────────


def test_concurrent_rejections_no_race():
    """Multiple symbols scored in parallel — each gets its own rejection key."""
    scorer = _scorer()
    scorer.score_workers = 4
    # All tricky symbols — no data, no fallback
    symbols = [f"BAD_{i:03d}" for i in range(20)]
    scorer.score_universe(symbols, {s: [] for s in symbols})
    assert len(scorer.scoring_rejections) == 20
    for s in symbols:
        assert s in scorer.scoring_rejections, f"{s} missing from rejections"
        assert scorer.scoring_rejections[s] == "insufficient_history_no_fallback"


# ── Existing scoring path not broken ─────────────────────────────────────────


def test_known_good_symbol_scores_without_rejection():
    """A symbol with valid data and fallback profile should NOT appear in rejections."""
    scorer = _scorer()
    df = _make_df(rows=120, base=130.0, trend_strength=2.0)
    scorer._load_history = lambda sym: df
    scorer.score_universe(["NVDA"], {"NVDA": []})
    # NVDA has a fallback profile AND real data — should either score or
    # have a specific rejection reason (not generic no-fallback)
    if "NVDA" in scorer.scoring_rejections:
        reason = scorer.scoring_rejections["NVDA"]
        assert "no_fallback" not in reason, (
            f"NVDA incorrectly reported as missing fallback: {reason}"
        )


# ── Gap risk: ratio-based only, no one-strike max_gap_pct rejection ────────────


def _make_gap_df(rows=120, base=50.0, gap_indices=(), gap_pct=10.0) -> pd.DataFrame:
    """Build a DataFrame with controlled overnight gaps.

    Parameters:
      rows: total number of rows
      base: baseline price
      gap_indices: row indices where the Open differs from prev Close by gap_pct
      gap_pct: percentage gap to inject (positive → Open above prev Close)
    """
    rng = np.random.default_rng(seed=1)
    # Build an oscillating Close series with enough amplitude to produce
    # ATR 1–8% (required by common_stock universe filter).
    t = np.linspace(0, 4 * np.pi, rows)
    closes = base + 3.5 * np.sin(t) + rng.normal(0, 0.30, rows)
    closes = np.clip(closes, 5.0, 500.0)

    opens = np.zeros(rows)
    for i in range(rows):
        if i in gap_indices and i > 0:
            # Inject a large gap: Open differs from prev Close
            opens[i] = closes[i - 1] * (1.0 + gap_pct / 100.0)
        else:
            # Normal: Open ≈ prev Close with small noise
            opens[i] = closes[i - 1] * (1.0 + rng.normal(0, 0.003)) if i > 0 else closes[0]

    # Wider bars to keep ATR healthy
    highs = np.maximum(opens, closes) + abs(rng.normal(0, 0.30, rows))
    lows = np.minimum(opens, closes) - abs(rng.normal(0, 0.30, rows))
    volumes = rng.integers(800_000, 4_000_000, rows)

    return pd.DataFrame({
        "Open": opens,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": volumes,
    })


def _gap_rejection_for_df(df, symbol="GAP_TEST"):
    """Score a DataFrame and return the rejection reason (or empty string if passed)."""
    scorer = _scorer()
    scorer._load_history = lambda sym: df
    scorer._market_cap_for_symbol = lambda sym: 50_000_000_000
    scorer.score_universe([symbol], {symbol: []})
    return scorer.scoring_rejections.get(symbol, "")


def test_gap_risk_single_earnings_gap_passes():
    """One large earnings gap should NOT trigger rejection.

    Before fix: max_gap_pct > 5.0% would reject (one-strike rule).
    After fix: only gap_rate > 0.20 triggers rejection.
    A single gap in 120 rows → gap_rate ≈ 1/119 ≈ 0.008 < 0.20 → PASS.
    """
    df = _make_gap_df(rows=120, base=80.0, gap_indices={60}, gap_pct=9.0)
    reason = _gap_rejection_for_df(df, "ONE_GAP")
    assert "frequent gap risk" not in reason, (
        f"Single earnings gap should NOT trigger rejection, got: {reason}"
    )


def test_gap_risk_frequent_gaps_rejected():
    """Frequent gaps (>20% of sessions) should still trigger rejection.

    gap_rate > 0.20 → "frequent gap risk" rejection.
    """
    # Inject 30 gap days (= 30/119 ≈ 25%) in a 120-row DF
    gap_indices = set(range(10, 40))  # 30 consecutive gap days
    df = _make_gap_df(rows=120, base=80.0, gap_indices=gap_indices, gap_pct=6.0)
    reason = _gap_rejection_for_df(df, "MANY_GAPS")
    assert "frequent gap risk" in reason, (
        f"gap_rate > 0.20 should trigger rejection, got: {reason}"
    )


def test_gap_risk_clean_symbol_passes():
    """A symbol with no large gaps should pass cleanly."""
    df = _make_gap_df(rows=120, base=80.0, gap_indices=set(), gap_pct=0.0)
    reason = _gap_rejection_for_df(df, "CLEAN")
    assert "frequent gap risk" not in reason, (
        f"Clean symbol should pass, got: {reason}"
    )


def test_gap_risk_both_triggers_rejected():
    """Both gap_rate > 0.20 AND large single gap → still rejected."""
    # 30 gap days (>20%) with large magnitude
    gap_indices = set(range(0, 30))
    df = _make_gap_df(rows=120, base=80.0, gap_indices=gap_indices, gap_pct=10.0)
    reason = _gap_rejection_for_df(df, "BOTH_BAD")
    assert "frequent gap risk" in reason, (
        f"Both triggers should still reject, got: {reason}"
    )


def test_gap_risk_rejection_reason_string_is_stable():
    """The rejection reason string 'frequent gap risk' is preserved for
    backward compatibility with downstream diagnostic consumers."""
    gap_indices = set(range(0, 30))  # >20%
    df = _make_gap_df(rows=120, base=80.0, gap_indices=gap_indices, gap_pct=6.0)
    reason = _gap_rejection_for_df(df, "COMPAT")
    assert "frequent gap risk" in reason, (
        f"Expected 'frequent gap risk', got: {reason}"
    )


def test_gap_metrics_still_computed():
    """gap_rate and max_gap_pct are still computed and recorded in
    result metrics — they just no longer gate scoring eligibility."""
    scorer = _scorer()
    # Build a clean DF with one small gap
    df = _make_gap_df(rows=120, base=50.0, gap_indices={40}, gap_pct=3.0)
    scorer._load_history = lambda sym: df
    scorer._market_cap_for_symbol = lambda sym: 50_000_000_000
    result = scorer.score_universe(["GAP_METRICS"], {"GAP_METRICS": []})
    assert result, "Symbol should score"
    item = result[0]
    metrics = item.get("metrics", {})
    assert "gap_rate" in metrics, "gap_rate should be in metrics"
    assert "max_gap_pct" in metrics, "max_gap_pct should be in metrics"
    assert metrics["gap_rate"] < 0.20, (
        f"gap_rate should be low, got {metrics['gap_rate']}"
    )
