"""
Price data fetcher: wraps yfinance for real-time and historical data.
"""
import time
import math
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
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
        self._synthetic_market = os.environ.get("SOXS_SYNTHETIC_MARKET", "").strip().lower() in {"1", "true", "yes", "on"}
        self._synthetic_start_price = _positive_float(os.environ.get("SOXS_SYNTHETIC_START_PRICE"), 100.0)
        self._synthetic_amplitude_pct = _positive_float(os.environ.get("SOXS_SYNTHETIC_AMPLITUDE_PCT"), 2.0)
        self._synthetic_period_seconds = max(15, int(_positive_float(os.environ.get("SOXS_SYNTHETIC_PERIOD_SECONDS"), 120.0)))
        self._synthetic_started_at = time.time()

    def _call_with_retries(self, func, attempts: int = 3, base_delay: float = 0.5):
        """Call *func* with retries and exponential backoff.

        This wrapper protects against transient yfinance/http failures while
        preserving the original exception when retries are exhausted.
        """
        last_exc = None
        for i in range(attempts):
            try:
                return func()
            except Exception as e:
                last_exc = e
                logger.debug(
                    "yfinance call failed (attempt %d/%d) for %s: %s",
                    i + 1,
                    attempts,
                    self.ticker,
                    e,
                )
                time.sleep(base_delay * (2 ** i))
        raise last_exc

    def _get_safe_fast_info(self) -> dict[str, float]:
        """Safely extract numeric fast_info fields without triggering lazy yfinance fetches."""
        try:
            fast = self._call_with_retries(lambda: self._ticker_obj.fast_info, attempts=1, base_delay=0.25)
        except Exception as e:
            logger.debug("fast_info unavailable for %s: %s", self.ticker, e)
            return {}

        result = {}
        for key in (
            "regularMarketPreviousClose",
            "bid",
            "ask",
            "lastPrice",
            "regularMarketPrice",
            "lastVolume",
        ):
            try:
                value = fast.get(key) if hasattr(fast, "get") else getattr(fast, key, None)
            except Exception as e:
                logger.debug("fast_info field %s failed for %s: %s", key, self.ticker, e)
                value = None
            result[key] = value

        return result

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

    def _fetch_chart_quote(self) -> dict:
        """Fetch a lightweight Yahoo chart quote without inherited proxy settings."""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{self.ticker}"
        session = requests.Session()
        session.trust_env = False
        last_exc = None
        for attempt in range(3):
            try:
                resp = session.get(
                    url,
                    params={"range": "1d", "interval": "1m", "includePrePost": "true"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=8,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                last_exc = e
                time.sleep(0.25 * (2 ** attempt))
        else:
            logger.debug("Yahoo chart fallback failed for %s: %s", self.ticker, last_exc)
            return {}

        try:
            result = ((data.get("chart") or {}).get("result") or [None])[0]
            if not result:
                return {}

            meta = result.get("meta") or {}
            quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}

            def _last(values):
                if not values:
                    return None
                for value in reversed(values):
                    if value is not None:
                        return value
                return None

            price = _positive_float(meta.get("regularMarketPrice"))
            if price <= 0:
                price = _positive_float(_last(quote.get("close")))

            return {
                "price": price,
                "previous_close": _positive_float(
                    meta.get("previousClose"),
                    _positive_float(meta.get("chartPreviousClose")),
                ),
                "volume": int(_positive_float(
                    _last(quote.get("volume")),
                    _positive_float(meta.get("regularMarketVolume")),
                )),
                "high": _positive_float(
                    _last(quote.get("high")),
                    _positive_float(meta.get("regularMarketDayHigh")),
                ),
                "low": _positive_float(
                    _last(quote.get("low")),
                    _positive_float(meta.get("regularMarketDayLow")),
                ),
            }
        except Exception as e:
            logger.debug("Yahoo chart fallback parse failed for %s: %s", self.ticker, e)
            return {}

    def _synthetic_quote(self) -> Quote:
        """Generate a deterministic synthetic quote when all live data sources fail."""
        now = time.time()
        if self._cached_quote and self._cached_quote.price > 0:
            base = self._synthetic_start_price or self._cached_quote.price
        else:
            base = self._synthetic_start_price

        elapsed = now - self._synthetic_started_at
        phase = (elapsed / self._synthetic_period_seconds) * 2 * math.pi
        swing = math.sin(phase)
        amplitude = base * (self._synthetic_amplitude_pct / 100.0)
        price = max(0.01, base + amplitude * swing)
        spread = max(price * 0.001, 0.01)
        prev_close = self._cached_quote.price if self._cached_quote else base
        quote = Quote(
            ticker=self.ticker,
            price=round(price, 4),
            bid=round(price - spread, 4),
            ask=round(price + spread, 4),
            volume=int(100000 + abs(swing) * 50000),
            change_pct=round(((price - prev_close) / prev_close * 100) if prev_close else 0.0, 2),
            timestamp=datetime.now(),
            high_1m=round(price + spread, 4),
            low_1m=round(price - spread, 4),
        )
        self._cached_quote = quote
        self._last_fetch_time = now
        return quote

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
            chart = self._fetch_chart_quote()
            fast = {} if _positive_float(chart.get("price")) > 0 else self._get_safe_fast_info()

            prev_close = _positive_float(
                chart.get("previous_close"),
                _positive_float(fast.get("regularMarketPreviousClose")),
            )
            bid = _positive_float(fast.get("bid"))
            ask = _positive_float(fast.get("ask"))

            price = _positive_float(chart.get("price"))
            volume = int(_positive_float(chart.get("volume")))
            high_1m = chart.get("high") or None
            low_1m = chart.get("low") or None

            if price > 0:
                if bid <= 0:
                    bid = price
                if ask <= 0:
                    ask = price

            # ── Layer 1: 5-day 1-minute history (works during & after hours) ──
            if price <= 0:
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

            # ── Layer 1d: fallback to daily history for tickers with no recent intraday bars ──
            if price <= 0:
                try:
                    hist = self._fetch_history(period="max", interval="1d", prepost=False)
                    if hist is not None and not hist.empty:
                        last_row = hist.iloc[-1]
                        price = float(last_row["Close"])
                        volume = int(_positive_float(last_row.get("Volume", 0)))
                        high_1m = float(last_row["High"])
                        low_1m = float(last_row["Low"])
                except Exception:
                    pass

            # ── Layer 2: pre-market / post-market prices ──
            if price <= 0:
                try:
                    price_info = self._call_with_retries(lambda: self._ticker_obj.info, attempts=1, base_delay=0.25)
                except Exception as e:
                    logger.debug("info unavailable for %s: %s", self.ticker, e)
                    price_info = {}
                if price_info is None:
                    price_info = {}

                pre = _positive_float(price_info.get("preMarketPrice"))
                post = _positive_float(price_info.get("postMarketPrice"))
                if pre > 0:
                    price = pre
                elif post > 0:
                    price = post

            # ── Layer 3: fast_info ──
            if price <= 0:
                price = _positive_float(
                    fast.get("lastPrice"),
                    _positive_float(fast.get("regularMarketPrice")),
                )
                volume = int(_positive_float(fast.get("lastVolume")))

            # ── Layer 4: synthetic fallback or stale cache ──
            if price <= 0 and self._synthetic_market:
                return self._synthetic_quote()
            if price <= 0 and self._cached_quote:
                return self._cached_quote

            if prev_close <= 0:
                prev_close = price
            if bid <= 0 and price > 0:
                bid = price
            if ask <= 0 and price > 0:
                ask = price

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
            if self._synthetic_market:
                return self._synthetic_quote()
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
