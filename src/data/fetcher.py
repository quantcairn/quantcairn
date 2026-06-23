"""
Price data fetcher: wraps yfinance for real-time and historical data.
"""
import time
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


def _positive_float(value, default: float = 0.0) -> float:
    """Return value as float only when it is positive and numeric."""
    try:
        if value is None:
            return default
        number = float(value)
        return number if number > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass
class Quote:
    """Real-time price snapshot."""
    ticker: str
    price: float
    bid: float
    ask: float
    volume: int
    change_pct: float
    timestamp: datetime
    high_1m: Optional[float] = None
    low_1m: Optional[float] = None


@dataclass
class OHLCV:
    """Candlestick data point."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class PriceFetcher:
    """Fetches real-time and historical price data for a ticker via yfinance."""

    def __init__(self, ticker: str, poll_interval: int = 15):
        self.ticker = ticker
        self.poll_interval = poll_interval
        self._ticker_obj = yf.Ticker(ticker)
        self._last_fetch_time: float = 0
        self._cached_quote: Optional[Quote] = None

    def _call_with_retries(self, func, attempts: int = 3, base_delay: float = 0.5):
        """Call *func* with retries and exponential backoff. Returns result or
        raises the last exception if all attempts fail.
        """
        last_exc = None
        for i in range(attempts):
            try:
                return func()
            except Exception as e:
                last_exc = e
                logger.debug("yfinance call failed (attempt %d/%d) for %s: %s", i + 1, attempts, self.ticker, e)
                time.sleep(base_delay * (2 ** i))
        raise last_exc

    def _fetch_history(self, period: str, interval: str, prepost: bool = True):
        """Fetch historical data with retries and pre/post-market support."""
        try:
            return self._call_with_retries(
                lambda: self._ticker_obj.history(period=period, interval=interval, prepost=prepost),
                attempts=3,
                base_delay=0.25,
            )
        except Exception as e:
            logger.debug("History fetch failed for %s (%s %s): %s", self.ticker, period, interval, e)
            return None

    def get_quote(self) -> Optional[Quote]:
        """Get current real-time quote. Cached for poll_interval seconds.

        Pricing priority (best → worst):
        1. 1-minute history Close  → freshest, for regular hours
        2. preMarketPrice / postMarketPrice → extended hours
        3. fast_info lastPrice → last resort
        """
        now = time.time()
        if self._cached_quote and (now - self._last_fetch_time) < self.poll_interval:
            return self._cached_quote

        try:
            # fast_info may intermittently raise; use retries to reduce noise
            try:
                fast = self._call_with_retries(lambda: self._ticker_obj.fast_info, attempts=2, base_delay=0.25)
            except Exception:
                fast = {}

            prev_close = _positive_float(fast.get("regularMarketPreviousClose"))
            bid = _positive_float(fast.get("bid"))
            ask = _positive_float(fast.get("ask"))

            price = 0.0
            volume = 0
            high_1m = None
            low_1m = None

            # ── Layer 1: 5-day 1-minute history (works during & after hours) ──
            try:
                hist = self._fetch_history(period="5d", interval="1m", prepost=True)
                if hist is not None and not hist.empty:
                    last_row = hist.iloc[-1]
                    price = float(last_row["Close"])
                    volume = int(last_row["Volume"])
                    high_1m = float(last_row["High"])
                    low_1m = float(last_row["Low"])
            except Exception:
                pass

            # ── Layer 1b: fallback to 1d 5m history if 1m unavailable ──
            if price <= 0:
                try:
                    hist = self._fetch_history(period="1d", interval="5m", prepost=True)
                    if hist is not None and not hist.empty:
                        last_row = hist.iloc[-1]
                        price = float(last_row["Close"])
                        volume = int(last_row["Volume"])
                        high_1m = float(last_row["High"])
                        low_1m = float(last_row["Low"])
                except Exception:
                    pass

            # ── Layer 1c: fallback to 1d 1m history if 5m unavailable ──
            if price <= 0:
                try:
                    hist = self._fetch_history(period="1d", interval="1m", prepost=True)
                    if hist is not None and not hist.empty:
                        last_row = hist.iloc[-1]
                        price = float(last_row["Close"])
                        volume = int(last_row["Volume"])
                        high_1m = float(last_row["High"])
                        low_1m = float(last_row["Low"])
                except Exception:
                    pass

            # ── Layer 2: pre-market / post-market prices ──
            if price <= 0:
                try:
                    try:
                        info = self._call_with_retries(lambda: self._ticker_obj.info, attempts=2, base_delay=0.25)
                    except Exception:
                        info = {}

                    pre = _positive_float(info.get("preMarketPrice"))
                    post = _positive_float(info.get("postMarketPrice"))
                    if pre > 0:
                        price = pre
                    elif post > 0:
                        price = post
                except Exception:
                    pass

            # ── Layer 3: fast_info ──
            if price <= 0:
                price = _positive_float(
                    fast.get("lastPrice"),
                    _positive_float(fast.get("regularMarketPrice")),
                )
                volume = int(_positive_float(fast.get("lastVolume")))

            # ── Layer 4: stale cache ──
            if price <= 0 and self._cached_quote:
                return self._cached_quote

            if prev_close <= 0:
                prev_close = price

            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

            quote = Quote(
                ticker=self.ticker,
                price=round(float(price), 4),
                bid=float(bid),
                ask=float(ask),
                volume=int(volume),
                change_pct=round(float(change_pct), 2),
                timestamp=datetime.now(),
                high_1m=high_1m,
                low_1m=low_1m,
            )

            self._cached_quote = quote
            self._last_fetch_time = now
            return quote

        except Exception as e:
            logger.warning(f"Failed to fetch quote for {self.ticker}: {e}")
            return self._cached_quote  # Return stale cache if available

    def get_ohlcv(self, period: str = "1d", interval: str = "5m") -> list[OHLCV]:
        """Get historical OHLCV data.

        Args:
            period: yfinance period string (1d, 5d, 1mo, etc.)
            interval: yfinance interval string (1m, 5m, 15m, 1h, etc.)
        """
        hist = self._fetch_history(period=period, interval=interval, prepost=True)
        if hist is None or hist.empty:
            logger.error(f"Failed to fetch OHLCV for {self.ticker}: no data returned")
            return []

        candles = []
        for idx, row in hist.iterrows():
            try:
                candles.append(OHLCV(
                    timestamp=idx.to_pydatetime(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                ))
            except Exception as e:
                logger.debug("Skipping invalid OHLCV row for %s: %s", self.ticker, e)
        return candles

    def get_recent_range(self, lookback_bars: int = 50, interval: str = "5m") -> tuple[float, float]:
        """Calculate recent high/low range from last N bars."""
        candles = self.get_ohlcv(period="5d", interval=interval)
        if len(candles) < lookback_bars:
            lookback_bars = len(candles)

        recent = candles[-lookback_bars:]
        highs = [c.high for c in recent]
        lows = [c.low for c in recent]

        if not highs or not lows:
            return 0, 0

        return min(lows), max(highs)

    def force_refresh(self):
        """Force next get_quote() to fetch fresh data."""
        self._last_fetch_time = 0
