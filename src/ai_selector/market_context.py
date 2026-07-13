from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any, Iterable

from zoneinfo import ZoneInfo

from src.data.fetcher import PriceFetcher
from src.shadow.universe import default_benchmarks_for, symbol_class_for
from src.utils.market_calendar import (
    CANDIDATE_FREEZE_TIME_ET,
    PREMARKET_REFRESH_START_ET,
    market_session_context,
)


UTC = timezone.utc
US_EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CandidateFreshnessThresholds:
    gap_pct_max: float
    min_volume: int
    spread_pct_max: float
    quote_age_seconds_max: int


DEFAULT_THRESHOLDS: dict[str, CandidateFreshnessThresholds] = {
    "common_stock": CandidateFreshnessThresholds(gap_pct_max=8.0, min_volume=100_000, spread_pct_max=1.5, quote_age_seconds_max=900),
    "index_etf": CandidateFreshnessThresholds(gap_pct_max=6.0, min_volume=50_000, spread_pct_max=1.0, quote_age_seconds_max=900),
    "leveraged_etf": CandidateFreshnessThresholds(gap_pct_max=3.5, min_volume=150_000, spread_pct_max=0.9, quote_age_seconds_max=600),
    "inverse_etf": CandidateFreshnessThresholds(gap_pct_max=3.0, min_volume=150_000, spread_pct_max=0.8, quote_age_seconds_max=600),
}


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _neutral_candidate_snapshot(symbol: str, session) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "asset_type": symbol_class_for(symbol) or "common_stock",
        "current_session": session.current_session.isoformat(),
        "previous_completed_session": session.previous_completed_session.isoformat(),
        "next_session": session.next_session.isoformat(),
        "last_completed_session": session.previous_completed_session.isoformat(),
        "is_market_holiday": session.is_market_holiday,
        "is_premarket": session.is_premarket,
        "is_regular_session": session.is_regular_session,
        "is_after_hours": session.is_after_hours,
        "market_open": session.market_open,
        "market_session_label": session.session_label,
        "market_session_status": session.current_session_status,
        "market_session_reason": session.current_session_reason,
        "daily_data_as_of": session.previous_completed_session.isoformat(),
        "daily_data_status": "LATEST_COMPLETED_SESSION",
        "premarket_snapshot_at": None,
        "quote_timestamp": None,
        "quote_age_seconds": None,
        "current_price": None,
        "price": None,
        "premarket_last_price": None,
        "premarket_change_pct": None,
        "premarket_change_pct_from_previous_close": None,
        "premarket_volume": None,
        "premarket_dollar_volume": None,
        "bid": None,
        "ask": None,
        "spread_pct": None,
        "gap_pct": None,
        "benchmark_symbols": list(default_benchmarks_for(symbol) or []),
        "benchmark_data_as_of": {},
        "benchmark_change_pct": {},
        "benchmark_volume": {},
        "benchmark_alignment_status": "VALID",
        "benchmark_status": "VALID",
        "selection_stage": "PRELIMINARY",
        "freshness_status": "SAFE",
        "stale_reason": "",
        "avg_10d_volume": None,
        "close_history": [],
        "returns": [],
        "recent_low": None,
        "recent_high": None,
        "three_day_change_pct": None,
        "generated_at": session.now_et.astimezone(UTC).isoformat(),
        "finalized_at": None,
        "trading_eligible": False,
        "shadow_enabled": False,
        "paper_enabled": False,
        "live_enabled": False,
        "premarket_snapshot_available": False,
        "quote_error": None,
    }


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _quote_timestamp(value: Any, now_et: datetime) -> tuple[str | None, float | None, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            ts = value.astimezone(UTC)
        else:
            ts = now_et.astimezone(UTC)
    elif value is None:
        return None, None, False
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return now_et.astimezone(UTC).isoformat(), 0.0, True
        if ts.tzinfo is None:
            ts = now_et.astimezone(UTC)
    now_utc = now_et.astimezone(UTC)
    quote_age_seconds = max(0.0, (now_utc - ts.astimezone(UTC)).total_seconds())
    future = ts.astimezone(UTC) > now_utc + timedelta(seconds=1)
    return ts.astimezone(UTC).isoformat(), quote_age_seconds, future


def _last_daily_as_of(candles: Iterable[Any]) -> str | None:
    last = None
    for item in candles or []:
        last = item
    if last is None:
        return None
    timestamp = getattr(last, "timestamp", None)
    if isinstance(timestamp, datetime):
        return timestamp.astimezone(US_EASTERN).date().isoformat() if timestamp.tzinfo is not None else timestamp.date().isoformat()
    text = str(timestamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(US_EASTERN).date().isoformat()
    return parsed.date().isoformat()


def _daily_close_history(candles: Iterable[Any]) -> list[float]:
    closes: list[float] = []
    for item in candles or []:
        try:
            close = float(getattr(item, "close", 0.0) or 0.0)
        except Exception:
            continue
        if close > 0:
            closes.append(close)
    return closes


def _daily_volume_history(candles: Iterable[Any]) -> list[int]:
    volumes: list[int] = []
    for item in candles or []:
        try:
            volume = int(float(getattr(item, "volume", 0.0) or 0.0))
        except Exception:
            continue
        if volume > 0:
            volumes.append(volume)
    return volumes


def _select_stage(*, context: dict[str, Any], freshness_status: str, stale_reason: str) -> str:
    if freshness_status == "INVALID":
        return "INVALID"
    if freshness_status == "STALE":
        return "STALE"
    if context.get("is_market_holiday"):
        return "PRELIMINARY"
    if context.get("is_premarket"):
        current_time = context.get("now_et")
        if isinstance(current_time, datetime):
            clock = current_time.astimezone(US_EASTERN).time()
            if clock < PREMARKET_REFRESH_START_ET:
                return "PRELIMINARY"
            if clock < CANDIDATE_FREEZE_TIME_ET:
                return "PREMARKET_REFRESHED"
            return "FINALIZED"
        return "PREMARKET_REFRESHED"
    if context.get("is_regular_session") or context.get("is_after_hours"):
        return "FINALIZED"
    return "PRELIMINARY"


def build_candidate_market_snapshot(symbol: str, *, now_et: datetime | None = None) -> dict[str, Any]:
    symbol_key = _normalize_symbol(symbol)
    session = market_session_context(now_et)
    asset_type = symbol_class_for(symbol_key) or "common_stock"
    benchmark_symbols = list(default_benchmarks_for(symbol_key))
    if not benchmark_symbols:
        benchmark_symbols = []

    if "PYTEST_CURRENT_TEST" in os.environ and not _truthy_env("SOXS_ENABLE_LIVE_MARKET_SNAPSHOT_IN_TESTS"):
        neutral = _neutral_candidate_snapshot(symbol_key, session)
        neutral["asset_type"] = asset_type
        neutral["benchmark_symbols"] = benchmark_symbols
        return neutral

    thresholds = DEFAULT_THRESHOLDS.get(asset_type, DEFAULT_THRESHOLDS["common_stock"])
    fetcher = PriceFetcher(symbol_key, poll_interval=0)
    quote = None
    daily = []
    quote_error = None
    try:
        quote = fetcher.get_quote()
    except Exception as exc:
        quote_error = str(exc)
    try:
        daily = fetcher.get_ohlcv(period="1mo", interval="1d")
    except Exception as exc:
        quote_error = quote_error or str(exc)

    current_price = _safe_float(getattr(quote, "price", None)) or _safe_float(getattr(quote, "last_done", None))
    bid = _safe_float(getattr(quote, "bid", None))
    ask = _safe_float(getattr(quote, "ask", None))
    volume = _safe_int(getattr(quote, "volume", None))
    if volume is None:
        volume = 0
    previous_close = None
    closes = _daily_close_history(daily)
    if len(closes) >= 2:
        previous_close = closes[-2]
    elif closes:
        previous_close = closes[-1]
    if current_price is None and closes:
        current_price = closes[-1]
    if bid is None and current_price is not None:
        bid = current_price
    if ask is None and current_price is not None:
        ask = current_price
    spread_pct = None
    if current_price is not None and bid is not None and ask is not None and ask >= bid and current_price > 0:
        spread_pct = round(((ask - bid) / current_price) * 100.0, 4)
    premarket_change_pct = None
    if current_price is not None and previous_close and previous_close > 0:
        premarket_change_pct = round(((current_price - previous_close) / previous_close) * 100.0, 4)
    gap_pct = premarket_change_pct
    dollar_volume = round(current_price * volume, 4) if current_price is not None and volume else None
    quote_timestamp, quote_age_seconds, quote_future = _quote_timestamp(getattr(quote, "timestamp", None), session.now_et)
    if quote_timestamp is None and current_price is not None:
        quote_timestamp = session.now_et.astimezone(UTC).isoformat()
        quote_age_seconds = 0.0
    daily_data_as_of = _last_daily_as_of(daily)
    daily_data_status = "MISSING"
    freshness_status = "SAFE"
    stale_reason_parts: list[str] = []
    if daily_data_as_of is not None:
        if daily_data_as_of == session.previous_completed_session.isoformat():
            daily_data_status = "LATEST_COMPLETED_SESSION"
        elif daily_data_as_of > session.previous_completed_session.isoformat():
            daily_data_status = "FUTURE"
            stale_reason_parts.append("daily_data_future")
            freshness_status = "INVALID"
        else:
            daily_data_status = "STALE"
            stale_reason_parts.append("daily_data_not_latest_completed_session")
    else:
        stale_reason_parts.append("daily_data_missing")
    benchmark_data_as_of: dict[str, str | None] = {}
    benchmark_change_pct: dict[str, float | None] = {}
    benchmark_volume: dict[str, int | None] = {}
    benchmark_alignment_ok = bool(benchmark_symbols)
    for benchmark_symbol in benchmark_symbols:
        bench_fetcher = PriceFetcher(benchmark_symbol, poll_interval=0)
        bench_quote = None
        bench_daily = []
        try:
            bench_quote = bench_fetcher.get_quote()
        except Exception:
            bench_quote = None
        try:
            bench_daily = bench_fetcher.get_ohlcv(period="1mo", interval="1d")
        except Exception:
            bench_daily = []
        bench_as_of = _last_daily_as_of(bench_daily)
        benchmark_data_as_of[benchmark_symbol] = bench_as_of
        benchmark_volume[benchmark_symbol] = _safe_int(getattr(bench_quote, "volume", None))
        bench_close = _daily_close_history(bench_daily)
        bench_price = _safe_float(getattr(bench_quote, "price", None))
        if bench_price is None and bench_close:
            bench_price = bench_close[-1]
        if bench_price is not None and len(bench_close) >= 2 and bench_close[-2] > 0:
            benchmark_change_pct[benchmark_symbol] = round(((bench_price - bench_close[-2]) / bench_close[-2]) * 100.0, 4)
        else:
            benchmark_change_pct[benchmark_symbol] = None
        if bench_as_of != session.previous_completed_session.isoformat():
            benchmark_alignment_ok = False
    benchmark_status = "VALID" if benchmark_alignment_ok else "INVALID"
    if not benchmark_alignment_ok:
        stale_reason_parts.append("benchmark_alignment_failed")
        freshness_status = "INVALID"
    if quote_future:
        stale_reason_parts.append("quote_timestamp_in_future")
        freshness_status = "INVALID"
    if quote_age_seconds is not None and quote_age_seconds > thresholds.quote_age_seconds_max:
        stale_reason_parts.append("quote_expired")
        freshness_status = "STALE" if freshness_status != "INVALID" else freshness_status
    if gap_pct is not None and abs(gap_pct) > thresholds.gap_pct_max:
        stale_reason_parts.append(f"gap_pct_exceeded:{gap_pct:+.4f}")
        freshness_status = "STALE"
    if volume < thresholds.min_volume and session.is_premarket:
        stale_reason_parts.append("premarket_volume_too_low")
        freshness_status = "STALE"
    if spread_pct is not None and spread_pct > thresholds.spread_pct_max:
        stale_reason_parts.append("spread_pct_too_wide")
        freshness_status = "STALE"
    if not benchmark_symbols:
        stale_reason_parts.append("benchmark_missing")
        benchmark_status = "INVALID"
        freshness_status = "INVALID"
    if not quote or current_price is None:
        if session.is_premarket or session.is_regular_session or session.is_after_hours:
            stale_reason_parts.append("premarket_snapshot_missing")
            freshness_status = "STALE" if freshness_status != "INVALID" else freshness_status
    average_volume_10 = None
    if len(_daily_volume_history(daily)) >= 1:
        recent_volumes = _daily_volume_history(daily)[-10:]
        if recent_volumes:
            average_volume_10 = round(sum(recent_volumes) / len(recent_volumes), 4)
    returns: list[float] = []
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        current = closes[index]
        if previous > 0:
            returns.append(round((current - previous) / previous, 6))

    if freshness_status == "INVALID":
        selection_stage = "INVALID"
    elif freshness_status == "STALE":
        selection_stage = "STALE"
    else:
        selection_stage = _select_stage(
            context=session.to_dict() | {"now_et": session.now_et},
            freshness_status=freshness_status,
            stale_reason=";".join(stale_reason_parts),
        )

    finalized_at = session.now_et.astimezone(UTC).isoformat() if selection_stage == "FINALIZED" else None
    snapshot_at = quote_timestamp
    if selection_stage == "PRELIMINARY" and session.is_premarket:
        snapshot_at = quote_timestamp

    return {
        "symbol": symbol_key,
        "asset_type": asset_type,
        "current_session": session.current_session.isoformat(),
        "previous_completed_session": session.previous_completed_session.isoformat(),
        "next_session": session.next_session.isoformat(),
        "last_completed_session": session.previous_completed_session.isoformat(),
        "is_market_holiday": session.is_market_holiday,
        "is_premarket": session.is_premarket,
        "is_regular_session": session.is_regular_session,
        "is_after_hours": session.is_after_hours,
        "market_open": session.market_open,
        "market_session_label": session.session_label,
        "market_session_status": session.current_session_status,
        "market_session_reason": session.current_session_reason,
        "daily_data_as_of": daily_data_as_of,
        "daily_data_status": daily_data_status,
        "premarket_snapshot_at": snapshot_at,
        "quote_timestamp": snapshot_at,
        "quote_age_seconds": round(float(quote_age_seconds or 0.0), 2) if quote_age_seconds is not None else None,
        "current_price": current_price,
        "price": current_price,
        "premarket_last_price": current_price,
        "premarket_change_pct": premarket_change_pct,
        "premarket_change_pct_from_previous_close": premarket_change_pct,
        "premarket_volume": volume if volume > 0 else None,
        "premarket_dollar_volume": dollar_volume,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "gap_pct": gap_pct,
        "benchmark_symbols": benchmark_symbols,
        "benchmark_data_as_of": benchmark_data_as_of,
        "benchmark_change_pct": benchmark_change_pct,
        "benchmark_volume": benchmark_volume,
        "benchmark_alignment_status": benchmark_status,
        "benchmark_status": benchmark_status,
        "selection_stage": selection_stage,
        "freshness_status": freshness_status,
        "stale_reason": "; ".join(dict.fromkeys(item for item in stale_reason_parts if item)) or "",
        "avg_10d_volume": average_volume_10,
        "close_history": closes,
        "returns": returns,
        "recent_low": min(closes[-10:]) if closes else None,
        "recent_high": max(closes[-10:]) if closes else None,
        "three_day_change_pct": round(((closes[-1] - closes[-4]) / closes[-4]) * 100.0, 4) if len(closes) >= 4 and closes[-4] > 0 else None,
        "generated_at": session.now_et.astimezone(UTC).isoformat(),
        "finalized_at": finalized_at,
        "trading_eligible": False,
        "shadow_enabled": False,
        "paper_enabled": False,
        "live_enabled": False,
        "premarket_snapshot_available": bool(quote and current_price is not None),
        "quote_error": quote_error,
    }
