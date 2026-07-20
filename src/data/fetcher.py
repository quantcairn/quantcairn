"""
Price data fetcher: wraps yfinance for real-time and historical data.
"""
import time
import math
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional

import requests
import yfinance as yf

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_YFINANCE_CACHE_DIR = Path(
    os.environ.get("SOXS_YFINANCE_CACHE_DIR", str(PROJECT_DIR / "state" / "yfinance_cache"))
).expanduser().resolve()
_YFINANCE_CACHE_LOCK = Lock()
_YFINANCE_CACHE_INITIALIZED = False
_YFINANCE_CACHE_ERROR: str | None = None


class PriceDataError(Exception):
    """Raised when price data cannot be obtained or is stale."""
    pass


def _positive_float(value, default: float = 0.0) -> float:
    """Return value as float only when it is positive and numeric."""
    try:
        if value is None:
            return default
        number = float(value)
        return number if number > 0 else default
    except (TypeError, ValueError):
        return default


def _provider_ticker(symbol: str) -> str:
    text = str(symbol or "").strip()
    if not text:
        return text
    upper = text.upper()
    if upper.endswith(".US"):
        return upper[:-3]
    return upper


def _close_session(session) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _classify_provider_error(exc: Exception | None) -> tuple[str, str]:
    message = str(exc or "")
    lowered = message.lower()
    if "unable to open database file" in lowered or "sqlite" in lowered:
        return "CACHE_ERROR", "YFINANCE_CACHE_ERROR"
    if "invalid crumb" in lowered or "unauthorized" in lowered or "401" in lowered:
        return "PROVIDER_ERROR", "YAHOO_UNAUTHORIZED"
    if "could not resolve host" in lowered or "getaddrinfo" in lowered or "dns" in lowered:
        return "PROVIDER_ERROR", "DNS_ERROR"
    return "PROVIDER_ERROR", "PROVIDER_ERROR"


