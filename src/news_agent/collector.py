from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, Sequence
from urllib.parse import quote_plus
import os
import re
import xml.etree.ElementTree as ET

import requests


POSITIVE_TERMS = {
    "beat",
    "beats",
    "bullish",
    "buyback",
    "growth",
    "guidance raised",
    "improves",
    "outperform",
    "profit",
    "raise",
    "raises",
    "record",
    "strong",
    "surge",
    "upgrade",
    "upside",
    "windfall",
}

NEGATIVE_TERMS = {
    "bearish",
    "downgrade",
    "loss",
    "miss",
    "misses",
    "probe",
    "regulatory",
    "lawsuit",
    "weak",
    "warning",
    "guidance cut",
    "cuts",
    "decline",
    "dilution",
    "fraud",
    "investigation",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _base_symbol(symbol: str) -> str:
    return symbol.split(".")[0].upper()


def _textify(*parts: Any) -> str:
    items = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, str):
            items.append(part)
        else:
            items.append(str(part))
    return " ".join(items).strip()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value = value / 1000.0
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc)
    except Exception:
        return None


def _recency_weight(published_at: datetime | None) -> float:
    if published_at is None:
        return 1.0
    hours = max((_now() - published_at).total_seconds() / 3600.0, 0.0)
    if hours <= 6:
        return 1.4
    if hours <= 24:
        return 1.2
    if hours <= 72:
        return 1.0
    if hours <= 168:
        return 0.85
    return 0.7


def _source_weight(source: str) -> float:
    source = source.lower()
    if "sec" in source or "filing" in source:
        return 1.25
    if "news" in source:
        return 1.0
    if "reddit" in source:
        return 0.8
    if "social" in source or "stocktwits" in source:
        return 0.75
    return 1.0


@dataclass(frozen=True)
class NewsItem:
    symbol: str
    source: str
    title: str
    summary: str = ""
    url: str = ""
    published_at: datetime | None = None

    @property
    def text(self) -> str:
        return _textify(self.title, self.summary)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.published_at is not None:
            data["published_at"] = self.published_at.isoformat()
        return data


@dataclass(frozen=True)
class NewsSentiment:
    symbol: str
    score: float
    item_count: int
    positive_hits: int
    negative_hits: int
    reasons: list[str]
    items: list[NewsItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": self.score,
            "item_count": self.item_count,
            "positive_hits": self.positive_hits,
            "negative_hits": self.negative_hits,
            "reasons": list(self.reasons),
            "items": [item.to_dict() for item in self.items],
        }


class BaseNewsProvider:
    source_name = "news"

    def fetch(self, symbol: str, quote_ctx: Any | None = None) -> list[NewsItem]:
        return []


class LongBridgeFilingsProvider(BaseNewsProvider):
    source_name = "sec"

    def fetch(self, symbol: str, quote_ctx: Any | None = None) -> list[NewsItem]:
        if quote_ctx is None or not hasattr(quote_ctx, "filings"):
            return []
        try:
            raw = quote_ctx.filings(symbol)
        except Exception:
            return []
        items: list[NewsItem] = []
        records = raw if isinstance(raw, (list, tuple)) else _attr(raw, "items", "data", "records", "filings", default=[])
        try:
            iterator = list(records)
        except Exception:
            iterator = [records] if records else []
        for rec in iterator:
            title = _attr(rec, "title", "headline", "name", default="")
            summary = _attr(rec, "content", "summary", "body", "description", default="")
            url = _attr(rec, "url", "link", default="")
            published = _parse_dt(_attr(rec, "published_at", "publishedAt", "date", "time", default=None))
            if title or summary:
                items.append(NewsItem(symbol=_normalize_symbol(symbol), source=self.source_name, title=str(title), summary=str(summary), url=str(url or ""), published_at=published))
        return items


