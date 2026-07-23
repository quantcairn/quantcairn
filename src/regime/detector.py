"""Market Regime Detector — fetches index data and computes indicators.

Reads SPY, QQQ, IWM OHLCV + VIX quote via PriceFetcher.
Computes price_vs_MA, RSI, ATR, returns, volume ratios.

Advisory only — never touches trading state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.data.fetcher import PriceFetcher
from src.regime.models import MarketSnapshot, RegimeState

# Market indices
PRIMARY_INDICES = ("SPY", "QQQ", "IWM")
VIX_SYMBOL = "^VIX"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return default
    if result != result or result in {float("inf"), float("-inf")}:
        return default
    return round(result, 4)


def _compute_rsi(closes: list[float], period: int = 14) -> float:
    """Compute Wilder's RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _compute_sma(values: list[float], window: int) -> float:
    if len(values) < window:
        return 0.0
    return round(sum(values[-window:]) / window, 4)


def _compute_atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Compute ATR as percentage of price for comparability across indices."""
    if len(closes) < period + 1:
        return 0.0
    tr_sum = 0.0
    for i in range(-period, 0):
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_sum += tr
    atr = tr_sum / period
    last_close = closes[-1] if closes else 1.0
    return round((atr / max(last_close, 0.01)) * 100.0, 4)


def fetch_market_snapshot(symbol: str) -> MarketSnapshot | None:
    """Fetch OHLCV data for a single index symbol and compute indicators."""
    fetcher = PriceFetcher(symbol, poll_interval=0)
    try:
        candles = fetcher.get_ohlcv(period="6mo", interval="1d")
    finally:
        fetcher.close()

    if len(candles) < 50:
        return None

    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    volumes = [float(c.volume) for c in candles]

    price = closes[-1]
    ma20 = _compute_sma(closes, 20)
    ma50 = _compute_sma(closes, 50)
    ma200 = _compute_sma(closes, 200)
    rsi = _compute_rsi(closes)
    atr_pct = _compute_atr_pct(highs, lows, closes)
    ret_5d = round((closes[-1] - closes[-5]) / max(closes[-5], 0.01) * 100.0, 4) if len(closes) >= 5 else 0.0
    ret_20d = round((closes[-1] - closes[-20]) / max(closes[-20], 0.01) * 100.0, 4) if len(closes) >= 20 else 0.0
    avg_vol_20 = _compute_sma(volumes, 20)
    vol_ratio = round(volumes[-1] / max(avg_vol_20, 1.0), 4) if avg_vol_20 > 0 else 1.0

    return MarketSnapshot(
        symbol=symbol, price=price, ma20=ma20, ma50=ma50, ma200=ma200,
        rsi=rsi, atr_pct=atr_pct, return_5d_pct=ret_5d,
        return_20d_pct=ret_20d, volume_sma20_ratio=vol_ratio,
    )


def fetch_vix() -> tuple[float, float]:
    """Return (VIX, VIX_change_pct) or (0, 0) on failure."""
    fetcher = PriceFetcher(VIX_SYMBOL, poll_interval=0)
    try:
        candles = fetcher.get_ohlcv(period="1mo", interval="1d")
    finally:
        fetcher.close()
    if len(candles) < 3:
        return 0.0, 0.0
    closes = [float(c.close) for c in candles]
    current = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else current
    change = round((current - prev) / max(prev, 0.01) * 100.0, 4) if prev > 0 else 0.0
    return current, change


def detect_regime(*, dry_run: bool = False) -> RegimeState:
    """Fetch market data and classify the current regime.

    Returns a fully-populated RegimeState with all indicator values and signals.
    Never writes any trading state.
    """
    from src.regime.classifier import classify

    state = RegimeState()
    indices: dict[str, dict[str, Any]] = {}

    for sym in PRIMARY_INDICES:
        snap = fetch_market_snapshot(sym)
        if snap is not None:
            indices[sym] = snap.to_dict()

    if not indices:
        state.signals.append("no_index_data_available")
        return state

    vix, vix_change = fetch_vix()
    state.vix = vix
    state.vix_change_pct = vix_change

    # Compute composite market return
    returns_5d = [d["return_5d_pct"] for d in indices.values()]
    returns_20d = [d["return_20d_pct"] for d in indices.values()]
    state.market_return_5d = round(sum(returns_5d) / len(returns_5d), 4) if returns_5d else 0.0
    state.market_return_20d = round(sum(returns_20d) / len(returns_20d), 4) if returns_20d else 0.0
    state.indices = indices

    # Classify
    classify(state)

    return state
