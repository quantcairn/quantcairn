from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from src.news_agent.collector import NewsCollector, NewsSentiment, score_news_items
from src.scoring.technical import TechnicalAnalysis, analyze_market, analyze_technical
from src.universe.universe import UniverseEntry, UniverseScanner


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if "." in symbol:
        return symbol
    return f"{symbol}.US" if symbol else symbol


def _safe_name(symbol: str) -> str:
    return symbol.replace("/", "-").replace(".", "_")


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            return f"{value:.4f}".rstrip("0").rstrip(".")
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    if any(ch in text for ch in [":", "#", "{", "}", "[", "]", ",", "&", "*", "?", "|", ">", "%", "@", "`"]) or text.strip() != text or " " in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _dump_yaml(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_dump_yaml(item, indent + 2))
            elif isinstance(item, list) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_dump_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}-")
                lines.append(_dump_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_yaml_scalar(value)}"


@dataclass(frozen=True)
class SelectionRow:
    symbol: str
    total_score: float
    technical_score: float
    news_score: float
    volume_score: float
    risk_score: float
    action: str
    price: float
    avg_volume_20d: float
    market_cap: float
    volatility_30d: float
    technical: TechnicalAnalysis
    news: NewsSentiment
    universe: UniverseEntry
    reasons: list[str]
    market_bias: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_score": self.total_score,
            "technical_score": self.technical_score,
            "news_score": self.news_score,
            "volume_score": self.volume_score,
            "risk_score": self.risk_score,
            "action": self.action,
            "price": self.price,
            "avg_volume_20d": self.avg_volume_20d,
            "market_cap": self.market_cap,
            "volatility_30d": self.volatility_30d,
            "technical": self.technical.to_dict(),
            "news": self.news.to_dict(),
            "universe": self.universe.to_dict(),
            "reasons": list(self.reasons),
            "market_bias": self.market_bias,
        }


@dataclass(frozen=True)
class SelectionReport:
    generated_at: str
    market_proxy: str
    market: TechnicalAnalysis
    universe_count: int
    screened_count: int
    rows: list[SelectionRow]

    @property
    def top10(self) -> list[SelectionRow]:
        return self.rows[:10]

    @property
    def top3(self) -> list[SelectionRow]:
        return self.rows[:3]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "market_proxy": self.market_proxy,
            "market": self.market.to_dict(),
            "universe_count": self.universe_count,
            "screened_count": self.screened_count,
            "rows": [row.to_dict() for row in self.rows],
            "top10": [row.to_dict() for row in self.top10],
            "top3": [row.to_dict() for row in self.top3],
        }


def _volume_score(entry: UniverseEntry) -> float:
    if entry.avg_volume_20d <= 0:
        return 0.0
    liquidity = min(100.0, math.log10(entry.avg_volume_20d / 1_000_000.0 + 1.0) * 55.0 + 45.0)
    volatility_bonus = min(15.0, entry.volatility_30d / 4.0)
    return _clamp(liquidity + volatility_bonus)


def _risk_score(technical: TechnicalAnalysis) -> float:
    if technical.close <= 0:
        return 0.0
    atr_pct = technical.atr14 / technical.close * 100.0 if technical.atr14 else 0.0
    support_gap = (technical.close - technical.support) / technical.close * 100.0 if technical.support else 0.0
    resistance_gap = (technical.resistance - technical.close) / technical.close * 100.0 if technical.resistance else 0.0

    score = 100.0
    if atr_pct > 8:
        score -= 30
    elif atr_pct > 5:
        score -= 18
    elif atr_pct < 1.2:
        score += 8

    if support_gap < 0:
        score -= 20
    elif support_gap < 2:
        score -= 10
    elif support_gap > 6:
        score += 4

    if resistance_gap < 0:
        score -= 15
    elif resistance_gap < 2:
        score -= 8
    elif resistance_gap > 6:
        score += 3

    if technical.trend == "sell":
        score -= 10
    elif technical.trend == "buy":
        score += 5

    return _clamp(score)