class GoogleNewsRssProvider(BaseNewsProvider):
    source_name = "news"

    def __init__(self, timeout: float = 8.0, enabled: bool = True):
        self.timeout = timeout
        self.enabled = enabled

    def fetch(self, symbol: str, quote_ctx: Any | None = None) -> list[NewsItem]:
        if not self.enabled:
            return []
        query = quote_plus(f"{_base_symbol(symbol)} stock")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(url, timeout=self.timeout, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except Exception:
            return []
        items: list[NewsItem] = []
        for item in root.findall(".//item")[:12]:
            title = (item.findtext("title") or "").strip()
            summary = (item.findtext("description") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = _parse_dt(item.findtext("pubDate"))
            if title:
                items.append(NewsItem(symbol=_normalize_symbol(symbol), source=self.source_name, title=title, summary=summary, url=link, published_at=pub))
        return items


class RedditSearchProvider(BaseNewsProvider):
    source_name = "reddit"

    def __init__(self, timeout: float = 8.0, enabled: bool = True):
        self.timeout = timeout
        self.enabled = enabled

    def fetch(self, symbol: str, quote_ctx: Any | None = None) -> list[NewsItem]:
        if not self.enabled:
            return []
        query = quote_plus(f"{_base_symbol(symbol)} stock")
        url = f"https://www.reddit.com/search.json?q={query}&sort=new&limit=10&restrict_sr=false"
        try:
            resp = requests.get(url, timeout=self.timeout, headers={"User-Agent": "soxs-range-arbitrage/1.0"})
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []
        items: list[NewsItem] = []
        children = _attr(payload, "data", default={})
        children = _attr(children, "children", default=[]) if children else []
        for child in children[:10]:
            data = _attr(child, "data", default=child)
            title = _attr(data, "title", default="")
            summary = _attr(data, "selftext", "body", default="")
            url = _attr(data, "url", "permalink", default="")
            pub = _parse_dt(_attr(data, "created_utc", "created", default=None))
            if title:
                items.append(NewsItem(symbol=_normalize_symbol(symbol), source=self.source_name, title=str(title), summary=str(summary), url=str(url or ""), published_at=pub))
        return items


class StockTwitsProvider(BaseNewsProvider):
    source_name = "social"

    def __init__(self, timeout: float = 8.0, enabled: bool = True):
        self.timeout = timeout
        self.enabled = enabled

    def fetch(self, symbol: str, quote_ctx: Any | None = None) -> list[NewsItem]:
        if not self.enabled:
            return []
        base = _base_symbol(symbol)
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{base}.json"
        try:
            resp = requests.get(url, timeout=self.timeout, headers={"User-Agent": "soxs-range-arbitrage/1.0"})
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []
        items: list[NewsItem] = []
        messages = _attr(payload, "messages", default=[])
        for message in messages[:12]:
            body = _attr(message, "body", default="")
            user = _attr(_attr(message, "user", default={}), "username", default="")
            posted = _parse_dt(_attr(message, "created_at", "created", default=None))
            title = f"{user}: {body}" if user else str(body)
            if title:
                items.append(NewsItem(symbol=_normalize_symbol(symbol), source=self.source_name, title=title, summary="", url="", published_at=posted))
        return items


def _sentiment_score(text: str) -> tuple[int, int]:
    lower = text.lower()
    positive = sum(lower.count(term) for term in POSITIVE_TERMS)
    negative = sum(lower.count(term) for term in NEGATIVE_TERMS)
    return positive, negative


def score_news_items(symbol: str, items: Sequence[NewsItem]) -> NewsSentiment:
    if not items:
        return NewsSentiment(symbol=_normalize_symbol(symbol), score=50.0, item_count=0, positive_hits=0, negative_hits=0, reasons=["no news data"], items=[])

    weighted_scores = []
    total_positive = 0
    total_negative = 0
    reasons: list[str] = []
    for item in items:
        positive, negative = _sentiment_score(item.text)
        total_positive += positive
        total_negative += negative
        base = 50.0 + positive * 7.5 - negative * 7.5
        base += 3.0 if len(item.title) < 80 else 0.0
        weighted = base * _source_weight(item.source) * _recency_weight(item.published_at)
        weighted_scores.append((weighted, item))
        if positive > negative:
            reasons.append(f"{item.source}: bullish mention")
        elif negative > positive:
            reasons.append(f"{item.source}: bearish mention")

    if not weighted_scores:
        return NewsSentiment(symbol=_normalize_symbol(symbol), score=50.0, item_count=0, positive_hits=0, negative_hits=0, reasons=["no news data"], items=[])

    raw = sum(weight for weight, _ in weighted_scores) / len(weighted_scores)
    raw += min(8.0, len(items) * 1.2)
    raw -= min(6.0, max(0, len(items) - 8) * 0.4)
    if total_positive > total_negative:
        raw += 4.0
    elif total_negative > total_positive:
        raw -= 4.0

    score = max(0.0, min(100.0, raw))
    if not reasons:
        reasons.append("neutral news mix")
    if len(items) >= 5:
        reasons.append("strong coverage")
    return NewsSentiment(
        symbol=_normalize_symbol(symbol),
        score=score,
        item_count=len(items),
        positive_hits=total_positive,
        negative_hits=total_negative,
        reasons=reasons,
        items=list(items),
    )


class NewsCollector:
    def __init__(self, providers: Sequence[BaseNewsProvider] | None = None, enable_web_sources: bool | None = None, timeout: float = 8.0):
        if enable_web_sources is None:
            enable_web_sources = os.getenv("AI_SELECTOR_ENABLE_WEB_SOURCES", "1").strip().lower() not in {"0", "false", "no", "off"}
        self.enable_web_sources = enable_web_sources
        if providers is None:
            providers = [
                LongBridgeFilingsProvider(),
                GoogleNewsRssProvider(timeout=timeout, enabled=self.enable_web_sources),
                RedditSearchProvider(timeout=timeout, enabled=self.enable_web_sources),
                StockTwitsProvider(timeout=timeout, enabled=self.enable_web_sources),
            ]
        self.providers = list(providers)

    def collect(self, symbols: Sequence[str], quote_ctx: Any | None = None) -> dict[str, list[NewsItem]]:
        collected: dict[str, list[NewsItem]] = {_normalize_symbol(symbol): [] for symbol in symbols}
        for symbol in symbols:
            norm = _normalize_symbol(symbol)
            for provider in self.providers:
                try:
                    collected[norm].extend(provider.fetch(norm, quote_ctx=quote_ctx))
                except Exception:
                    continue
        return collected

    def score(self, symbol: str, items: Sequence[NewsItem]) -> NewsSentiment:
        return score_news_items(symbol, items)
