from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, List, Optional, Sequence

try:  # pragma: no cover - imported in the real runtime
    from longbridge.openapi import AdjustType, Period
except Exception:  # pragma: no cover - keep pure tests importable if SDK is unavailable
    AdjustType = None
    Period = None


def _get_attr(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            attr = getattr(value, name)
            if attr is not None:
                return attr
    return default


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return 0.0


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value)))
    except Exception:
        return 0


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
            timestamp=_get_attr(raw, "timestamp", "time", "date", "datetime", default=None),
            open=_as_float(_get_attr(raw, "open", "open_price", "o")),
            high=_as_float(_get_attr(raw, "high", "high_price", "h")),
            low=_as_float(_get_attr(raw, "low", "low_price", "l")),
            close=_as_float(_get_attr(raw, "close", "close_price", "c")),
            volume=_as_float(_get_attr(raw, "volume", "vol", "v", default=0.0)),
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
class MarketContext:
    symbol: str
    action: str
    score: int
    close: float
    ma20: float
    ma60: float
    k: float
    d: float
    j: float
    reasons: List[str]


@dataclass(frozen=True)
class TechnicalSignal:
    symbol: str
    action: str
    score: int
    market_bias: int
    close: float
    ma5: float
    ma10: float
    ma20: float
    ma60: float
    k: float
    d: float
    j: float
    doji: bool
    reasons: List[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _flatten_records(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    for name in ("candlesticks", "candles", "items", "data", "records", "history"):
        value = _get_attr(raw, name)
        if value is not None:
            if isinstance(value, (list, tuple)):
                return list(value)
            try:
                return list(value)
            except TypeError:
                return [value]
    try:
        return list(raw)
    except TypeError:
        return [raw]


def _timestamp_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, float)):
        return (0, float(value))
    try:
        return (0, float(str(value)))
    except Exception:
        return (1, str(value))


def _extract_candles(raw: Any) -> List[Candle]:
    return [Candle.from_sdk(item) for item in _flatten_records(raw)]


def _sma(values: Sequence[float], window: int) -> float:
    if len(values) < window:
        return 0.0
    chunk = values[-window:]
    return sum(chunk) / window


def _kdj(candles: Sequence[Candle], period: int = 9) -> tuple[float, float, float, float, float, float]:
    if len(candles) < period + 1:
        return 50.0, 50.0, 50.0, 50.0, 50.0, 50.0

    k = 50.0
    d = 50.0
    prev_k = k
    prev_d = d
    for idx in range(period - 1, len(candles)):
        window = candles[idx - period + 1 : idx + 1]
        high_n = max(item.high for item in window)
        low_n = min(item.low for item in window)
        close = candles[idx].close
        rsv = 50.0 if high_n <= low_n else (close - low_n) / (high_n - low_n) * 100.0
        prev_k = k
        prev_d = d
        k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
        d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
    j = 3.0 * k - 2.0 * d
    return k, d, j, prev_k, prev_d, rsv


def _is_doji(candle: Candle, threshold: float = 0.18) -> bool:
    return candle.body_ratio <= threshold


def _cross_up(prev_k: float, prev_d: float, k: float, d: float) -> bool:
    return prev_k <= prev_d and k > d


def _cross_down(prev_k: float, prev_d: float, k: float, d: float) -> bool:
    return prev_k >= prev_d and k < d


def _trend_score(close: float, ma5: float, ma10: float, ma20: float, ma60: float) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if ma20 and ma60 and close > ma20 > ma60:
        score += 3
        reasons.append("price above MA20 and MA60")
    elif ma20 and ma60 and close < ma20 < ma60:
        score -= 3
        reasons.append("price below MA20 and MA60")

    if ma5 and ma10 and ma5 > ma10:
        score += 1
        reasons.append("MA5 above MA10")
    elif ma5 and ma10 and ma5 < ma10:
        score -= 1
        reasons.append("MA5 below MA10")

    if ma20 and close > ma20:
        score += 1
    elif ma20:
        score -= 1

    if ma60 and close < ma60:
        score -= 1

    return score, reasons


