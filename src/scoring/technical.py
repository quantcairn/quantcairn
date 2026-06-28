from __future__ import annotations

from dataclasses import asdict, dataclass
from math import log, sqrt
from statistics import mean, pstdev
from typing import Any, Iterable, Sequence

try:  # pragma: no cover - imported in runtime
    from longbridge.openapi import AdjustType, Period
except Exception:  # pragma: no cover
    AdjustType = None
    Period = None


def _attr(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            out = getattr(value, name)
            if out is not None:
                return out
    return default


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return default


def _ts_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, float)):
        return (0, float(value))
    try:
        return (0, float(str(value)))
    except Exception:
        return (1, str(value))


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Candle:
    timestamp: Any
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_sdk(cls, raw: Any) -> "Candle":
        return cls(
            timestamp=_attr(raw, "timestamp", "time", "date", "datetime"),
            open=_f(_attr(raw, "open", "open_price", "o")),
            high=_f(_attr(raw, "high", "high_price", "h")),
            low=_f(_attr(raw, "low", "low_price", "l")),
            close=_f(_attr(raw, "close", "close_price", "c")),
            volume=_f(_attr(raw, "volume", "vol", "v"), 0.0),
        )

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-9)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_ratio(self) -> float:
        return self.body / self.range

    @property
    def upper_shadow(self) -> float:
        return max(self.high - max(self.open, self.close), 0.0)

    @property
    def lower_shadow(self) -> float:
        return max(min(self.open, self.close) - self.low, 0.0)


@dataclass(frozen=True)
class TechnicalAnalysis:
    symbol: str
    close: float
    sma20: float
    sma50: float
    sma200: float
    rsi14: float
    macd: float
    macd_signal: float
    macd_hist: float
    atr14: float
    support: float
    resistance: float
    volatility_30d: float
    score: float
    reasons: list[str]
    trend: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_candles(raw: Any) -> list[Candle]:
    if raw is None:
        return []
    if isinstance(raw, Candle):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [c if isinstance(c, Candle) else Candle.from_sdk(c) for c in raw]
    for name in ("candlesticks", "candles", "items", "data", "records", "history"):
        value = _attr(raw, name)
        if value is not None:
            if isinstance(value, (list, tuple)):
                return [c if isinstance(c, Candle) else Candle.from_sdk(c) for c in value]
            try:
                return [c if isinstance(c, Candle) else Candle.from_sdk(c) for c in list(value)]
            except Exception:
                return [Candle.from_sdk(value)]
    try:
        return [c if isinstance(c, Candle) else Candle.from_sdk(c) for c in list(raw)]
    except Exception:
        return [Candle.from_sdk(raw)]


def _ordered_candles(raw: Any) -> list[Candle]:
    candles = [c for c in _as_candles(raw) if c.close and c.high and c.low]
    candles.sort(key=lambda item: _ts_key(item.timestamp))
    return candles


def _sma(values: Sequence[float], window: int) -> float:
    if len(values) < window:
        return 0.0
    return mean(values[-window:])


def _ema(values: Sequence[float], window: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (window + 1.0)
    ema = values[0]
    for value in values[1:]:
        ema = alpha * value + (1.0 - alpha) * ema
    return ema


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


def _macd(values: Sequence[float]) -> tuple[float, float, float]:
    if len(values) < 35:
        return 0.0, 0.0, 0.0
    fast = []
    slow = []
    for idx in range(1, len(values) + 1):
        fast.append(_ema(values[:idx], 12))
        slow.append(_ema(values[:idx], 26))
    macd_line = fast[-1] - slow[-1]
    macd_series = [f - s for f, s in zip(fast, slow)]
    signal = _ema(macd_series[-9:], 9)
    hist = macd_line - signal
    return macd_line, signal, hist


def _atr(candles: Sequence[Candle], window: int = 14) -> float:
    if len(candles) <= window:
        return 0.0
    trs = []
    for prev, curr in zip(candles[-(window + 1):-1], candles[-window:]):
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )
        trs.append(tr)
    return mean(trs) if trs else 0.0


def _volatility_30d(closes: Sequence[float]) -> float:
    if len(closes) < 30:
        return 0.0
    returns = []
    for prev, curr in zip(closes[-31:-1], closes[-30:]):
        if prev <= 0 or curr <= 0:
            continue
        returns.append(log(curr / prev))
    if len(returns) < 2:
        return 0.0
    return pstdev(returns) * sqrt(252.0) * 100.0


def _support_resistance(candles: Sequence[Candle], window: int = 20) -> tuple[float, float]:
    if len(candles) < window:
        window = len(candles)
    recent = candles[-window:] if window else list(candles)
    support = min(item.low for item in recent) if recent else 0.0
    resistance = max(item.high for item in recent) if recent else 0.0
    return support, resistance


