"""Demo data provider for QuantCairn — deterministic, no external dependencies.

Returns realistic OHLCV DataFrames for a fixed set of 5 sample symbols using
a seeded pseudo-random generator.  Results are fully reproducible — same seed
always produces the same data.

Intended for:
  - Quick-start evaluation without API keys
  - Pipeline integration testing
  - Demo mode in public documentation

Never accesses:
  - Network / HTTP
  - Yahoo Finance
  - LongBridge or any broker API
  - Filesystem (except when explicitly called to read a cache)

Safety:
  - Cannot produce live orders (returns DataFrames only)
  - No broker integration
  - No side effects
"""

from __future__ import annotations

import hashlib
import random
from typing import Dict, List

import numpy as np
import pandas as pd

# ── Sample universe (5 well-known tickers) ───────────────────────────────────
DEMO_SYMBOLS: list[str] = ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]

# ── Base prices (roughly Q1 2026 levels) — used as the starting point for
#    the random walk ──────────────────────────────────────────────────────────
_BASE_PRICES: dict[str, float] = {
    "AAPL": 200.0,
    "MSFT": 450.0,
    "NVDA": 115.0,
    "SPY": 580.0,
    "TSLA": 250.0,
}

# ── Realistic daily volatility per symbol (approx annualised 25-45%) ─────────
_VOLATILITIES: dict[str, float] = {
    "AAPL": 0.018,
    "MSFT": 0.016,
    "NVDA": 0.032,
    "SPY": 0.012,
    "TSLA": 0.028,
}

# ── Sectors / asset type metadata ───────────────────────────────────────────
_DEMO_SECTORS: dict[str, str] = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Semiconductors",
    "SPY": "Index ETF",
    "TSLA": "Consumer Discretionary",
}

_DEMO_ASSET_TYPES: dict[str, str] = {
    "AAPL": "common_stock",
    "MSFT": "common_stock",
    "NVDA": "common_stock",
    "SPY": "etf",
    "TSLA": "common_stock",
}

# Number of trading days to generate
DEMO_HISTORY_ROWS: int = 252


