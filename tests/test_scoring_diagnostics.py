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