def analyze_market(symbol: str, candles: Sequence[Candle]) -> MarketContext:
    closes = [item.close for item in candles]
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    ma20_prev = _sma(closes[:-1], 20) if len(closes) > 20 else 0.0
    k, d, j, prev_k, prev_d, _ = _kdj(candles)

    score, reasons = _trend_score(closes[-1], _sma(closes, 5), _sma(closes, 10), ma20, ma60)
    if ma20 and ma20_prev and ma20 > ma20_prev:
        score += 1
        reasons.append("MA20 rising")
    elif ma20 and ma20_prev and ma20 < ma20_prev:
        score -= 1
        reasons.append("MA20 falling")

    if _cross_up(prev_k, prev_d, k, d):
        score += 1
        reasons.append("KDJ crossed up")
    elif _cross_down(prev_k, prev_d, k, d):
        score -= 1
        reasons.append("KDJ crossed down")

    if k < 20 and d < 30:
        score += 1
        reasons.append("KDJ oversold zone")
    if j > 90:
        score -= 1
        reasons.append("J overbought")

    action = "buy" if score >= 2 else "sell" if score <= -2 else "hold"
    return MarketContext(
        symbol=symbol,
        action=action,
        score=score,
        close=closes[-1],
        ma20=ma20,
        ma60=ma60,
        k=k,
        d=d,
        j=j,
        reasons=reasons,
    )


def analyze_symbol(symbol: str, candles: Sequence[Candle], market_bias: int = 0) -> TechnicalSignal:
    closes = [item.close for item in candles]
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    k, d, j, prev_k, prev_d, _ = _kdj(candles)
    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else candles[-1]

    score, reasons = _trend_score(last.close, ma5, ma10, ma20, ma60)

    if _cross_up(prev_k, prev_d, k, d):
        score += 2
        reasons.append("KDJ golden cross")
    elif _cross_down(prev_k, prev_d, k, d):
        score -= 2
        reasons.append("KDJ death cross")

    if k < 20 and d < 30 and k > d:
        score += 2
        reasons.append("KDJ low-zone turn up")
    elif k > 80 and d > 80 and k < d:
        score -= 2
        reasons.append("KDJ high-zone turn down")

    doji = _is_doji(last)
    if doji:
        reasons.append("doji")
        if ma20 and abs(last.close - ma20) / ma20 <= 0.02 and score >= 0:
            score += 2
            reasons.append("doji near MA20")
        elif ma20 and last.close >= ma20:
            score += 1
            reasons.append("doji above MA20")
        elif ma20 and last.close < ma20:
            score -= 1
            reasons.append("doji below MA20")

    if prev.close < last.close and last.close > ma5:
        score += 1
        reasons.append("close reclaimed MA5")
    if prev.close > last.close and last.close < ma5:
        score -= 1
        reasons.append("close lost MA5")

    score += market_bias
    if market_bias:
        reasons.append(f"market bias {market_bias:+d}")

    action = "buy"
    if score <= -3:
        action = "sell"
    elif score < 2:
        action = "hold"

    return TechnicalSignal(
        symbol=symbol,
        action=action,
        score=score,
        market_bias=market_bias,
        close=last.close,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        ma60=ma60,
        k=k,
        d=d,
        j=j,
        doji=doji,
        reasons=reasons,
    )


def _fetch_history_candles(quote_ctx: Any, symbol: str, count: int = 90, period: Any = None, adjust_type: Any = None) -> List[Candle]:
    if period is None:
        if Period is None:
            raise RuntimeError("longbridge.openapi.Period is unavailable")
        period = Period.Day
    if adjust_type is None:
        if AdjustType is None:
            raise RuntimeError("longbridge.openapi.AdjustType is unavailable")
        adjust_type = AdjustType.NoAdjust
    raw = quote_ctx.history_candlesticks_by_offset(symbol, period, adjust_type, False, count)
    candles = _extract_candles(raw)
    candles = [item for item in candles if item.close and item.high and item.low]
    candles.sort(key=lambda item: _timestamp_key(item.timestamp))
    return candles


def select_trade(symbols: Sequence[str], quote_ctx: Any, market_proxy: str = "SPY.US", lookback: int = 90) -> dict[str, Any]:
    market_candles = _fetch_history_candles(quote_ctx, market_proxy, count=lookback)
    market_ctx = analyze_market(market_proxy, market_candles)

    market_bias = max(min(market_ctx.score, 3), -3)
    if market_ctx.action == "sell":
        market_bias = min(market_bias - 1, -2)

    signals: list[TechnicalSignal] = []
    for symbol in symbols:
        candles = _fetch_history_candles(quote_ctx, symbol, count=lookback)
        if len(candles) < 20:
            continue
        signals.append(analyze_symbol(symbol, candles, market_bias=market_bias))

    signals.sort(key=lambda item: (item.score, item.market_bias), reverse=True)
    best = signals[0] if signals else None
    return {
        "market": market_ctx,
        "signals": signals,
        "best": best,
        "actionable": best is not None and best.action in {"buy", "sell"} and best.score >= 2,
    }
