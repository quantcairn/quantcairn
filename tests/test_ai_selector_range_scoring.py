import numpy as np
import pandas as pd

from src.scoring.scorer import Scorer


def _make_range_like_df(rows: int = 120, base: float = 100.0) -> pd.DataFrame:
    x = np.linspace(0, 8 * np.pi, rows)
    close = base + np.sin(x) * 3.0 + np.sin(x * 0.25) * 0.8
    open_ = close + np.sin(x * 1.3) * 0.2
    high = np.maximum(open_, close) + 1.8
    low = np.minimum(open_, close) - 1.8
    volume = np.full(rows, 2_500_000.0) + (np.sin(x * 0.7) + 1.0) * 180_000
    return pd.DataFrame({
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume,
    })


def _make_trend_df(rows: int = 120, base: float = 100.0, slope: float = 0.8) -> pd.DataFrame:
    close = base + np.arange(rows) * slope
    open_ = close - 0.3
    high = close + 0.8
    low = close - 1.0
    volume = np.full(rows, 3_000_000.0)
    return pd.DataFrame({
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume,
    })


def _make_flat_df(rows: int = 120, base: float = 100.0) -> pd.DataFrame:
    close = np.full(rows, base) + np.sin(np.linspace(0, 2 * np.pi, rows)) * 0.2
    open_ = close
    high = close + 0.15
    low = close - 0.15
    volume = np.full(rows, 2_000_000.0)
    return pd.DataFrame({
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume,
    })


def test_range_like_series_scores_and_generates_range():
    scorer = Scorer()
    result = scorer.score_frame("TEST", _make_range_like_df(), news_items=[], sector="Technology")

    assert result is not None
    assert result["score"] > 0
    assert result["range_low"] < result["range_high"]
    assert result["suggested_range"].startswith("$")
    assert result["metrics"]["atr_pct"] > 0


def test_strong_trend_is_filtered_out():
    scorer = Scorer()
    result = scorer.score_frame("TREND", _make_trend_df(), news_items=[], sector="Technology")

    assert result is None


def test_flat_series_is_filtered_out():
    scorer = Scorer()
    result = scorer.score_frame("FLAT", _make_flat_df(), news_items=[], sector="Utilities")

    assert result is None


def test_high_price_series_is_filtered_out():
    scorer = Scorer()
    result = scorer.score_frame("EXPENSIVE", _make_range_like_df(base=120.0), news_items=[], sector="Technology")

    assert result is None
