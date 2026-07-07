import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from ta.momentum import rsi
from ta.trend import MACD
from ta.volatility import AverageTrueRange

from src.config.runtime_values import get_runtime_env, has_longbridge_runtime_credentials
from src.ai_selector.settings import get_float_setting


class Scorer:
    """Score symbols for range-bound swing trading.

    The scoring model is intentionally biased toward names that:
    - trade actively,
    - move enough to create a band,
    - but do not trend too hard in one direction,
    - and have a repeatable tendency to rotate through the same price area.
    """

    MIN_PRICE = 4.0
    MAX_PRICE = 30.0
    MIN_AVG_VOLUME = 1_000_000
    MIN_MARKET_CAP = 1_000_000_000
    MIN_HISTORY_ROWS = 60
    MAX_RANGE_WIDTH_PCT = 45.0
    MIN_RANGE_WIDTH_PCT = 4.0
    MIN_ATR_PCT = 1.0
    MAX_ATR_PCT = 12.0
    GAP_LIMIT_PCT = 5.0
    EVENT_NEWS_SCORE = 80.0
    DEFAULT_MARKET_TIMEOUT = 2.0
    DEFAULT_SCORE_WORKERS = 8
    DEFAULT_MIN_SPREAD_PCT = 3.0

    FALLBACK_PROFILES = {
        "NVDA": {"score": 74.0, "range_low": 118.0, "range_high": 154.0, "volume": 220_000_000},
        "AMD": {"score": 70.0, "range_low": 110.0, "range_high": 168.0, "volume": 70_000_000},
        "TSLA": {"score": 68.0, "range_low": 170.0, "range_high": 260.0, "volume": 85_000_000},
        "META": {"score": 66.0, "range_low": 470.0, "range_high": 660.0, "volume": 18_000_000},
        "AVGO": {"score": 65.0, "range_low": 160.0, "range_high": 260.0, "volume": 30_000_000},
        "MSFT": {"score": 64.0, "range_low": 410.0, "range_high": 500.0, "volume": 25_000_000},
        "AMZN": {"score": 63.0, "range_low": 165.0, "range_high": 230.0, "volume": 45_000_000},
        "GOOGL": {"score": 62.0, "range_low": 145.0, "range_high": 190.0, "volume": 35_000_000},
        "AAPL": {"score": 61.0, "range_low": 170.0, "range_high": 230.0, "volume": 55_000_000},
        "NFLX": {"score": 60.0, "range_low": 600.0, "range_high": 1000.0, "volume": 5_000_000},
        "QCOM": {"score": 58.0, "range_low": 130.0, "range_high": 190.0, "volume": 9_000_000},
        "UBER": {"score": 57.0, "range_low": 60.0, "range_high": 95.0, "volume": 20_000_000},
        "LYFT": {"score": 61.0, "range_low": 12.5, "range_high": 16.8, "volume": 4_000_000},
        "PLTR": {"score": 62.0, "range_low": 104.0, "range_high": 138.0, "volume": 35_000_000},
        "QBTS": {"score": 67.0, "range_low": 20.0, "range_high": 26.5, "volume": 10_000_000},
        "WULF": {"score": 66.0, "range_low": 21.0, "range_high": 27.8, "volume": 10_000_000},
        "SOFI": {"score": 64.0, "range_low": 15.2, "range_high": 19.8, "volume": 22_000_000},
        "NIO": {"score": 59.0, "range_low": 4.5, "range_high": 5.6, "volume": 9_000_000},
        "SMR": {"score": 63.0, "range_low": 8.5, "range_high": 11.2, "volume": 10_000_000},
        "SOXL": {"score": 71.0, "range_low": 18.2, "range_high": 24.8, "volume": 42_000_000},
        "SOXS": {"score": 69.0, "range_low": 4.0, "range_high": 5.4, "volume": 36_000_000},
        "LABU": {"score": 67.0, "range_low": 13.8, "range_high": 18.9, "volume": 16_000_000},
        "LABD": {"score": 67.0, "range_low": 14.1, "range_high": 19.4, "volume": 11_000_000},
        "TQQQ": {"score": 70.0, "range_low": 21.4, "range_high": 28.7, "volume": 58_000_000},
        "SQQQ": {"score": 68.0, "range_low": 7.8, "range_high": 10.7, "volume": 95_000_000},
        "TNA": {"score": 66.0, "range_low": 18.0, "range_high": 24.6, "volume": 9_000_000},
        "TZA": {"score": 66.0, "range_low": 11.8, "range_high": 16.2, "volume": 14_000_000},
        "FAS": {"score": 65.0, "range_low": 21.2, "range_high": 28.1, "volume": 18_000_000},
        "FAZ": {"score": 65.0, "range_low": 10.4, "range_high": 14.7, "volume": 14_000_000},
        "GUSH": {"score": 67.0, "range_low": 18.7, "range_high": 25.3, "volume": 13_000_000},
        "DRIP": {"score": 67.0, "range_low": 11.6, "range_high": 16.1, "volume": 8_000_000},
        "YINN": {"score": 64.0, "range_low": 16.2, "range_high": 22.4, "volume": 15_000_000},
        "YANG": {"score": 64.0, "range_low": 9.8, "range_high": 13.9, "volume": 9_000_000},
        "NAIL": {"score": 65.0, "range_low": 18.4, "range_high": 25.1, "volume": 6_000_000},
        "DPST": {"score": 65.0, "range_low": 17.6, "range_high": 24.2, "volume": 5_000_000},
    }

    FALLBACK_RANGE_PCT = {
        "NVDA": 0.035,
        "AMD": 0.04,
        "TSLA": 0.045,
        "META": 0.03,
        "AVGO": 0.03,
        "MSFT": 0.025,
        "AMZN": 0.03,
        "GOOGL": 0.03,
        "AAPL": 0.025,
        "NFLX": 0.035,
        "QCOM": 0.03,
        "UBER": 0.035,
        "LYFT": 0.06,
        "PLTR": 0.03,
        "QBTS": 0.08,
        "WULF": 0.08,
        "SOFI": 0.06,
        "NIO": 0.08,
        "SMR": 0.08,
        "SOXL": 0.09,
        "SOXS": 0.1,
        "LABU": 0.1,
        "LABD": 0.1,
        "TQQQ": 0.09,
        "SQQQ": 0.1,
        "TNA": 0.1,
        "TZA": 0.1,
        "FAS": 0.09,
        "FAZ": 0.1,
        "GUSH": 0.1,
        "DRIP": 0.1,
        "YINN": 0.1,
        "YANG": 0.1,
        "NAIL": 0.1,
        "DPST": 0.1,
    }

    FALLBACK_SECTOR = {
        "NVDA": "Semiconductors",
        "AMD": "Semiconductors",
        "QCOM": "Semiconductors",
        "AVGO": "Semiconductors",
        "TSLA": "Consumer Discretionary",
        "AAPL": "Technology",
        "MSFT": "Technology",
        "GOOGL": "Communication Services",
        "META": "Communication Services",
        "AMZN": "Consumer Discretionary",
        "NFLX": "Communication Services",
        "UBER": "Technology",
        "LYFT": "Technology",
        "PLTR": "Technology",
        "QBTS": "Information Technology",
        "WULF": "Energy",
        "SOFI": "Financial Services",
        "NIO": "Consumer Discretionary",
        "SMR": "Energy",
        "SOXL": "Leveraged Semiconductor ETF",
        "SOXS": "Inverse Semiconductor ETF",
        "LABU": "Leveraged Biotechnology ETF",
        "LABD": "Inverse Biotechnology ETF",
        "TQQQ": "Leveraged Nasdaq ETF",
        "SQQQ": "Inverse Nasdaq ETF",
        "TNA": "Leveraged Small Cap ETF",
        "TZA": "Inverse Small Cap ETF",
        "FAS": "Leveraged Financial ETF",
        "FAZ": "Inverse Financial ETF",
        "GUSH": "Leveraged Energy ETF",
        "DRIP": "Inverse Energy ETF",
        "YINN": "Leveraged China ETF",
        "YANG": "Inverse China ETF",
        "NAIL": "Leveraged Homebuilders ETF",
        "DPST": "Leveraged Regional Banks ETF",
    }

    def __init__(self):
        self.min_price = self._env_float("AI_SELECTOR_MIN_PRICE", get_float_setting("min_price", self.MIN_PRICE))
        self.max_price = self._env_float("AI_SELECTOR_MAX_PRICE", get_float_setting("max_price", self.MAX_PRICE))
        self.market_timeout = self._env_float("AI_SELECTOR_MARKET_TIMEOUT", self.DEFAULT_MARKET_TIMEOUT)
        self.score_workers = max(1, self._env_int("AI_SELECTOR_SCORE_WORKERS", self.DEFAULT_SCORE_WORKERS))
        self.min_spread_pct = self._env_float("AI_SELECTOR_MIN_SPREAD_PCT", self.DEFAULT_MIN_SPREAD_PCT)
        self.allow_proxy_market = os.environ.get("AI_SELECTOR_ALLOW_PROXY_MARKET", "0") == "1"

    def _env_float(self, name: str, default: float) -> float:
        raw = os.environ.get(name)
        try:
            return float(raw) if raw not in (None, "") else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _env_int(self, name: str, default: int) -> int:
        raw = os.environ.get(name)
        try:
            return int(raw) if raw not in (None, "") else int(default)
        except (TypeError, ValueError):
            return int(default)

    def _longbridge_symbol(self, symbol: str) -> str:
        return symbol if "." in symbol else f"{symbol}.US"

    def _longbridge_value(self, obj, *names, default=None):
        if isinstance(obj, dict):
            for name in names:
                if name in obj and obj[name] is not None:
                    return obj[name]
            return default
        for name in names:
            if hasattr(obj, name):
                value = getattr(obj, name)
                if value is not None:
                    return value
        return default

    def _fetch_longbridge_snapshot(self, symbol: str) -> dict:
        if not has_longbridge_runtime_credentials():
            raise RuntimeError("longbridge credentials unavailable")

        import longbridge.openapi as lb

        config = lb.Config.from_apikey(
            get_runtime_env("LONGBRIDGE_APP_KEY") or get_runtime_env("LONGBRIDGE_API_KEY") or "",
            get_runtime_env("LONGBRIDGE_APP_SECRET") or get_runtime_env("LONGBRIDGE_API_SECRET") or "",
            get_runtime_env("LONGBRIDGE_ACCESS_TOKEN", ""),
            http_url=get_runtime_env("LONGBRIDGE_HTTP_URL") or get_runtime_env("LONGBRIDGE_BASE_URL"),
            quote_ws_url=get_runtime_env("LONGBRIDGE_QUOTE_WS_URL"),
            trade_ws_url=get_runtime_env("LONGBRIDGE_TRADE_WS_URL"),
            log_path=get_runtime_env("LONGBRIDGE_LOG_PATH"),
        )
        ctx = lb.QuoteContext(config)
        try:
            resp = ctx.quote(symbols=[self._longbridge_symbol(symbol)])
            items = resp if isinstance(resp, (list, tuple)) else [resp]
            item = items[0] if items else None
            if item is None:
                raise RuntimeError("longbridge quote unavailable")

            price = self._longbridge_value(item, "last_done", "price", "last_price", default=0.0)
            high = self._longbridge_value(item, "high", "day_high", default=price)
            low = self._longbridge_value(item, "low", "day_low", default=price)
            volume = self._longbridge_value(item, "volume", "turnover", default=0)
            price = float(price or 0.0)
            high = float(high or price or 0.0)
            low = float(low or price or 0.0)
            volume = int(float(volume or 0))
            if price <= 0:
                raise RuntimeError("longbridge quote missing price")

            return {
                "price": price,
                "recent_high": high if high > 0 else price,
                "recent_low": low if low > 0 else price,
                "volume": volume,
            }
        finally:
            for attr in ("close", "dispose", "release"):
                fn = getattr(ctx, attr, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break

    def _fetch_chart_daily(self, symbol: str, days: int = 320) -> pd.DataFrame:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "range": "1y" if days >= 250 else f"{max(days, 1)}d",
            "interval": "1d",
            "includePrePost": "false",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            )
        }
        last_error = None
        resp = None
        trust_env_options = (False, True) if self.allow_proxy_market else (False,)
        for trust_env in trust_env_options:
            try:
                session = requests.Session()
                session.trust_env = trust_env
                resp = session.get(url, params=params, headers=headers, timeout=self.market_timeout)
                resp.raise_for_status()
                break
            except Exception as exc:
                last_error = exc
                resp = None
        if resp is None:
            raise last_error
        result = (resp.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return pd.DataFrame()
        quote = (result.get("indicators", {}).get("quote") or [None])[0]
        ts = result.get("timestamp") or []
        if not quote or not ts:
            return pd.DataFrame()
        df = pd.DataFrame(
            {
                "Open": quote.get("open") or [],
                "High": quote.get("high") or [],
                "Low": quote.get("low") or [],
                "Close": quote.get("close") or [],
                "Volume": quote.get("volume") or [],
            }
        )
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return self._standardize_history(df)

    def _load_history(self, symbol: str) -> pd.DataFrame:
        if os.environ.get("AI_SELECTOR_LIVE_DATA", "1") == "0":
            return pd.DataFrame()

        prefer_yfinance = os.environ.get("AI_SELECTOR_USE_YFINANCE", "0") == "1"
        if prefer_yfinance:
            try:
                df = yf.download(symbol, period="260d", interval="1d", progress=False)
                if df is not None and not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    return self._standardize_history(df)
            except Exception:
                pass
        try:
            df = self._fetch_chart_daily(symbol)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass
        if not prefer_yfinance:
            try:
                df = yf.download(symbol, period="260d", interval="1d", progress=False)
                if df is not None and not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    return self._standardize_history(df)
            except Exception:
                pass
        return pd.DataFrame()

    def _standardize_history(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        rename = {}
        for col in out.columns:
            low = str(col).lower()
            if low == "adj close":
                continue
            if low == "open":
                rename[col] = "Open"
            elif low == "high":
                rename[col] = "High"
            elif low == "low":
                rename[col] = "Low"
            elif low == "close":
                rename[col] = "Close"
            elif low == "volume":
                rename[col] = "Volume"
        out = out.rename(columns=rename)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        if "Volume" not in out.columns:
            out["Volume"] = 0.0
        out = out.dropna(subset=["High", "Low", "Close"])
        return out

    def _fetch_live_snapshot(self, symbol: str) -> dict:
        last_error = None
        trust_env_options = (False, True) if self.allow_proxy_market else (False,)
        for trust_env in trust_env_options:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                params = {"range": "5d", "interval": "1d", "includePrePost": "false"}
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                    )
                }
                session = requests.Session()
                session.trust_env = trust_env
                resp = session.get(url, params=params, headers=headers, timeout=self.market_timeout)
                resp.raise_for_status()
                result = (resp.json().get("chart", {}).get("result") or [None])[0] or {}
                meta = result.get("meta") or {}
                quote = ((result.get("indicators") or {}).get("quote") or [None])[0] or {}

                closes = pd.to_numeric(pd.Series(quote.get("close") or []), errors="coerce").dropna()
                highs = pd.to_numeric(pd.Series(quote.get("high") or []), errors="coerce").dropna()
                lows = pd.to_numeric(pd.Series(quote.get("low") or []), errors="coerce").dropna()
                volumes = pd.to_numeric(pd.Series(quote.get("volume") or []), errors="coerce").dropna()

                last_close = float(closes.iloc[-1]) if not closes.empty else 0.0
                last_price = float(meta.get("regularMarketPrice") or last_close or 0.0)
                recent_high = float(highs.max()) if not highs.empty else last_price
                recent_low = float(lows.min()) if not lows.empty else last_price
                recent_volume = int(volumes.iloc[-1]) if not volumes.empty else 0

                if last_price > 0:
                    return {
                        "price": last_price,
                        "recent_high": recent_high,
                        "recent_low": recent_low,
                        "volume": recent_volume,
                    }
            except Exception as exc:
                last_error = exc
        try:
            return self._fetch_longbridge_snapshot(symbol)
        except Exception as exc:
            if last_error is None:
                last_error = exc
        raise last_error

    def _fallback_profile_for_symbol(self, symbol: str) -> dict | None:
        profile = self.FALLBACK_PROFILES.get(symbol)
        if not profile:
            return None

        dynamic = dict(profile)
        if os.environ.get("AI_SELECTOR_LIVE_DATA", "1") == "0":
            return dynamic
        try:
            snapshot = self._fetch_live_snapshot(symbol)
            price = float(snapshot.get("price") or 0.0)
            if price > 0:
                band = float(self.FALLBACK_RANGE_PCT.get(symbol, 0.03))
                low = max(0.01, price * (1.0 - band))
                high = price * (1.0 + band)
                recent_low = float(snapshot.get("recent_low") or low)
                recent_high = float(snapshot.get("recent_high") or high)
                dynamic["range_low"] = round(min(low, recent_low), 2)
                dynamic["range_high"] = round(max(high, recent_high), 2)
                dynamic["volume"] = max(int(snapshot.get("volume") or 0), int(profile["volume"]))
        except Exception:
            pass
        return dynamic

    def score_universe(self, symbols: List[str], news_map: Dict[str, List[str]]):
        scored = []
        if len(symbols) <= 1:
            for symbol in symbols:
                item = self._score_symbol(symbol, news_map.get(symbol, []))
                if item:
                    scored.append(item)
        else:
            with ThreadPoolExecutor(max_workers=min(self.score_workers, len(symbols))) as executor:
                futures = {
                    executor.submit(self._score_symbol, symbol, news_map.get(symbol, [])): symbol
                    for symbol in symbols
                }
                for future in as_completed(futures):
                    try:
                        item = future.result()
                    except Exception:
                        item = None
                    if item:
                        scored.append(item)

        if not scored:
            return self._fallback_scores(symbols, news_map)

        return scored

    def _score_symbol(self, symbol: str, news_items: Sequence[str]) -> Optional[dict]:
        try:
            df = self._load_history(symbol)
            if df.empty or len(df) < self.MIN_HISTORY_ROWS:
                fallback = self._fallback_profile_for_symbol(symbol)
                if fallback:
                    return self._fallback_scored_item(symbol, fallback, news_items)
                return None
            return self.score_frame(symbol=symbol, df=df, news_items=list(news_items))
        except Exception:
            fallback = self._fallback_profile_for_symbol(symbol)
            if fallback:
                return self._fallback_scored_item(symbol, fallback, news_items)
            return None

    def score_frame(
        self,
        symbol: str,
        df: pd.DataFrame,
        news_items: Optional[List[str]] = None,
        sector: Optional[str] = None,
    ) -> Optional[dict]:
        df = self._standardize_history(df)
        if df.empty or len(df) < self.MIN_HISTORY_ROWS:
            return None

        news_items = news_items or []
        sector = sector or self._sector_for_symbol(symbol)

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        volume = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series([0.0] * len(df), index=df.index)
        open_ = df["Open"].astype(float) if "Open" in df.columns else close.shift(1).fillna(close.iloc[0])

        last_close = float(close.iloc[-1])
        last_open = float(open_.iloc[-1])
        last_volume = float(volume.iloc[-1]) if len(volume) else 0.0
        avg_volume_20 = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.mean())
        avg_volume_60 = float(volume.rolling(60).mean().iloc[-1]) if len(volume) >= 60 else float(volume.mean())

        if last_close < self.min_price:
            return None
        if last_close > self.max_price:
            return None
        if avg_volume_20 < self.MIN_AVG_VOLUME:
            return None

        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1] if len(df) >= 200 else np.nan
        sma20_prev = close.rolling(20).mean().iloc[-6] if len(df) >= 26 else np.nan
        sma50_prev = close.rolling(50).mean().iloc[-6] if len(df) >= 56 else np.nan
        sma200_prev = close.rolling(200).mean().iloc[-6] if len(df) >= 206 else np.nan

        rsi_val = float(rsi(close, window=14).iloc[-1])
        macd_hist = float(MACD(close).macd_diff().iloc[-1])
        atr = float(AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])
        atr_pct = (atr / last_close * 100.0) if last_close else 0.0

        returns = close.pct_change().dropna()
        return_vol_pct = float(returns.rolling(20).std().iloc[-1] * 100.0) if len(returns) >= 20 else 0.0
        rolling_high_20 = float(high.tail(20).max())
        rolling_low_20 = float(low.tail(20).min())
        range_width_pct = ((rolling_high_20 - rolling_low_20) / last_close * 100.0) if last_close else 0.0

        gaps = self._gap_stats(df)
        gap_rate = gaps["gap_rate"]
        max_gap_pct = gaps["max_gap_pct"]
        volume_spike = (last_volume / avg_volume_20) if avg_volume_20 > 0 else 0.0
        news_score = self._news_score(news_items)
        max_drawdown_pct = self._max_drawdown(close)

        reject_reasons = []
        if range_width_pct > self.MAX_RANGE_WIDTH_PCT:
            reject_reasons.append("range too wide")
        if range_width_pct < self.MIN_RANGE_WIDTH_PCT:
            reject_reasons.append("range too tight")
        if atr_pct < self.MIN_ATR_PCT:
            reject_reasons.append("volatility too low")
        if atr_pct > self.MAX_ATR_PCT:
            reject_reasons.append("volatility too high")
        if gap_rate > 0.20 or max_gap_pct > self.GAP_LIMIT_PCT:
            reject_reasons.append("frequent gap risk")
        if news_score >= self.EVENT_NEWS_SCORE:
            reject_reasons.append("event/news driven")
        if self._strong_trend(close, sma20, sma50, sma200, rsi_val, return_vol_pct):
            reject_reasons.append("strong trend")
        if self._too_flat(close, atr_pct, return_vol_pct):
            reject_reasons.append("insufficient movement")

        if reject_reasons:
            return None

        volatility_score = self._volatility_score(atr_pct, return_vol_pct, range_width_pct)
        volume_score = self._volume_score(last_volume, avg_volume_20, avg_volume_60, volume_spike)
        trend_fit_score = self._trend_fit_score(close, sma20, sma50, sma200, sma20_prev, sma50_prev, sma200_prev, rsi_val, macd_hist)
        repeatability_score = self._repeatability_score(close, high, low, sma20, sma50)
        drawdown_safety_score = self._drawdown_safety_score(close, max_drawdown_pct)
        base_score = (
            0.30 * volatility_score
            + 0.20 * volume_score
            + 0.20 * trend_fit_score
            + 0.15 * repeatability_score
            + 0.10 * drawdown_safety_score
        )

        support, resistance, support_meta, resistance_meta = self._estimate_range(df, atr)
        if ((resistance - support) / support * 100.0) < self.min_spread_pct:
            return None
        price_mid = (support + resistance) / 2.0 if resistance > support else last_close

        return {
            "ticker": symbol,
            "sector": sector,
            "score": float(round(base_score, 2)),
            "base_score": float(round(base_score, 2)),
            "volatility_score": float(round(volatility_score, 2)),
            "volume_score": float(round(volume_score, 2)),
            "trend_fit_score": float(round(trend_fit_score, 2)),
            "repeatability_score": float(round(repeatability_score, 2)),
            "drawdown_safety_score": float(round(drawdown_safety_score, 2)),
            "correlation_penalty": 0.0,
            "news_score": float(round(news_score, 2)),
            "range_low": float(round(support, 2)),
            "range_high": float(round(resistance, 2)),
            "suggested_range": f"${support:.2f} - ${resistance:.2f}",
            "support_source": support_meta,
            "resistance_source": resistance_meta,
            "risk": {
                "stop_loss_pct": self._stop_loss_pct(atr_pct, max_drawdown_pct),
            },
            "size": self._position_size_hint(last_close, avg_volume_20),
            "data_source": "live",
            "metrics": {
                "last_close": float(round(last_close, 4)),
                "atr_pct": float(round(atr_pct, 4)),
                "return_vol_pct": float(round(return_vol_pct, 4)),
                "range_width_pct": float(round(range_width_pct, 4)),
                "gap_rate": float(round(gap_rate, 4)),
                "max_gap_pct": float(round(max_gap_pct, 4)),
                "volume_spike": float(round(volume_spike, 4)),
                "max_drawdown_pct": float(round(max_drawdown_pct, 4)),
                "price_midpoint": float(round(price_mid, 4)),
            },
            "series": {
                "returns": self._series_tail_returns(close),
            },
        }

    def _fallback_scores(self, symbols: List[str], news_map: Dict[str, List[str]]):
        scored = []
        for symbol in symbols:
            profile = self._fallback_profile_for_symbol(symbol)
            if not profile:
                continue
            item = self._fallback_scored_item(symbol, profile, news_map.get(symbol, []))
            if item:
                scored.append(item)
        return scored

    def _fallback_scored_item(self, symbol: str, profile: dict, news_items: Sequence[str]):
        support = float(profile["range_low"])
        resistance = float(profile["range_high"])
        price_mid = (support + resistance) / 2.0
        if price_mid < self.min_price or price_mid > self.max_price:
            return None
        if ((resistance - support) / support * 100.0) < self.min_spread_pct:
            return None
        band_pct = ((resistance - support) / price_mid * 100.0) if price_mid else 0.0
        news_score = self._news_score(list(news_items))
        volume_score = min(100.0, 35.0 + math.log10(max(float(profile["volume"]), 1.0) / 1_000_000.0 + 1.0) * 20.0)
        volatility_score = max(0.0, min(100.0, 55.0 + band_pct * 1.5))
        trend_fit_score = 58.0
        repeatability_score = 62.0
        drawdown_safety_score = 55.0
        base_score = (
            0.30 * volatility_score
            + 0.20 * volume_score
            + 0.20 * trend_fit_score
            + 0.15 * repeatability_score
            + 0.10 * drawdown_safety_score
        )
        return {
            "ticker": symbol,
            "sector": self._sector_for_symbol(symbol),
            "score": float(round(base_score, 2)),
            "base_score": float(round(base_score, 2)),
            "volatility_score": float(round(volatility_score, 2)),
            "volume_score": float(round(volume_score, 2)),
            "trend_fit_score": float(round(trend_fit_score, 2)),
            "repeatability_score": float(round(repeatability_score, 2)),
            "drawdown_safety_score": float(round(drawdown_safety_score, 2)),
            "correlation_penalty": 0.0,
            "news_score": float(round(news_score, 2)),
            "range_low": support,
            "range_high": resistance,
            "suggested_range": f"${support:.2f} - ${resistance:.2f}",
            "support_source": "fallback",
            "resistance_source": "fallback",
            "risk": {"stop_loss_pct": 1.5},
            "size": int(max(1, min(1000, profile["volume"] // 1000))),
            "data_source": "fallback",
            "avg_daily_volume_hint": int(profile["volume"]),
            "price_midpoint_hint": float(round(price_mid, 4)),
            "fallback_history_incomplete": True,
            "metrics": {
                "last_close": float(round(price_mid, 4)),
                "atr_pct": float(round(band_pct / 2.0, 4)),
                "return_vol_pct": float(round(band_pct / 3.0, 4)),
                "range_width_pct": float(round(band_pct, 4)),
                "gap_rate": 0.0,
                "max_gap_pct": 0.0,
                "volume_spike": 1.0,
                "max_drawdown_pct": 12.0,
                "price_midpoint": float(round(price_mid, 4)),
            },
            "series": {"returns": []},
        }

    def _sector_for_symbol(self, symbol: str) -> str:
        return self.FALLBACK_SECTOR.get(symbol, "Unknown")

    def _series_tail_returns(self, close: pd.Series, tail: int = 60) -> List[float]:
        series = close.pct_change().dropna().tail(tail)
        return [float(round(x, 6)) for x in series.tolist() if pd.notna(x)]

    def _gap_stats(self, df: pd.DataFrame) -> Dict[str, float]:
        if "Open" in df.columns:
            open_ = df["Open"].astype(float)
        else:
            open_ = df["Close"].shift(1).fillna(df["Close"].iloc[0])
        prev_close = df["Close"].shift(1).astype(float)
        gap_pct = ((open_ - prev_close).abs() / prev_close.replace(0, np.nan) * 100.0).dropna()
        if gap_pct.empty:
            return {"gap_rate": 0.0, "max_gap_pct": 0.0}
        gap_rate = float((gap_pct > self.GAP_LIMIT_PCT).mean())
        max_gap_pct = float(gap_pct.max())
        return {"gap_rate": gap_rate, "max_gap_pct": max_gap_pct}

    def _max_drawdown(self, close: pd.Series) -> float:
        rolling_max = close.cummax()
        drawdown = (close / rolling_max - 1.0) * 100.0
        min_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
        return abs(min_drawdown)

    def _strong_trend(
        self,
        close: pd.Series,
        sma20,
        sma50,
        sma200,
        rsi_val: float,
        return_vol_pct: float,
    ) -> bool:
        if any(pd.isna(x) for x in [sma20, sma50]):
            return False

        price = float(close.iloc[-1])
        trend_gap_1 = abs(float(sma20) - float(sma50)) / price * 100.0 if price else 0.0
        trend_gap_2 = abs(float(sma50) - float(sma200)) / price * 100.0 if price and not pd.isna(sma200) else 0.0
        up_stack = not pd.isna(sma200) and float(sma20) > float(sma50) > float(sma200)
        down_stack = not pd.isna(sma200) and float(sma20) < float(sma50) < float(sma200)
        stacked = up_stack or down_stack
        momentum_extreme = rsi_val >= 68.0 or rsi_val <= 32.0
        volatility_strong = return_vol_pct >= 2.8
        return bool(stacked and (trend_gap_1 >= 1.5 or trend_gap_2 >= 1.5) and momentum_extreme and volatility_strong)

    def _too_flat(self, close: pd.Series, atr_pct: float, return_vol_pct: float) -> bool:
        if atr_pct >= self.MIN_ATR_PCT or return_vol_pct >= 0.8:
            return False
        recent_range_pct = ((float(close.tail(20).max()) - float(close.tail(20).min())) / float(close.iloc[-1]) * 100.0) if len(close) >= 20 and float(close.iloc[-1]) else 0.0
        return recent_range_pct < 3.0

    def _volatility_score(self, atr_pct: float, return_vol_pct: float, range_width_pct: float) -> float:
        if atr_pct <= 0 or return_vol_pct <= 0:
            return 0.0
        combined = (atr_pct + return_vol_pct) / 2.0
        ideal = 3.5
        score = 100.0 - abs(combined - ideal) * 14.0
        if range_width_pct > 0:
            score += min(10.0, range_width_pct / 6.0)
        return float(max(0.0, min(100.0, score)))

    def _volume_score(self, last_volume: float, avg_volume_20: float, avg_volume_60: float, volume_spike: float) -> float:
        if avg_volume_20 <= 0:
            return 0.0
        base = math.log10(avg_volume_20 / 1_000_000.0 + 1.0) * 35.0
        activity = min(30.0, volume_spike * 10.0)
        persistence = 0.0
        if avg_volume_60 > 0:
            persistence = min(25.0, math.log10(avg_volume_60 / 1_000_000.0 + 1.0) * 10.0)
        score = 20.0 + base + activity + persistence
        return float(max(0.0, min(100.0, score)))

    def _trend_fit_score(
        self,
        close: pd.Series,
        sma20,
        sma50,
        sma200,
        sma20_prev,
        sma50_prev,
        sma200_prev,
        rsi_val: float,
        macd_hist: float,
    ) -> float:
        price = float(close.iloc[-1])
        if price <= 0:
            return 0.0

        def pct_gap(a, b) -> float:
            if pd.isna(a) or pd.isna(b) or price <= 0:
                return 0.0
            return abs(float(a) - float(b)) / price * 100.0

        alignment_penalty = pct_gap(sma20, sma50) * 8.0
        if not pd.isna(sma200):
            alignment_penalty += pct_gap(sma50, sma200) * 5.0

        slope_penalty = 0.0
        if not pd.isna(sma20_prev):
            slope_penalty += abs((float(sma20) - float(sma20_prev)) / price * 100.0) * 30.0
        if not pd.isna(sma50_prev):
            slope_penalty += abs((float(sma50) - float(sma50_prev)) / price * 100.0) * 18.0
        if not pd.isna(sma200_prev):
            slope_penalty += abs((float(sma200) - float(sma200_prev)) / price * 100.0) * 10.0

        rsi_score = max(0.0, 100.0 - abs(rsi_val - 50.0) * 2.6)
        macd_score = max(0.0, 100.0 - min(100.0, abs(macd_hist) / max(price, 1e-6) * 10000.0))
        score = 100.0 - alignment_penalty - slope_penalty
        score = score * 0.55 + rsi_score * 0.25 + macd_score * 0.20
        return float(max(0.0, min(100.0, score)))

    def _repeatability_score(self, close: pd.Series, high: pd.Series, low: pd.Series, sma20, sma50) -> float:
        window = min(60, len(close))
        if window < 20:
            return 0.0
        recent_close = close.tail(window)
        recent_high = high.tail(window)
        recent_low = low.tail(window)

        support_band = float(recent_low.min())
        resistance_band = float(recent_high.max())
        price = float(recent_close.iloc[-1])
        if price <= 0 or resistance_band <= support_band:
            return 0.0

        band_width = resistance_band - support_band
        support_touches = int((recent_close <= support_band + band_width * 0.12).sum())
        resistance_touches = int((recent_close >= resistance_band - band_width * 0.12).sum())
        middle_zone = support_band + band_width * 0.45
        middle_touches = int(((recent_close >= middle_zone - band_width * 0.08) & (recent_close <= middle_zone + band_width * 0.08)).sum())

        sign_series = np.sign((recent_close - recent_close.rolling(5).mean()).dropna())
        oscillations = int((sign_series.diff().fillna(0) != 0).sum()) if len(sign_series) else 0
        balance = min(support_touches, resistance_touches) / max(1, max(support_touches, resistance_touches))

        score = (
            min(40.0, (support_touches + resistance_touches) * 3.0)
            + min(25.0, middle_touches * 2.0)
            + min(20.0, oscillations * 1.8)
            + balance * 15.0
        )
        return float(max(0.0, min(100.0, score)))

    def _drawdown_safety_score(self, close: pd.Series, max_drawdown_pct: float) -> float:
        if close.empty:
            return 0.0
        recent = close.tail(60)
        low = float(recent.min())
        high = float(recent.max())
        last = float(recent.iloc[-1])
        if high <= low or last <= 0:
            return 0.0

        recovery_position = (last - low) / (high - low)
        drawdown_penalty = max(0.0, max_drawdown_pct - 8.0) * 2.0
        recovery_bonus = max(0.0, recovery_position * 30.0)
        stability_bonus = 25.0 if max_drawdown_pct <= 20.0 else max(0.0, 25.0 - (max_drawdown_pct - 20.0) * 2.5)
        score = 45.0 + recovery_bonus + stability_bonus - drawdown_penalty
        return float(max(0.0, min(100.0, score)))

    def _estimate_range(self, df: pd.DataFrame, atr: float) -> Tuple[float, float, str, str]:
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        recent = close.tail(60)
        recent_high = high.tail(60)
        recent_low = low.tail(60)
        last_close = float(close.iloc[-1])

        hist_support = float(recent_low.quantile(0.12))
        hist_resistance = float(recent_high.quantile(0.88))

        if "Volume" in df.columns and df["Volume"].notna().any():
            volume = df["Volume"].astype(float).tail(60)
            valid = pd.DataFrame({"close": recent, "volume": volume}).dropna()
            if len(valid) >= 10:
                q1 = valid["close"].quantile(0.20)
                q2 = valid["close"].quantile(0.80)
                lower_vol = valid.loc[valid["close"] <= q1, "close"].mean()
                upper_vol = valid.loc[valid["close"] >= q2, "close"].mean()
            else:
                lower_vol = hist_support
                upper_vol = hist_resistance
        else:
            lower_vol = hist_support
            upper_vol = hist_resistance

        atr_adjust = max(atr * 1.2, last_close * 0.012)
        lower_atr = last_close - atr_adjust
        upper_atr = last_close + atr_adjust

        support = (hist_support * 0.45) + (lower_vol * 0.35) + (lower_atr * 0.20)
        resistance = (hist_resistance * 0.45) + (upper_vol * 0.35) + (upper_atr * 0.20)

        support = max(0.01, min(support, last_close * 0.98))
        resistance = max(last_close * 1.02, resistance)
        if resistance <= support:
            resistance = support * 1.08

        return round(float(support), 2), round(float(resistance), 2), "hist+volume+atr", "hist+volume+atr"

    def _news_score(self, news_items: List[str]):
        pos = ["beat", "beats", "raise", "upgrade", "positive", "growth", "beat expectations"]
        neg = ["miss", "falls", "downgrade", "negative", "lawsuit", "recall", "missed", "investigation"]
        s = 50.0
        text = " ".join(news_items).lower()
        for p in pos:
            s += text.count(p) * 2
        for n in neg:
            s -= text.count(n) * 3
        return max(0.0, min(100.0, s))

    def _stop_loss_pct(self, atr_pct: float, max_drawdown_pct: float) -> float:
        base = max(1.2, min(3.5, atr_pct * 1.3))
        if max_drawdown_pct > 20:
            base = max(base, 2.5)
        return float(round(base, 2))

    def _position_size_hint(self, last_close: float, avg_volume_20: float) -> int:
        if last_close <= 0:
            return 1
        liquidity_scale = max(1.0, min(12.0, avg_volume_20 / 2_000_000.0))
        size = int(max(1, min(1000, (liquidity_scale * 1000.0) / max(last_close, 1.0) * 0.25)))
        return max(1, size)
