from __future__ import annotations

from dataclasses import asdict, dataclass
from math import sqrt
from statistics import mean, pstdev
from typing import Any, Iterable, Sequence


def _resolve_column_name(df: Any, candidates: Sequence[str]) -> str | None:
    columns = getattr(df, "columns", None)
    if columns is not None:
        lookup = {str(col).lower(): col for col in columns}
        for candidate in candidates:
            key = candidate.lower()
            if key in lookup:
                return lookup[key]
    if isinstance(df, dict):
        lookup = {str(key).lower(): key for key in df.keys()}
        for candidate in candidates:
            key = candidate.lower()
            if key in lookup:
                return lookup[key]
    return None


def _series(df: Any, *candidates: str) -> list[float]:
    column = _resolve_column_name(df, candidates)
    if column is None:
        return []
    raw = df[column]
    if hasattr(raw, "tolist"):
        values = raw.tolist()
    else:
        try:
            values = list(raw)
        except TypeError:
            values = [raw]
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except Exception:
            out.append(0.0)
    return out


def _scalar(df: Any, *candidates: str, default: float = 0.0) -> float:
    if hasattr(df, "attrs"):
        for candidate in candidates:
            if candidate in df.attrs and df.attrs[candidate] is not None:
                try:
                    return float(df.attrs[candidate])
                except Exception:
                    return default
    series = _series(df, *candidates)
    if series:
        return series[-1]
    if isinstance(df, dict):
        for candidate in candidates:
            if candidate in df and df[candidate] is not None:
                try:
                    return float(df[candidate])
                except Exception:
                    return default
    return default


def _sma(values: Sequence[float], window: int) -> float:
    if len(values) < window:
        return 0.0
    return mean(values[-window:])


def _rsi(values: Sequence[float], window: int = 14) -> float:
    if len(values) <= window:
        return 50.0
    gains = []
    losses = []
    for prev, curr in zip(values[-(window + 1):-1], values[-window:]):
        diff = curr - prev
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = mean(gains) if gains else 0.0
    avg_loss = mean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], window: int = 14) -> float:
    if len(closes) <= window or len(highs) != len(lows) or len(lows) != len(closes):
        return 0.0
    true_ranges = []
    for idx in range(1, len(closes)):
        tr = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )
        true_ranges.append(tr)
    if len(true_ranges) < window:
        return mean(true_ranges) if true_ranges else 0.0
    return mean(true_ranges[-window:])


def _volatility(closes: Sequence[float], window: int = 30) -> float:
    if len(closes) < window + 1:
        return 0.0
    returns = []
    for prev, curr in zip(closes[-(window + 1):-1], closes[-window:]):
        if prev <= 0 or curr <= 0:
            continue
        returns.append((curr / prev) - 1.0)
    if len(returns) < 2:
        return 0.0
    return pstdev(returns)


def _momentum(closes: Sequence[float], window: int = 10) -> float:
    if len(closes) <= window:
        return 0.0
    prev = closes[-(window + 1)]
    curr = closes[-1]
    if prev <= 0:
        return 0.0
    return (curr - prev) / prev


def _volume_spike(volumes: Sequence[float], window: int = 20) -> float:
    if len(volumes) < window + 1:
        return 0.0
    base = mean(volumes[-(window + 1):-1])
    if base <= 0:
        return 0.0
    return volumes[-1] / base


def _gap_pct(opens: Sequence[float], closes: Sequence[float]) -> float:
    if len(opens) < 2 or len(closes) < 2:
        return 0.0
    prev_close = closes[-2]
    if prev_close <= 0:
        return 0.0
    return abs(opens[-1] - prev_close) / prev_close


@dataclass(frozen=True)
class MarketFeatures:
    close: float
    atr: float
    rsi: float
    sma20: float
    sma50: float
    sma200: float
    volatility: float
    volume_spike: float
    momentum: float
    gap_pct: float
    news_score: float
    sma_trend_strength: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_market_features(df: Any) -> MarketFeatures:
    closes = _series(df, "close", "close_price", "c")
    highs = _series(df, "high", "high_price", "h")
    lows = _series(df, "low", "low_price", "l")
    opens = _series(df, "open", "open_price", "o")
    volumes = _series(df, "volume", "vol", "v")
    if len(closes) < 60:
        raise ValueError("detect_market_regime requires at least 60 rows")
    if not highs:
        highs = closes[:]
    if not lows:
        lows = closes[:]
    if not opens:
        opens = closes[:]
    if not volumes:
        volumes = [0.0 for _ in closes]

    close = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    atr = _atr(highs, lows, closes, 14)
    rsi = _rsi(closes, 14)
    volatility = _volatility(closes, 30)
    volume_spike = _volume_spike(volumes, 20)
    momentum = _momentum(closes, 10)
    gap_pct = _gap_pct(opens, closes)
    news_score = _scalar(df, "news_score", default=0.0)
    sma_trend_strength = 0.0
    if close > 0:
        sma_trend_strength = max(
            abs(sma20 - sma50) / close if sma20 and sma50 else 0.0,
            abs(sma50 - sma200) / close if sma50 and sma200 else 0.0,
        )

    return MarketFeatures(
        close=close,
        atr=atr,
        rsi=rsi,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        volatility=volatility,
        volume_spike=volume_spike,
        momentum=momentum,
        gap_pct=gap_pct,
        news_score=news_score,
        sma_trend_strength=sma_trend_strength,
    )


def detect_market_regime(df: Any) -> str:
    features = compute_market_features(df)

    if features.gap_pct > 0.05 or features.volume_spike > 3.0 or features.news_score > 80.0:
        return "EVENT"

    low_vol = features.volatility <= 0.004
    medium_high_vol = features.volatility >= 0.004
    range_like = (
        low_vol
        and 40.0 <= features.rsi <= 60.0
        and features.sma_trend_strength <= 0.015
    )
    trend_like = (
        (features.sma20 > features.sma50 or features.sma50 > features.sma200)
        and features.momentum > 0
        and medium_high_vol
    )

    if range_like:
        return "RANGE"
    if trend_like:
        return "TREND"
    if medium_high_vol and features.momentum > 0:
        return "TREND"
    return "RANGE"
