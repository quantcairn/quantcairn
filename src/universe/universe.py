from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, log, sqrt
from statistics import mean, pstdev
from typing import Any, Sequence

try:  # pragma: no cover
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


def _normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if "." in symbol:
        return symbol
    return f"{symbol}.US" if symbol else symbol


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return default


def _as_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    for name in ("items", "data", "records", "securities", "security_list", "list"):
        value = _attr(raw, name)
        if value is not None:
            try:
                return list(value)
            except Exception:
                return [value]
    try:
        return list(raw)
    except Exception:
        return [raw]


def _ts_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, float)):
        return (0, float(value))
    try:
        return (0, float(str(value)))
    except Exception:
        return (1, str(value))


def _volatility(closes: Sequence[float]) -> float:
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


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    price: float
    avg_volume_20d: float
    market_cap: float
    volatility_30d: float
    volatility_rank: float
    filter_reasons: list[str]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UniverseScanner:
    def __init__(
        self,
        quote_ctx: Any,
        market: str = "US",
        category: str = "stock",
        candidate_limit: int = 200,
        min_price: float = 5.0,
        min_avg_volume: float = 1_000_000.0,
        min_market_cap: float = 1_000_000_000.0,
        top_volatility_pct: float = 0.30,
    ):
        self.quote_ctx = quote_ctx
        self.market = market
        self.category = category
        self.candidate_limit = candidate_limit
        self.min_price = min_price
        self.min_avg_volume = min_avg_volume
        self.min_market_cap = min_market_cap
        self.top_volatility_pct = top_volatility_pct

    def _candles(self, symbol: str, count: int = 30):
        if Period is None or AdjustType is None:
            raise RuntimeError("longbridge.openapi Period/AdjustType unavailable")
        raw = self.quote_ctx.history_candlesticks_by_offset(symbol, Period.Day, AdjustType.NoAdjust, False, count)
        candles = _as_list(raw)
        candles.sort(key=lambda item: _ts_key(_attr(item, "timestamp", "time", "date", default=None)))
        return candles

    def _security_candidates(self) -> list[str]:
        if not hasattr(self.quote_ctx, "security_list"):
            return []
        try:
            raw = self.quote_ctx.security_list(self.market, self.category)
        except Exception:
            return []
        symbols: list[str] = []
        for item in _as_list(raw):
            symbol = _attr(item, "symbol", "ticker", "code", default=None)
            if symbol:
                symbols.append(_normalize_symbol(str(symbol)))
            if len(symbols) >= self.candidate_limit:
                break
        return symbols

    def _static_info(self, symbols: Sequence[str]) -> dict[str, Any]:
        if not hasattr(self.quote_ctx, "static_info"):
            return {}
        try:
            raw = self.quote_ctx.static_info(list(symbols))
        except Exception:
            return {}
        mapping: dict[str, Any] = {}
        records = _as_list(raw)
        for item in records:
            symbol = _attr(item, "symbol", "ticker", "code", default=None)
            if symbol:
                mapping[_normalize_symbol(str(symbol))] = item
        return mapping

    def scan(self, symbols: Sequence[str] | None = None) -> list[UniverseEntry]:
        candidates = [_normalize_symbol(symbol) for symbol in (symbols or self._security_candidates())]
        if self.candidate_limit:
            candidates = candidates[: self.candidate_limit]
        if not candidates:
            return []

        static_map = self._static_info(candidates)
        entries: list[UniverseEntry] = []
        for symbol in candidates:
            info = static_map.get(symbol)
            candles = self._candles(symbol, 30)
            closes = [_f(_attr(item, "close", "close_price", "c"), 0.0) for item in candles]
            volumes = [_f(_attr(item, "volume", "vol", "v"), 0.0) for item in candles]
            if not closes:
                continue

            price = _f(_attr(info, "last_done", "last", "close", "close_price", "price", default=closes[-1]), closes[-1])
            avg_volume_20d = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes) if volumes else 0.0
            market_cap = _f(
                _attr(
                    info,
                    "market_cap",
                    "market_capitalization",
                    "total_market_cap",
                    "capitalization",
                    "market_value",
                    default=0.0,
                ),
                0.0,
            )
            vol_30d = _volatility(closes)

            reasons: list[str] = []
            passed = True
            if price <= self.min_price:
                passed = False
                reasons.append(f"price <= {self.min_price}")
            if avg_volume_20d <= self.min_avg_volume:
                passed = False
                reasons.append("avg volume below threshold")
            if market_cap and market_cap <= self.min_market_cap:
                passed = False
                reasons.append("market cap below threshold")
            if not market_cap:
                reasons.append("market cap unavailable")

            entries.append(
                UniverseEntry(
                    symbol=symbol,
                    price=price,
                    avg_volume_20d=avg_volume_20d,
                    market_cap=market_cap,
                    volatility_30d=vol_30d,
                    volatility_rank=0.0,
                    filter_reasons=reasons,
                    passed=passed,
                )
            )

        passed = [entry for entry in entries if entry.passed]
        passed.sort(key=lambda item: item.volatility_30d, reverse=True)
        if passed:
            keep = max(1, ceil(len(passed) * self.top_volatility_pct))
            selected = passed[:keep]
            threshold = selected[-1].volatility_30d if selected else 0.0
            updated: list[UniverseEntry] = []
            for entry in entries:
                if not entry.passed:
                    updated.append(entry)
                    continue
                rank = entry.volatility_30d / threshold if threshold else 0.0
                if entry in selected:
                    updated.append(
                        UniverseEntry(
                            symbol=entry.symbol,
                            price=entry.price,
                            avg_volume_20d=entry.avg_volume_20d,
                            market_cap=entry.market_cap,
                            volatility_30d=entry.volatility_30d,
                            volatility_rank=rank,
                            filter_reasons=entry.filter_reasons,
                            passed=True,
                        )
                    )
                else:
                    updated.append(
                        UniverseEntry(
                            symbol=entry.symbol,
                            price=entry.price,
                            avg_volume_20d=entry.avg_volume_20d,
                            market_cap=entry.market_cap,
                            volatility_30d=entry.volatility_30d,
                            volatility_rank=rank,
                            filter_reasons=entry.filter_reasons + ["volatility rank outside top 30%"],
                            passed=False,
                        )
                    )
            entries = updated

        entries.sort(key=lambda item: (item.passed, item.volatility_30d, item.avg_volume_20d), reverse=True)
        return entries