def _configure_yfinance_cache() -> tuple[str, str | None]:
    """Pin yfinance's sqlite-backed cache to an absolute, writable path."""
    global _YFINANCE_CACHE_INITIALIZED, _YFINANCE_CACHE_ERROR
    with _YFINANCE_CACHE_LOCK:
        if _YFINANCE_CACHE_INITIALIZED:
            return ("CACHE_ERROR" if _YFINANCE_CACHE_ERROR else "COMPLETE", _YFINANCE_CACHE_ERROR)
        _YFINANCE_CACHE_INITIALIZED = True
        cache_dir = DEFAULT_YFINANCE_CACHE_DIR
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _YFINANCE_CACHE_ERROR = f"cache_dir_create_failed:{exc}"
            logger.warning("Failed to create yfinance cache dir %s: %s", cache_dir, exc)
            return "CACHE_ERROR", _YFINANCE_CACHE_ERROR
        try:
            import yfinance.cache as yf_cache

            yf_cache.set_cache_location(str(cache_dir))
            _YFINANCE_CACHE_ERROR = None
            return "COMPLETE", None
        except Exception as exc:
            _YFINANCE_CACHE_ERROR = f"cache_config_failed:{exc}"
            logger.warning("Failed to configure yfinance cache dir %s: %s", cache_dir, exc)
            return "CACHE_ERROR", _YFINANCE_CACHE_ERROR


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
    bid_ask_confirmed: bool = False


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

    def __init__(self, ticker: str, poll_interval: int = 15, max_data_age_seconds: int = 120):
        self.ticker = ticker
        self._provider_ticker = _provider_ticker(ticker)
        self.poll_interval = poll_interval
        self.max_data_age_seconds = max_data_age_seconds
        self._cache_status, self._cache_error_message = _configure_yfinance_cache()
        self._ticker_obj = yf.Ticker(self._provider_ticker)
        self._last_fetch_time: float = 0
        self._last_successful_fetch: float = 0.0
        self._cached_quote: Optional[Quote] = None
        self._last_quote_fetch_status: str = "UNAVAILABLE"
        self._last_quote_error_code: str | None = None
        self._last_quote_error_message: str | None = None
        self._last_history_fetch_status: str = "UNAVAILABLE"
        self._last_history_error_code: str | None = None
        self._last_history_error_message: str | None = None
        self._synthetic_market = os.environ.get("SOXS_SYNTHETIC_MARKET", "").strip().lower() in {"1", "true", "yes", "on"}
        self._synthetic_start_price = _positive_float(os.environ.get("SOXS_SYNTHETIC_START_PRICE"), 100.0)
        self._synthetic_amplitude_pct = _positive_float(os.environ.get("SOXS_SYNTHETIC_AMPLITUDE_PCT"), 2.0)
        self._synthetic_period_seconds = max(15, int(_positive_float(os.environ.get("SOXS_SYNTHETIC_PERIOD_SECONDS"), 120.0)))
        self._synthetic_started_at = time.time()

    def _set_quote_diagnostic(self, status: str, error_code: str | None = None, error_message: str | None = None) -> None:
        self._last_quote_fetch_status = status
        self._last_quote_error_code = error_code
        self._last_quote_error_message = error_message

    def _set_history_diagnostic(self, status: str, error_code: str | None = None, error_message: str | None = None) -> None:
        self._last_history_fetch_status = status
        self._last_history_error_code = error_code
        self._last_history_error_message = error_message

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

    def get_market_cap(self) -> Optional[float]:
        """Best-effort market-cap lookup for selector quality gating."""
        try:
            fast = self._call_with_retries(lambda: self._ticker_obj.fast_info, attempts=1, base_delay=0.25)
        except Exception as e:
            logger.debug("market cap fast_info unavailable for %s: %s", self.ticker, e)
            fast = {}
        try:
            value = None
            if isinstance(fast, dict):
                value = fast.get("market_cap") or fast.get("marketCap")
            else:
                value = getattr(fast, "market_cap", None) or getattr(fast, "marketCap", None)
            if value is None:
                info = self._call_with_retries(lambda: self._ticker_obj.info, attempts=1, base_delay=0.25)
                if isinstance(info, dict):
                    value = info.get("marketCap") or info.get("market_cap")
            market_cap = _positive_float(value)
            return market_cap if market_cap > 0 else None
        except Exception as e:
            logger.debug("market cap unavailable for %s: %s", self.ticker, e)
            return None

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
            status, code = _classify_provider_error(e)
            self._set_history_diagnostic(status, code, str(e))
            return None

    def _fetch_chart_quote(self) -> dict:
        """Fetch a lightweight Yahoo chart quote without inherited proxy settings."""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{self._provider_ticker}"
        session = requests.Session()
        session.trust_env = False
        last_exc = None
        unauthorized_retry_used = False
        try:
            for attempt in range(3):
                try:
                    resp = session.get(
                        url,
                        params={"range": "1d", "interval": "1m", "includePrePost": "true"},
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=float(os.environ.get("AI_SELECTOR_HTTP_TIMEOUT_SECONDS", "3") or 3),
                    )
                    if getattr(resp, "status_code", None) == 401:
                        last_exc = RuntimeError("Yahoo 401 Invalid Crumb")
                        if not unauthorized_retry_used:
                            unauthorized_retry_used = True
                            try:
                                session.cookies.clear()
                            except Exception:
                                pass
                            time.sleep(0.25)
                            continue
                        self._set_quote_diagnostic("PROVIDER_ERROR", "YAHOO_UNAUTHORIZED", str(last_exc))
                        return {}
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as e:
                    last_exc = e
                    time.sleep(0.25 * (2 ** attempt))
            else:
                logger.debug("Yahoo chart fallback failed for %s: %s", self.ticker, last_exc)
                status, code = _classify_provider_error(last_exc)
                self._set_quote_diagnostic(status, code, str(last_exc) if last_exc else None)
                return {}
        finally:
            _close_session(session)

        try:
            if data is None:
                self._set_quote_diagnostic("EMPTY_RESPONSE", "EMPTY_JSON", "chart payload is null")
                return {}
            if not isinstance(data, dict):
                self._set_quote_diagnostic("MALFORMED_RESPONSE", "NON_DICT_JSON", f"chart payload type={type(data).__name__}")
                return {}
            chart = data.get("chart")
            if not isinstance(chart, dict):
                self._set_quote_diagnostic("EMPTY_RESPONSE", "MISSING_CHART", "chart payload missing")
                return {}
            result_list = chart.get("result")
            if not isinstance(result_list, list) or not result_list or result_list[0] is None:
                self._set_quote_diagnostic("EMPTY_RESPONSE", "MISSING_RESULT", "chart result missing")
                return {}
            result = result_list[0]
            if not isinstance(result, dict):
                self._set_quote_diagnostic("MALFORMED_RESPONSE", "NON_DICT_RESULT", f"result type={type(result).__name__}")
                return {}
            if not result:
                self._set_quote_diagnostic("EMPTY_RESPONSE", "EMPTY_RESULT", "chart result empty")
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

            self._set_quote_diagnostic("COMPLETE")
            return {
                "status": "COMPLETE",
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
                    meta.get("regularMarketDayHigh"),
                    _positive_float(_last(quote.get("high"))),
                ),
                "low": _positive_float(
                    meta.get("regularMarketDayLow"),
                    _positive_float(_last(quote.get("low"))),
                ),
            }
        except Exception as e:
            logger.debug("Yahoo chart fallback parse failed for %s: %s", self.ticker, e)
            self._set_quote_diagnostic("MALFORMED_RESPONSE", "CHART_PARSE_ERROR", str(e))
            return {}

    def _fetch_chart_history(self, period: str, interval: str) -> list[OHLCV]:
        range_map = {
            ("1mo", "1d"): "1mo",
            ("6mo", "1d"): "6mo",
            ("1y", "1d"): "1y",
            ("260d", "1d"): "1y",
            ("5d", "1d"): "5d",
            ("1d", "5m"): "1d",
            ("1d", "1m"): "1d",
        }
        yahoo_range = range_map.get((period, interval))
        if not yahoo_range:
            return []
        session = requests.Session()
        session.trust_env = False
        try:
            unauthorized_retry_used = False
            payload = None
            for attempt in range(3):
                try:
                    resp = session.get(
                        f"https://query1.finance.yahoo.com/v8/finance/chart/{self._provider_ticker}",
                        params={"range": yahoo_range, "interval": interval, "includePrePost": "true"},
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=float(os.environ.get("AI_SELECTOR_HTTP_TIMEOUT_SECONDS", "3") or 3),
                    )
                    if getattr(resp, "status_code", None) == 401:
                        if not unauthorized_retry_used:
                            unauthorized_retry_used = True
                            try:
                                session.cookies.clear()
                            except Exception:
                                pass
                            time.sleep(0.25)
                            continue
                        self._set_history_diagnostic("PROVIDER_ERROR", "YAHOO_UNAUTHORIZED", "Yahoo 401 Invalid Crumb")
                        return []
                    resp.raise_for_status()
                    payload = resp.json()
                    break
                except Exception as e:
                    if attempt >= 2:
                        raise
                    time.sleep(0.25 * (2 ** attempt))
            if payload is None:
                self._set_history_diagnostic("EMPTY_RESPONSE", "EMPTY_JSON", "chart payload is null")
                return []
            if payload is None:
                self._set_history_diagnostic("EMPTY_RESPONSE", "EMPTY_JSON", "chart payload is null")
                return []
            if not isinstance(payload, dict):
                self._set_history_diagnostic("MALFORMED_RESPONSE", "NON_DICT_JSON", f"chart payload type={type(payload).__name__}")
                return []
            chart = payload.get("chart")
            if not isinstance(chart, dict):
                self._set_history_diagnostic("EMPTY_RESPONSE", "MISSING_CHART", "chart payload missing")
                return []
            result_list = chart.get("result")
            if not isinstance(result_list, list) or not result_list or result_list[0] is None:
                self._set_history_diagnostic("EMPTY_RESPONSE", "MISSING_RESULT", "chart result missing")
                return []
            result = result_list[0]
            if not isinstance(result, dict):
                self._set_history_diagnostic("MALFORMED_RESPONSE", "NON_DICT_RESULT", f"result type={type(result).__name__}")
                return []
            if not result:
                self._set_history_diagnostic("EMPTY_RESPONSE", "EMPTY_RESULT", "chart result empty")
                return []
            timestamps = result.get("timestamp") or []
            quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []
            candles: list[OHLCV] = []
            for ts, opn, high, low, close, volume in zip(timestamps, opens, highs, lows, closes, volumes):
                if None in (ts, opn, high, low, close):
                    continue
                candles.append(
                    OHLCV(
                        timestamp=datetime.fromtimestamp(ts),
                        open=float(opn),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        volume=int(volume or 0),
                    )
                )
            self._set_history_diagnostic("COMPLETE")
            return candles
        except Exception as e:
            logger.debug("Direct chart history failed for %s (%s %s): %s", self.ticker, period, interval, e)
            status, code = _classify_provider_error(e)
            if code == "PROVIDER_ERROR":
                code = "CHART_HISTORY_ERROR"
            self._set_history_diagnostic(status, code, str(e))
            return []
        finally:
            _close_session(session)

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
            bid_ask_confirmed=True,
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
            if not isinstance(chart, dict):
                chart = {}
            fast = {} if _positive_float(chart.get("price")) > 0 else self._get_safe_fast_info()

            prev_close = _positive_float(
                chart.get("previous_close"),
                _positive_float(fast.get("regularMarketPreviousClose")),
            )
            bid = _positive_float(fast.get("bid"))
            ask = _positive_float(fast.get("ask"))
            bid_ask_confirmed = bid > 0 and ask > 0

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

            # If all data sources failed and we have no fallback, raise
            if price <= 0:
                if self._last_successful_fetch == 0:
                    raise PriceDataError(
                        f"Failed to fetch price for {self.ticker}: "
                        "no data from any source"
                    )
                age = time.time() - self._last_successful_fetch
                if age > self.max_data_age_seconds:
                    raise PriceDataError(
                        f"Price data for {self.ticker} is stale "
                        f"({age:.0f}s > {self.max_data_age_seconds}s max age)"
                    )
                # Data is unavailable but last_fetch is still fresh — return stale cache
                if self._cached_quote:
                    return self._cached_quote
                raise PriceDataError(
                    f"Failed to fetch price for {self.ticker}: "
                    "all sources exhausted"
                )

            # Record successful fetch timestamp
            self._last_successful_fetch = time.time()

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
                bid_ask_confirmed=bid_ask_confirmed,
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
        direct_first = os.environ.get("AI_SELECTOR_DIRECT_HISTORY", "1") == "1"
        skip_slow_fallbacks = os.environ.get("AI_SELECTOR_SKIP_YFINANCE_HISTORY", "0") == "1"
        if direct_first:
            candles = self._fetch_chart_history(period=period, interval=interval)
            if candles:
                return candles
            if skip_slow_fallbacks:
                logger.warning(
                    "Direct history unavailable for %s (%s %s); skipping slow yfinance fallback",
                    self.ticker,
                    period,
                    interval,
                )
                return []

        hist = self._fetch_history(period=period, interval=interval, prepost=True)
        if hist is None or hist.empty:
            logger.error(f"Failed to fetch OHLCV for {self.ticker}: no data returned")
            if self._last_history_fetch_status == "UNAVAILABLE":
                self._set_history_diagnostic("EMPTY_RESPONSE", "NO_HISTORY", "no data returned")
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