def _seed_for_symbol(symbol: str, *, base_seed: int = 42) -> int:
    """Derive a deterministic per-symbol seed from the base seed + symbol name."""
    digest = hashlib.sha256(f"{base_seed}:{symbol}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def _generate_ohlcv(symbol: str, rows: int = DEMO_HISTORY_ROWS) -> pd.DataFrame:
    """Generate deterministic OHLCV data for one symbol via a seeded random walk.

    Parameters
    ----------
    symbol : str
        Ticker symbol (must be in DEMO_SYMBOLS).
    rows : int
        Number of trading days (default 252 ≈ 1 year).

    Returns
    -------
    pd.DataFrame
        Columns: Open, High, Low, Close, Volume.  Index: pd.DatetimeIndex.
        Never empty.  Never contains NaN.
    """
    if symbol not in _BASE_PRICES:
        raise KeyError(
            f"Unknown demo symbol '{symbol}'. "
            f"Available: {', '.join(sorted(_BASE_PRICES))}"
        )

    base_price = _BASE_PRICES[symbol]
    daily_vol = _VOLATILITIES[symbol]
    seed = _seed_for_symbol(symbol)
    rng = random.Random(seed)
    np_rng = np.random.RandomState(seed)

    # Generate business-day date range ending "today"
    end_date = pd.Timestamp.now().normalize()
    dates = pd.bdate_range(end=end_date, periods=rows)
    if len(dates) < rows:
        # If we somehow get fewer, extend backwards
        dates = pd.bdate_range(end=end_date, periods=rows)

    # Random walk: log-returns then cumulative product
    log_returns = np_rng.normal(loc=0.0, scale=daily_vol, size=rows)
    # First day starts at base_price
    log_returns[0] = 0.0
    closes = base_price * np.exp(np.cumsum(log_returns))

    # Generate OHLC around Close
    opens = np.zeros(rows)
    highs = np.zeros(rows)
    lows = np.zeros(rows)

    for i in range(rows):
        intraday_range = closes[i] * daily_vol * abs(np_rng.normal(0.5, 0.3))
        gap = closes[i] * daily_vol * np_rng.normal(0, 0.3)
        opens[i] = closes[i] + gap if i > 0 else closes[i] - gap
        highs[i] = max(opens[i], closes[i]) + intraday_range * rng.random()
        lows[i] = min(opens[i], closes[i]) - intraday_range * rng.random()

    opens = np.maximum(opens, 0.01)
    highs = np.maximum(highs, opens)
    lows = np.minimum(lows, opens)
    closes = np.maximum(closes, 0.01)

    # Volume: log-normal, scaled by symbol
    base_vol = {"AAPL": 55e6, "MSFT": 25e6, "NVDA": 220e6, "SPY": 75e6, "TSLA": 85e6}[symbol]
    volumes = np.maximum(
        np_rng.lognormal(mean=np.log(base_vol), sigma=0.4, size=rows),
        1_000_000,
    ).astype(np.int64)

    df = pd.DataFrame(
        {
            "Open": np.round(opens, 2),
            "High": np.round(highs, 2),
            "Low": np.round(lows, 2),
            "Close": np.round(closes, 2),
            "Volume": volumes,
        },
        index=dates,
    )
    df.index.name = "Date"
    return df


class DemoDataProvider:
    """Deterministic in-memory OHLCV provider for demo mode.

    Usage::

        provider = DemoDataProvider()
        df = provider.get_ohlcv("AAPL")         # 252 rows
        all_data = provider.get_all_ohlcv()      # {symbol: DataFrame}
        symbols = provider.symbols               # ["AAPL", "MSFT", ...]
    """

    def __init__(self) -> None:
        self._cache: dict[str, pd.DataFrame] = {}

    @property
    def symbols(self) -> list[str]:
        """The 5 demo universe symbols."""
        return list(DEMO_SYMBOLS)

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        """Return OHLCV DataFrame for *symbol* (cached after first call)."""
        if symbol not in self._cache:
            self._cache[symbol] = _generate_ohlcv(symbol)
        return self._cache[symbol].copy()

    def get_all_ohlcv(self) -> dict[str, pd.DataFrame]:
        """Return {symbol: DataFrame} for all demo symbols."""
        return {sym: self.get_ohlcv(sym) for sym in DEMO_SYMBOLS}

    def get_demo_universe(self) -> list[str]:
        """Alias for symbols — used by demo selector integration."""
        return self.symbols

    def sector_for(self, symbol: str) -> str:
        """Return the sector label for a demo symbol."""
        return _DEMO_SECTORS.get(symbol, "Unknown")

    def asset_type_for(self, symbol: str) -> str:
        """Return the asset type for a demo symbol."""
        return _DEMO_ASSET_TYPES.get(symbol, "common_stock")

    def price_at(self, symbol: str, offset: int = -1) -> float | None:
        """Return the Close price at the given row offset from the end.

        offset=-1 → most recent close.
        """
        df = self.get_ohlcv(symbol)
        if df.empty:
            return None
        return float(df["Close"].iloc[offset])

    def daily_volume_at(self, symbol: str, offset: int = -1) -> float:
        """Return the Volume at the given row offset."""
        df = self.get_ohlcv(symbol)
        if df.empty:
            return 0.0
        return float(df["Volume"].iloc[offset])


# ── Module-level singleton convenience ───────────────────────────────────────
_demo_provider: DemoDataProvider | None = None


def get_demo_provider() -> DemoDataProvider:
    """Return a module-level DemoDataProvider singleton."""
    global _demo_provider
    if _demo_provider is None:
        _demo_provider = DemoDataProvider()
    return _demo_provider