def _trade_range(technical: TechnicalAnalysis) -> tuple[float, float, dict[str, float]]:
    atr = technical.atr14 or max(technical.close * 0.02, 0.01)
    support = technical.support or technical.close - atr
    resistance = technical.resistance or technical.close + atr
    low = max(support, technical.close - atr * 1.2)
    high = min(resistance, technical.close + atr * 1.6)
    if high <= low:
        low = max(0.01, technical.close - atr)
        high = technical.close + atr * 1.8
    stop_loss = max(0.01, low - atr * 0.6)
    take_profit = max(high + atr * 0.8, technical.close + atr * 2.0)
    return round(low, 2), round(high, 2), {
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
        "atr": round(atr, 2),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "atr_stop_multiple": 1.2,
        "atr_take_multiple": 2.0,
        "max_position_usd": 1000,
    }


def _range_state(price: float, low: float, high: float) -> tuple[str, bool]:
    if low <= price <= high:
        return "区间触发中", True
    return "未触发", False


class AIStockSelector:
    def __init__(
        self,
        quote_ctx: Any,
        news_collector: NewsCollector | None = None,
        candidate_limit: int = 200,
        market_proxy: str = "SPY.US",
    ):
        self.quote_ctx = quote_ctx
        self.news_collector = news_collector or NewsCollector()
        self.candidate_limit = candidate_limit
        self.market_proxy = _normalize_symbol(market_proxy)

    def _history(self, symbol: str, count: int = 240) -> list[Any]:
        if not hasattr(self.quote_ctx, "history_candlesticks_by_offset"):
            return []
        if not hasattr(self.quote_ctx, "history_candlesticks_by_offset"):
            return []
        from longbridge.openapi import AdjustType, Period  # local import to keep module load light

        try:
            raw = self.quote_ctx.history_candlesticks_by_offset(symbol, Period.Day, AdjustType.NoAdjust, False, count)
        except Exception:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        for attr in ("candlesticks", "candles", "items", "data", "records", "history"):
            value = getattr(raw, attr, None)
            if value is not None:
                try:
                    return list(value)
                except Exception:
                    return [value]
        try:
            return list(raw)
        except Exception:
            return []

    def _market_bias(self) -> tuple[TechnicalAnalysis, float]:
        market = analyze_market(self._history(self.market_proxy, 240), symbol=self.market_proxy)
        bias = _clamp((market.score - 50.0) / 10.0, -5.0, 5.0)
        return market, bias

    def _news(self, symbols: Sequence[str]) -> dict[str, NewsSentiment]:
        items_map = self.news_collector.collect(symbols, quote_ctx=self.quote_ctx)
        return {symbol: self.news_collector.score(symbol, items_map.get(symbol, [])) for symbol in symbols}

    def _universe(self, symbols: Sequence[str] | None = None) -> list[UniverseEntry]:
        scanner = UniverseScanner(self.quote_ctx, candidate_limit=self.candidate_limit)
        return scanner.scan(symbols=symbols)

    def run(self, symbols: Sequence[str] | None = None) -> SelectionReport:
        universe = self._universe(symbols=symbols)
        market, market_bias = self._market_bias()
        candidate_symbols = [entry.symbol for entry in universe]
        news_map = self._news(candidate_symbols)

        rows: list[SelectionRow] = []
        for entry in universe:
            candles = self._history(entry.symbol, 240)
            technical = analyze_technical(candles, symbol=entry.symbol, market_bias=market_bias)
            news = news_map.get(entry.symbol) or self.news_collector.score(entry.symbol, [])
            volume_score = _volume_score(entry)
            risk_score = _risk_score(technical)
            total = _clamp(
                technical.score * 0.4 + news.score * 0.3 + volume_score * 0.2 + risk_score * 0.1
            )
            action = (
                "buy"
                if entry.passed and total >= 70 and technical.trend != "sell"
                else "sell"
                if entry.passed and total <= 40 and technical.trend == "sell"
                else "hold"
            )
            reasons = list(technical.reasons) + list(news.reasons)
            if entry.filter_reasons:
                reasons.extend(entry.filter_reasons)
            rows.append(
                SelectionRow(
                    symbol=entry.symbol,
                    total_score=round(total, 2),
                    technical_score=round(technical.score, 2),
                    news_score=round(news.score, 2),
                    volume_score=round(volume_score, 2),
                    risk_score=round(risk_score, 2),
                    action=action,
                    price=entry.price,
                    avg_volume_20d=entry.avg_volume_20d,
                    market_cap=entry.market_cap,
                    volatility_30d=entry.volatility_30d,
                    technical=technical,
                    news=news,
                    universe=entry,
                    reasons=reasons,
                    market_bias=market_bias,
                )
            )

        rows.sort(key=lambda item: (item.total_score, item.technical_score, item.news_score), reverse=True)
        return SelectionReport(
            generated_at=_now().isoformat(),
            market_proxy=self.market_proxy,
            market=market,
            universe_count=len(universe),
            screened_count=len(rows),
            rows=rows,
        )