def _score_technical(
    candles: Sequence[Candle],
    market_bias: float = 0.0,
) -> tuple[float, dict[str, float], list[str], str]:
    closes = [item.close for item in candles]
    if len(closes) < 30:
        return 0.0, {}, ["insufficient candles"], "hold"

    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    rsi14 = _rsi(closes, 14)
    macd_line, macd_signal, macd_hist = _macd(closes)
    atr14 = _atr(candles, 14)
    support, resistance = _support_resistance(candles, 20)
    close = closes[-1]
    atr_pct = (atr14 / close * 100.0) if close else 0.0

    score = 0.0
    reasons: list[str] = []

    if close > sma20 > sma50 > sma200 and sma200 > 0:
        score += 30
        reasons.append("bullish SMA stack")
    elif close < sma20 < sma50 < sma200 and sma200 > 0:
        score -= 30
        reasons.append("bearish SMA stack")
    elif close > sma20 and sma20 > sma50:
        score += 18
        reasons.append("price above SMA20/SMA50")
    elif close < sma20 and sma20 < sma50:
        score -= 18
        reasons.append("price below SMA20/SMA50")

    if sma20 and len(closes) > 21 and sma20 > _sma(closes[:-1], 20):
        score += 6
        reasons.append("SMA20 rising")
    elif sma20 and len(closes) > 21 and sma20 < _sma(closes[:-1], 20):
        score -= 6
        reasons.append("SMA20 falling")

    if rsi14 < 30:
        score += 6
        reasons.append("RSI oversold")
    elif rsi14 > 70:
        score -= 6
        reasons.append("RSI overbought")
    else:
        score += 4 if 45 <= rsi14 <= 65 else 0

    if macd_line > macd_signal and macd_hist > 0:
        score += 12
        reasons.append("MACD bullish")
    elif macd_line < macd_signal and macd_hist < 0:
        score -= 12
        reasons.append("MACD bearish")

    if atr_pct < 1.0:
        score += 4
        reasons.append("low ATR risk")
    elif atr_pct > 6.0:
        score -= 5
        reasons.append("high ATR risk")
    else:
        score += 3

    upside = resistance - close if resistance else 0.0
    downside = close - support if support else 0.0
    if support and resistance and downside > 0:
        rr = upside / downside if downside else 0.0
        if close >= support and close <= resistance:
            score += 6
            reasons.append("inside support/resistance band")
        if rr >= 1.5:
            score += 6
            reasons.append("favorable reward/risk")
        elif rr < 0.8:
            score -= 5
            reasons.append("poor reward/risk")

    if market_bias:
        score += market_bias
        reasons.append(f"market bias {market_bias:+.1f}")

    if close > resistance * 0.995 and resistance > 0:
        score += 4
        reasons.append("near resistance breakout")
    if close < support * 1.005 and support > 0:
        score -= 4
        reasons.append("near support breakdown")

    trend = "buy" if score >= 12 else "sell" if score <= -12 else "hold"
    score = _clamp(50.0 + score / 1.4)
    return score, {
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "rsi14": rsi14,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "atr14": atr14,
        "support": support,
        "resistance": resistance,
        "volatility_30d": _volatility_30d(closes),
    }, reasons, trend


def analyze_technical(candles: Sequence[Any], symbol: str = "", market_bias: float = 0.0) -> TechnicalAnalysis:
    ordered = _ordered_candles(candles)
    score, metrics, reasons, trend = _score_technical(ordered, market_bias=market_bias)
    close = ordered[-1].close if ordered else 0.0
    return TechnicalAnalysis(
        symbol=symbol,
        close=close,
        sma20=metrics.get("sma20", 0.0),
        sma50=metrics.get("sma50", 0.0),
        sma200=metrics.get("sma200", 0.0),
        rsi14=metrics.get("rsi14", 0.0),
        macd=metrics.get("macd", 0.0),
        macd_signal=metrics.get("macd_signal", 0.0),
        macd_hist=metrics.get("macd_hist", 0.0),
        atr14=metrics.get("atr14", 0.0),
        support=metrics.get("support", 0.0),
        resistance=metrics.get("resistance", 0.0),
        volatility_30d=metrics.get("volatility_30d", 0.0),
        score=score,
        reasons=reasons,
        trend=trend,
    )


def analyze_market(candles: Sequence[Any], symbol: str = "SPY.US") -> TechnicalAnalysis:
    return analyze_technical(candles, symbol=symbol, market_bias=0.0)


def analyze_symbol(candles: Sequence[Any], symbol: str, market_bias: float = 0.0) -> TechnicalAnalysis:
    return analyze_technical(candles, symbol=symbol, market_bias=market_bias)