def write_top_configs(report: SelectionReport, config_dir: str | os.PathLike[str]) -> list[Path]:
    config_root = Path(config_dir)
    config_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    top3 = list(report.top3)
    if len(top3) < 3:
        for row in report.top10:
            if row not in top3:
                top3.append(row)
            if len(top3) == 3:
                break

    for index, row in enumerate(top3[:3], start=1):
        low, high, risk = _trade_range(row.technical)
        state_label, triggered = _range_state(row.price, low, high)
        payload = {
            "ticker": row.symbol,
            "generated_at": report.generated_at,
            "market_proxy": report.market_proxy,
            "status": state_label,
            "signal": {
                "action": row.action,
                "total_score": row.total_score,
                "technical_score": row.technical_score,
                "news_score": row.news_score,
                "volume_score": row.volume_score,
                "risk_score": row.risk_score,
                "range_state": state_label,
            },
            "range": {
                "low": low,
                "high": high,
                "state": state_label,
                "triggered": triggered,
                "price": round(row.price, 2),
            },
            "risk": risk,
            "metrics": {
                "price": round(row.price, 2),
                "avg_volume_20d": round(row.avg_volume_20d, 2),
                "market_cap": round(row.market_cap, 2),
                "volatility_30d": round(row.volatility_30d, 2),
                "technical": row.technical.to_dict(),
                "news": row.news.to_dict(),
            },
        }
        path = config_root / f"TOP{index}.yaml"
        path.write_text(_dump_yaml(payload) + "\n", encoding="utf-8")
        written.append(path)
    return written


def write_report(report: SelectionReport, output_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    day = report.generated_at[:10]
    json_path = out_root / f"ai_stock_selection-{day}.json"
    md_path = out_root / f"ai_stock_selection-{day}.md"
    json_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    lines = [
        f"# AI Stock Selection Report ({day})",
        "",
        f"- Market proxy: `{report.market_proxy}`",
        f"- Universe size: `{report.universe_count}`",
        f"- Screened symbols: `{report.screened_count}`",
        f"- Market state: `{report.market.trend}` score `{report.market.score:.1f}`",
        "",
        "## Top10",
        "",
        "| Rank | Ticker | Total | Tech | News | Volume | Risk | Action | Price | Vol30D |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    for idx, row in enumerate(report.top10, start=1):
        lines.append(
            f"| {idx} | {row.symbol} | {row.total_score:.1f} | {row.technical_score:.1f} | {row.news_score:.1f} | {row.volume_score:.1f} | {row.risk_score:.1f} | {row.action} | {row.price:.2f} | {row.volatility_30d:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Top3 Trading Symbols",
            "",
        ]
    )
    for idx, row in enumerate(report.top3, start=1):
        low, high, _ = _trade_range(row.technical)
        state_label, _ = _range_state(row.price, low, high)
        lines.append(
            f"{idx}. **{row.symbol}** - score `{row.total_score:.1f}` action `{row.action}` state `{state_label}` range `{low:.2f}` to `{high:.2f}`"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
