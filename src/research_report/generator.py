from __future__ import annotations

import html
import json
import logging
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import yaml

from src.ai_selector.selection_state import load_selection_state
from src.ai_selector.settings import DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE, resolve_price_band
from src.reports.trade_audit import latest_trade_activity_day, latest_trade_log_day, load_trade_records, summarize_trade_log

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REPORTS_DIR = PROJECT_DIR / "reports" / "research"
DEFAULT_SITE_DIR = PROJECT_DIR / "site" / "research"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs"
DEFAULT_BROKER_CACHE_DIR = PROJECT_DIR / "state" / "broker_cache"
DEFAULT_ORDER_STATE_DIR = PROJECT_DIR / "state" / "order_state"
DEFAULT_AI_REPORT_PATH = PROJECT_DIR / "reports" / "ai_selection_latest.json"
DEFAULT_SELECTION_STATE_PATH = PROJECT_DIR / "state" / "ai_selection_state.json"

FALLBACK_PRICE_BAND = (DEFAULT_MIN_PRICE, DEFAULT_MAX_PRICE)
DEFAULT_TICKER_SET = ["SOFI", "LABD", "F"]
IGNORE_TICKERS = {"TEST", "MOCK", "FAKE"}


@dataclass(frozen=True)
class ResearchPaths:
    report_json: Path
    report_md: Path
    report_html: Path


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _current_report_date(explicit: date | None, ai_report: dict[str, Any], selection_state: dict[str, Any] | None) -> date:
    if explicit is not None:
        return explicit
    for payload in (
        str(ai_report.get("selection_date") or "").strip(),
        str((selection_state or {}).get("et_date") or "").strip(),
    ):
        if not payload:
            continue
        try:
            return date.fromisoformat(payload[:10])
        except ValueError:
            continue
    return _et_now().date()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_ai_selection_report(project_dir: Path | None = None) -> dict[str, Any]:
    if project_dir is not None:
        return _load_json(Path(project_dir) / "reports" / "ai_selection_latest.json")
    return _load_json(DEFAULT_AI_REPORT_PATH)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_ticker(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw.split(".")[0]


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _fmt_money(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "N/A"
    return f"${number:,.2f}"


def _fmt_pct(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    number = _safe_float(value)
    if number is None:
        return "N/A"
    return f"{number:,.{digits}f}"


def _fmt_bool(value: Any) -> str:
    return "是" if bool(value) else "否"


def _coerce_selection_entry(item: dict[str, Any]) -> dict[str, Any]:
    entry = item.get("entry") if isinstance(item.get("entry"), dict) else {}
    payload = dict(entry or {})
    payload.setdefault("entry_proximity_score", _safe_float(item.get("entry_proximity_score"), 50.0))
    payload.setdefault("good_for_entry_now", bool(item.get("good_for_entry_now", False)))
    payload.setdefault("entry_quality", str(item.get("entry_quality") or "unknown"))
    payload.setdefault("entry_reason", str(item.get("entry_reason") or ""))
    payload.setdefault("range_position", item.get("range_position"))
    payload.setdefault("dist_to_support", item.get("dist_to_support"))
    payload.setdefault("dist_to_resistance", item.get("dist_to_resistance"))
    return payload


def _load_selection_state(project_dir: Path) -> dict[str, Any]:
    return _load_json(project_dir / "state" / "ai_selection_state.json")


def _load_top_configs(project_dir: Path) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for idx in range(1, 6):
        path = project_dir / "configs" / f"TOP{idx}.yaml"
        raw = _load_yaml(path)
        ticker = _normalize_ticker(raw.get("ticker"))
        if not ticker:
            continue
        selection = raw.get("selection") if isinstance(raw.get("selection"), dict) else {}
        allocation = raw.get("allocation") if isinstance(raw.get("allocation"), dict) else {}
        portfolio = raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {}
        top = {
            "rank": idx,
            "ticker": ticker,
            "mode": str(raw.get("mode") or "").strip().lower() or "unknown",
            "selection_date": str(selection.get("selection_date") or "").strip(),
            "selection": dict(selection or {}),
            "allocation": dict(allocation or {}),
            "portfolio": dict(portfolio or {}),
            "path": str(path),
        }
        top["entry"] = _coerce_selection_entry(selection)
        top["ai_score"] = _safe_float(selection.get("ai_score"), _safe_float(selection.get("score"), 0.0))
        top["range_score"] = _safe_float(selection.get("range_score"), 0.0)
        top["final_score"] = _safe_float(selection.get("final_score"), _safe_float(selection.get("score"), top["ai_score"]))
        top["confidence"] = _safe_float(selection.get("confidence"), 0.0)
        top["trade_filter_passed"] = bool(selection.get("trade_filter_passed", False))
        top["fallback_used"] = bool(selection.get("fallback_used", False))
        top["leveraged_etf"] = bool(selection.get("leveraged_etf", False))
        top["composition_filter_passed"] = bool(selection.get("composition_filter_passed", True))
        top["composition_reject_reason"] = str(selection.get("composition_reject_reason") or "")
        top["final_rank"] = _safe_int(selection.get("final_rank"), idx)
        top["protected_position"] = bool(selection.get("protected_position", False))
        top["reduce_only"] = bool(selection.get("reduce_only", False))
        configs.append(top)
    return configs


def _load_cached_snapshot(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    result = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
    if "fetched_at" in payload:
        result["fetched_at"] = payload.get("fetched_at")
    return result


def _load_cached_account_and_positions(project_dir: Path) -> dict[str, Any]:
    cache_dir = project_dir / "state" / "broker_cache"
    account = _load_cached_snapshot(cache_dir / "longbridge_account.json")
    positions_payload = _load_cached_snapshot(cache_dir / "longbridge_positions.json")
    positions = positions_payload.get("positions")
    if not isinstance(positions, list) or not positions:
        positions = account.get("positions") if isinstance(account.get("positions"), list) else []
    normalized_positions: list[dict[str, Any]] = []
    for raw in positions or []:
        if not isinstance(raw, dict):
            continue
        ticker = _normalize_ticker(raw.get("ticker"))
        if not ticker:
            continue
        normalized_positions.append(
            {
                "ticker": ticker,
                "quantity": _safe_int(raw.get("quantity"), 0),
                "avg_entry_price": _safe_float(raw.get("avg_entry_price"), None),
                "current_price": _safe_float(raw.get("current_price"), None),
                "market_value": _safe_float(raw.get("market_value"), None),
                "unrealized_pnl": _safe_float(raw.get("unrealized_pnl"), None),
                "unrealized_pnl_pct": _safe_float(raw.get("unrealized_pnl_pct"), None),
            }
        )
    return {
        "account": {
            "cash": _safe_float(account.get("cash"), None),
            "equity": _safe_float(account.get("equity"), None),
            "buying_power": _safe_float(account.get("buying_power"), None),
            "fetched_at": account.get("fetched_at"),
        },
        "positions": normalized_positions,
        "source_paths": {
            "account": str(cache_dir / "longbridge_account.json"),
            "positions": str(cache_dir / "longbridge_positions.json"),
        },
    }


def _parse_order_state_file(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    ticker = _normalize_ticker(raw.get("ticker") or path.stem)
    blocked = raw.get("blocked") if isinstance(raw.get("blocked"), dict) else {}
    failed_orders = raw.get("failed_orders_today") if isinstance(raw.get("failed_orders_today"), list) else []
    return {
        "ticker": ticker,
        "path": str(path),
        "updated_at": str(raw.get("updated_at") or ""),
        "blocked": {
            "blocked_until": str(blocked.get("blocked_until") or ""),
            "reason": str(blocked.get("reason") or ""),
            "buying_power_at_block": _safe_float(blocked.get("buying_power_at_block"), None),
        },
        "failed_orders_today": [
            {
                "ticker": _normalize_ticker(item.get("ticker") or ticker),
                "timestamp": str(item.get("timestamp") or ""),
                "reason": str(item.get("reason") or ""),
                "quantity": _safe_int(item.get("quantity"), 0),
                "price": _safe_float(item.get("price"), None),
                "buying_power": _safe_float(item.get("buying_power"), None),
                "runtime_scope": str(item.get("runtime_scope") or ""),
            }
            for item in failed_orders
            if isinstance(item, dict)
        ],
    }


def _load_order_state(project_dir: Path) -> list[dict[str, Any]]:
    order_dir = project_dir / "state" / "order_state"
    if not order_dir.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(order_dir.glob("*.json")):
        if not path.is_file():
            continue
        item = _parse_order_state_file(path)
        if item.get("ticker"):
            payloads.append(item)
    return payloads


def _is_test_record(record: dict[str, Any]) -> bool:
    scope = str(record.get("runtime_scope") or "").strip().lower()
    ticker = _normalize_ticker(record.get("ticker"))
    return scope == "test" or ticker in IGNORE_TICKERS


def _trade_summary(project_dir: Path, report_day: date) -> dict[str, Any]:
    log_dir = project_dir / "logs"
    requested_day = report_day.strftime("%Y%m%d")
    path = log_dir / f"trades-{requested_day}.jsonl"
    day = requested_day if path.exists() else latest_trade_activity_day(log_dir=log_dir, mode="paper")
    if not day:
        day = latest_trade_log_day(log_dir=log_dir) or requested_day
    summary = summarize_trade_log(log_dir=log_dir, day=day, mode="paper")
    records = summary.get("latest_record")
    return {
        "requested_trade_day": requested_day,
        "trade_log_day_used": day,
        "summary": summary,
        "path": str(log_dir / f"trades-{day}.jsonl"),
    }


def _compute_focus_symbols(top_configs: list[dict[str, Any]], positions: list[dict[str, Any]], ai_report: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()
    for item in top_configs:
        ticker = _normalize_ticker(item.get("ticker"))
        if ticker:
            symbols.add(ticker)
    for item in positions:
        ticker = _normalize_ticker(item.get("ticker"))
        if ticker:
            symbols.add(ticker)
    for item in ai_report.get("protected_positions") or []:
        if isinstance(item, dict):
            ticker = _normalize_ticker(item.get("ticker"))
            if ticker:
                symbols.add(ticker)
    return sorted(symbols)


def _build_market_snapshot(
    ticker: str,
    *,
    fetcher_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    ticker = _normalize_ticker(ticker)
    if not ticker:
        return {}
    try:
        fetcher = fetcher_factory(ticker) if fetcher_factory else None
        if fetcher is None:
            from src.data.fetcher import PriceFetcher

            fetcher = PriceFetcher(ticker, poll_interval=0)
        quote = fetcher.get_quote()
        candles = fetcher.get_ohlcv(period="1mo", interval="1d")
    except Exception as exc:
        logger.debug("market snapshot failed for %s: %s", ticker, exc)
        return {
            "ticker": ticker,
            "available": False,
            "error": str(exc),
        }

    closes = [float(getattr(item, "close", 0.0) or 0.0) for item in candles or [] if float(getattr(item, "close", 0.0) or 0.0) > 0]
    highs = [float(getattr(item, "high", 0.0) or 0.0) for item in candles or [] if float(getattr(item, "high", 0.0) or 0.0) > 0]
    lows = [float(getattr(item, "low", 0.0) or 0.0) for item in candles or [] if float(getattr(item, "low", 0.0) or 0.0) > 0]
    volumes = [float(getattr(item, "volume", 0.0) or 0.0) for item in candles or [] if float(getattr(item, "volume", 0.0) or 0.0) > 0]
    latest_close = closes[-1] if closes else None
    prev_close = closes[-2] if len(closes) >= 2 else None
    current_price = _safe_float(getattr(quote, "price", None), latest_close)
    if current_price is None:
        current_price = latest_close
    change_1d_pct = None
    if current_price is not None and prev_close and prev_close > 0:
        change_1d_pct = ((current_price - prev_close) / prev_close) * 100.0
    change_5d_pct = None
    if len(closes) >= 6 and closes[-6] > 0:
        change_5d_pct = ((closes[-1] - closes[-6]) / closes[-6]) * 100.0
    price_range_low = min(lows) if lows else None
    price_range_high = max(highs) if highs else None
    range_position = None
    dist_to_support = None
    dist_to_resistance = None
    if current_price and price_range_low and price_range_high and price_range_high > price_range_low:
        range_position = round(((current_price - price_range_low) / (price_range_high - price_range_low)) * 100.0, 2)
        dist_to_support = round(((current_price - price_range_low) / current_price) * 100.0, 2)
        dist_to_resistance = round(((price_range_high - current_price) / current_price) * 100.0, 2)
    atr_pct = None
    if candles and current_price:
        true_ranges: list[float] = []
        prev_close_val = None
        for candle in candles[-14:]:
            high = float(getattr(candle, "high", 0.0) or 0.0)
            low = float(getattr(candle, "low", 0.0) or 0.0)
            close = float(getattr(candle, "close", 0.0) or 0.0)
            if high <= 0 or low <= 0:
                continue
            if prev_close_val and prev_close_val > 0:
                true_range = max(high - low, abs(high - prev_close_val), abs(low - prev_close_val))
            else:
                true_range = high - low
            true_ranges.append(true_range)
            prev_close_val = close if close > 0 else prev_close_val
        if true_ranges:
            atr = sum(true_ranges) / len(true_ranges)
            atr_pct = round((atr / current_price) * 100.0, 2) if current_price > 0 else None
    volatility_pct = None
    if len(closes) >= 3:
        returns: list[float] = []
        for idx in range(1, len(closes)):
            prev = closes[idx - 1]
            curr = closes[idx]
            if prev > 0 and curr > 0:
                returns.append((curr - prev) / prev)
        if len(returns) >= 2:
            volatility_pct = round(statistics.pstdev(returns) * 100.0, 2)
    avg_volume_10d = None
    if volumes:
        avg_volume_10d = round(sum(volumes[-10:]) / len(volumes[-10:]), 2)
    return {
        "ticker": ticker,
        "available": True,
        "current_price": current_price,
        "previous_close": prev_close,
        "latest_close": latest_close,
        "change_1d_pct": change_1d_pct,
        "change_5d_pct": change_5d_pct,
        "price_range_low": price_range_low,
        "price_range_high": price_range_high,
        "range_position": range_position,
        "dist_to_support": dist_to_support,
        "dist_to_resistance": dist_to_resistance,
        "atr_pct": atr_pct,
        "volatility_pct": volatility_pct,
        "avg_volume_10d": avg_volume_10d,
        "volume_latest": volumes[-1] if volumes else None,
        "close_history": [round(value, 4) for value in closes[-20:]],
    }


def _merge_top_card(top: dict[str, Any], market: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    ticker = _normalize_ticker(top.get("ticker"))
    position = next((item for item in positions if _normalize_ticker(item.get("ticker")) == ticker), {})
    entry = dict(top.get("entry") or {})
    card = {
        "rank": top.get("rank"),
        "final_rank": top.get("final_rank"),
        "ticker": ticker,
        "mode": top.get("mode"),
        "selection_date": top.get("selection_date"),
        "score": top.get("final_score"),
        "ai_score": top.get("ai_score"),
        "range_score": top.get("range_score"),
        "final_score": top.get("final_score"),
        "confidence": top.get("confidence"),
        "trade_filter_passed": bool(top.get("trade_filter_passed", False)),
        "fallback_used": bool(top.get("fallback_used", False)),
        "candidate_fallback": bool(top.get("candidate_fallback", False)),
        "fallback_sources": list(top.get("fallback_sources") or []),
        "mock_used": bool(top.get("mock_used", False)),
        "mock_sources": list(top.get("mock_sources") or []),
        "degraded": bool(top.get("degraded", False)),
        "degradation_reasons": list(top.get("degradation_reasons") or []),
        "leveraged_etf": bool(top.get("leveraged_etf", False)),
        "composition_filter_passed": bool(top.get("composition_filter_passed", True)),
        "composition_reject_reason": str(top.get("composition_reject_reason") or ""),
        "protected_position": bool(top.get("protected_position", False)),
        "reduce_only": bool(top.get("reduce_only", False)),
        "current_validation_status": str(top.get("current_validation_status") or top.get("validation_status") or ""),
        "trade_admission_status": str(top.get("trade_admission_status") or ""),
        "data_mode": str(top.get("data_mode") or ""),
        "data_freshness": str(top.get("data_freshness") or ""),
        "data_status": str(top.get("data_status") or ""),
        "scoring_eligible": bool(top.get("scoring_eligible", False)),
        "scoring_block_reason": str(top.get("scoring_block_reason") or ""),
        "entry": entry,
        "allocation": dict(top.get("allocation") or {}),
        "portfolio": dict(top.get("portfolio") or {}),
        "market": market,
        "position": position or None,
        "entry_ready": bool(entry.get("good_for_entry_now", False)),
        "entry_quality": str(entry.get("entry_quality") or "unknown"),
        "entry_reason": str(entry.get("entry_reason") or ""),
    }
    if position:
        card["position"] = {
            "ticker": position.get("ticker"),
            "quantity": position.get("quantity"),
            "avg_entry_price": position.get("avg_entry_price"),
            "current_price": position.get("current_price"),
            "market_value": position.get("market_value"),
            "unrealized_pnl": position.get("unrealized_pnl"),
            "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
        }
    return card


def _selection_sync(project_dir: Path, report_day: date, top_configs: list[dict[str, Any]], ai_report: dict[str, Any], selection_state: dict[str, Any]) -> dict[str, Any]:
    required_et_date = report_day.isoformat()
    state = dict(selection_state or {})
    selection_state_symbols = [
        _normalize_ticker(item)
        for item in (state.get("selected_symbols") or [])
        if _normalize_ticker(item)
    ]
    top_config_symbols = [_normalize_ticker(item.get("ticker")) for item in top_configs if _normalize_ticker(item.get("ticker"))]
    current_top_config_symbols_list = [
        _normalize_ticker(item)
        for item in (state.get("current_top_config_symbols") or state.get("top_config_symbols") or top_config_symbols)
        if _normalize_ticker(item)
    ]
    report_top_symbols = [_normalize_ticker(item.get("ticker")) for item in top_configs if _normalize_ticker(item.get("ticker"))]
    synced = bool(state) and selection_state_symbols == top_config_symbols and str(state.get("et_date") or "") == required_et_date
    mismatch_reason = None
    if not synced:
        if not state:
            mismatch_reason = "selection_state_missing"
        elif str(state.get("et_date") or "") != required_et_date:
            mismatch_reason = "selection_state_date_mismatch"
        else:
            mismatch_reason = "top_config_symbols_do_not_match_selection_state"
    return {
        "ok": bool(synced),
        "reason": "ok" if synced else mismatch_reason or "unknown",
        "mismatch_reason": mismatch_reason,
        "required_et_date": required_et_date,
        "selection_state_et_date": str(state.get("et_date") or ""),
        "selection_state_symbols": selection_state_symbols,
        "current_top_config_symbols": current_top_config_symbols_list,
        "report_top_symbols": report_top_symbols,
        "top_config_symbols": top_config_symbols,
        "top_config_count": len(top_configs),
        "ai_selection_date": str(ai_report.get("selection_date") or ""),
    }


def _quality_summary(top_cards: list[dict[str, Any]], ai_report: dict[str, Any]) -> dict[str, Any]:
    entry_ready = [card for card in top_cards if card.get("entry_ready")]
    observation_only = [card for card in top_cards if not card.get("entry_ready")]
    quality_counts = Counter()
    top_quality_rows = []
    for card in top_cards:
        entry = card.get("entry") if isinstance(card.get("entry"), dict) else {}
        quality = str(card.get("entry_quality") or entry.get("entry_quality") or "unknown").strip().lower() or "unknown"
        quality_counts[quality] += 1
        top_quality_rows.append(
            {
                "rank": int(card.get("rank") or card.get("final_rank") or 0) or None,
                "ticker": str(card.get("ticker") or ""),
                "entry_quality": quality,
                "good_for_entry_now": bool(card.get("entry_ready")),
                "entry_reason": str(card.get("entry_reason") or entry.get("entry_reason") or ""),
                "final_score": _safe_float(card.get("final_score"), None),
            }
        )
    return {
        "entry_ready_symbols": [card["ticker"] for card in entry_ready],
        "observation_only_symbols": [card["ticker"] for card in observation_only],
        "entry_ready_count": len(entry_ready),
        "observation_only_count": len(observation_only),
        "top_quality_rows": top_quality_rows,
        "top_quality_counts": dict(quality_counts),
        "fallback_used": bool(ai_report.get("fallback_used", False)),
        "provider_fallback_used": bool(ai_report.get("provider_fallback_used", False)),
        "execution_status": str(ai_report.get("execution_status") or "").strip().upper(),
        "result_quality": str(ai_report.get("result_quality") or "").strip().upper(),
        "research_admission": str(ai_report.get("research_admission") or "").strip().upper(),
        "selected_top_n": int(ai_report.get("selected_top_n") or 0),
        "requested_top_n": int(ai_report.get("requested_top_n") or 0),
        "top_n_complete": bool(ai_report.get("top_n_complete", False)),
        "top_n_missing_count": int(ai_report.get("top_n_missing_count") or 0),
        "provider_audit": ai_report.get("provider_audit") or {},
        "provider_outputs": ai_report.get("provider_outputs") or {},
        "warnings_structured": list(ai_report.get("warnings_structured") or []),
        "warnings": list(ai_report.get("warnings") or []),
        "price_band": {
            "min": _safe_float((ai_report.get("settings") or {}).get("min_price"), FALLBACK_PRICE_BAND[0]),
            "max": _safe_float((ai_report.get("settings") or {}).get("max_price"), FALLBACK_PRICE_BAND[1]),
        },
    }


def _no_trade_reason(top_cards: list[dict[str, Any]], trade_summary: dict[str, Any]) -> str:
    if any(card.get("entry_ready") for card in top_cards):
        return "部分标的具备开仓条件，但最终是否成交取决于实时风控和买点触发。"
    return "当前 TOP 更偏观察级，价格位置不够理想，因此没有形成可开仓级别的买点。"


def _trade_decision_summary(project_dir: Path, report_day: date, trade_summary: dict[str, Any]) -> dict[str, Any]:
    requested_day = report_day.strftime("%Y%m%d")
    path = project_dir / "logs" / f"trades-{requested_day}.jsonl"
    day = requested_day if path.exists() else latest_trade_activity_day(log_dir=project_dir / "logs", mode="paper")
    if not day:
        day = latest_trade_log_day(log_dir=project_dir / "logs") or requested_day
    records = load_trade_records(project_dir / "logs", day)
    decision_records = [
        record for record in records
        if "decision" in str(record.get("phase") or "").lower() or str(record.get("phase") or "").lower() == "risk_decision"
    ]

    signal_counts: Counter[str] = Counter()
    no_trade_reason_counts: Counter[str] = Counter()
    risk_block_reason_counts: Counter[str] = Counter()
    buy_signal_count = 0
    buy_allowed_count = 0
    buy_blocked_count = 0

    for record in decision_records:
        signal = str(
            _first_non_empty(
                record.get("signal"),
                (record.get("trade_signal") or {}).get("action") if isinstance(record.get("trade_signal"), dict) else None,
                (record.get("order") or {}).get("side") if isinstance(record.get("order"), dict) else None,
            )
            or ""
        ).strip().upper()
        if signal:
            signal_counts[signal] += 1
        if signal != "BUY":
            if signal:
                no_trade_reason_counts[f"signal_{signal.lower()}"] += 1
            continue

        buy_signal_count += 1
        risk_approved = record.get("risk_approved")
        final_action = str(record.get("final_action") or "").strip().lower()
        if risk_approved is True or final_action in {"buy_allowed", "submitted", "filled", "allowed"}:
            buy_allowed_count += 1
            continue

        buy_blocked_count += 1
        reason = str(
            _first_non_empty(
                record.get("reason"),
                record.get("blocked_by"),
                record.get("ai_reason"),
                record.get("blocked_reason"),
            )
            or "blocked"
        ).strip()
        no_trade_reason_counts[reason] += 1
        risk_block_reason_counts[reason] += 1

    execution_summary = trade_summary.get("summary") or {}
    return {
        "requested_trade_day": requested_day,
        "trade_log_day_used": day,
        "decision_record_count": len(decision_records),
        "buy_signal_count": buy_signal_count,
        "buy_allowed_count": buy_allowed_count,
        "buy_blocked_count": buy_blocked_count,
        "signal_counts": dict(signal_counts),
        "no_trade_reason_counts": dict(no_trade_reason_counts),
        "risk_block_reason_counts": dict(risk_block_reason_counts),
        "trade_activity_execution_count": int(execution_summary.get("execution_count", 0) or 0),
        "trade_activity_buy_count": int(execution_summary.get("buy_count", 0) or 0),
        "trade_activity_sell_count": int(execution_summary.get("sell_count", 0) or 0),
    }


def _trade_event_flags(records: list[dict[str, Any]], ticker: str) -> dict[str, Any]:
    normalized = _normalize_ticker(ticker)
    buy_count = 0
    sell_count = 0
    buy_reasons: list[str] = []
    sell_reasons: list[str] = []

    for record in records or []:
        record_ticker = _normalize_ticker(record.get("ticker") or (record.get("request") or {}).get("ticker"))
        if record_ticker != normalized:
            continue
        signal = str(
            _first_non_empty(
                record.get("signal"),
                (record.get("trade_signal") or {}).get("action") if isinstance(record.get("trade_signal"), dict) else None,
                (record.get("order") or {}).get("side") if isinstance(record.get("order"), dict) else None,
            )
            or ""
        ).strip().upper()
        reason = str(
            _first_non_empty(
                record.get("reason"),
                record.get("blocked_by"),
                record.get("ai_reason"),
                record.get("blocked_reason"),
                (record.get("response") or {}).get("status") if isinstance(record.get("response"), dict) else None,
            )
            or ""
        ).strip()

        if signal == "BUY":
            buy_count += 1
            if reason:
                buy_reasons.append(reason)
        elif signal == "SELL":
            sell_count += 1
            if reason:
                sell_reasons.append(reason)

        phase = str(record.get("phase") or "").lower()
        if phase == "execution" and isinstance(record.get("order"), dict):
            side = str(record["order"].get("side") or "").strip().upper()
            if side == "BUY":
                buy_count += 1
                if reason:
                    buy_reasons.append(reason)
            elif side == "SELL":
                sell_count += 1
                if reason:
                    sell_reasons.append(reason)

    return {
        "buy_triggered": buy_count > 0,
        "sell_triggered": sell_count > 0,
        "buy_trigger_count": buy_count,
        "sell_trigger_count": sell_count,
        "buy_reasons": buy_reasons,
        "sell_reasons": sell_reasons,
    }


def _strategy_review_summary(project_dir: Path, report_day: date, top_cards: list[dict[str, Any]], trade_activity: dict[str, Any]) -> dict[str, Any]:
    requested_day = report_day.strftime("%Y%m%d")
    trade_log_day = trade_activity.get("trade_log_day_used") or requested_day
    records = load_trade_records(project_dir / "logs", trade_log_day)
    review_rows: list[dict[str, Any]] = []
    counts = Counter()

    for idx, card in enumerate(top_cards or [], start=1):
        market = card.get("market") if isinstance(card.get("market"), dict) else {}
        entry = card.get("entry") if isinstance(card.get("entry"), dict) else {}
        ticker = _normalize_ticker(card.get("ticker"))
        entry_price = _safe_float(
            _first_non_empty(
                market.get("current_price"),
                card.get("selection_price"),
                card.get("entry_price"),
                market.get("latest_close"),
            ),
            None,
        )
        day_high = _safe_float(_first_non_empty(market.get("price_range_high"), market.get("day_high")), None)
        day_low = _safe_float(_first_non_empty(market.get("price_range_low"), market.get("day_low")), None)
        day_close = _safe_float(_first_non_empty(market.get("latest_close"), market.get("current_price")), entry_price)

        max_upside_pct = None
        if entry_price and entry_price > 0 and day_high is not None:
            max_upside_pct = round(((day_high - entry_price) / entry_price) * 100.0, 2)
        max_drawdown_pct = None
        if entry_price and entry_price > 0 and day_low is not None:
            max_drawdown_pct = round(((day_low - entry_price) / entry_price) * 100.0, 2)
        close_change_pct = None
        if entry_price and entry_price > 0 and day_close is not None:
            close_change_pct = round(((day_close - entry_price) / entry_price) * 100.0, 2)

        trade_flags = _trade_event_flags(records, ticker)
        entry_ready = bool(card.get("entry_ready"))
        if trade_flags["buy_triggered"] or trade_flags["sell_triggered"]:
            if (day_close is not None and entry_price and day_close >= entry_price) or (max_upside_pct is not None and max_upside_pct > 0):
                review_result = "选股成功"
            else:
                review_result = "失败"
        else:
            review_result = "观察正确" if not entry_ready else "失败"

        counts[review_result] += 1
        review_rows.append(
            {
                "rank": idx,
                "ticker": ticker,
                "entry_price": entry_price,
                "day_high": day_high,
                "day_low": day_low,
                "close_price": day_close,
                "max_upside_pct": max_upside_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "buy_triggered": trade_flags["buy_triggered"],
                "sell_triggered": trade_flags["sell_triggered"],
                "buy_trigger_count": trade_flags["buy_trigger_count"],
                "sell_trigger_count": trade_flags["sell_trigger_count"],
                "review_result": review_result,
                "review_reason": (
                    "已触发买卖且收盘不弱于入选价"
                    if review_result == "选股成功"
                    else "未触发交易且属于观察级标的"
                    if review_result == "观察正确"
                    else "买卖触发后走势未兑现预期"
                ),
                "entry_quality": str(card.get("entry_quality") or entry.get("entry_quality") or "unknown"),
                "good_for_entry_now": bool(card.get("entry_ready")),
            }
        )

    return {
        "trade_log_day_used": trade_log_day,
        "rows": review_rows,
        "counts": dict(counts),
        "success_count": int(counts.get("选股成功", 0) or 0),
        "observation_correct_count": int(counts.get("观察正确", 0) or 0),
        "failure_count": int(counts.get("失败", 0) or 0),
        "buy_triggered_count": sum(1 for row in review_rows if row.get("buy_triggered")),
        "sell_triggered_count": sum(1 for row in review_rows if row.get("sell_triggered")),
    }


def _format_summary_note(report: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    sync = report.get("selection_sync") or {}
    quality = report.get("quality") or {}
    decision = report.get("decision_summary") or {}
    trade = report.get("trade_activity") or {}
    account = report.get("account") or {}
    positions = report.get("positions") or []
    entry_ready = quality.get("entry_ready_symbols") or []
    observation = quality.get("observation_only_symbols") or []

    if sync.get("ok"):
        notes.append("选股配置已与 selection_state 同步。")
    else:
        notes.append(
            f"选股配置存在不一致：{sync.get('mismatch_reason') or sync.get('reason') or 'unknown'}。"
        )
    if entry_ready:
        notes.append(f"可开仓级标的：{', '.join(entry_ready)}。")
    if observation:
        notes.append(f"观察级标的：{', '.join(observation)}。")
    if decision.get("buy_blocked_count", 0):
        notes.append(
            f"买入被阻断 {int(decision.get('buy_blocked_count', 0) or 0)} 次，主要原因："
            f"{', '.join(f'{k}×{v}' for k, v in list((decision.get('risk_block_reason_counts') or {}).items())[:3]) or '暂无'}。"
        )
    if int(trade.get("execution_count", 0) or 0) == 0:
        notes.append("当日未见已完成成交。")
    else:
        notes.append(
            f"当日成交 {int(trade.get('execution_count', 0) or 0)} 笔，买 {int(trade.get('buy_count', 0) or 0)} / 卖 {int(trade.get('sell_count', 0) or 0)}。"
        )
    if account.get("equity") is not None:
        notes.append(f"账户权益快照：{_fmt_money(account.get('equity'))}。")
    if positions:
        notes.append(f"当前持仓数量：{len(positions)}。")
    return notes


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    sep = ["---" for _ in headers]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# 每日研究报告 {report.get('date', '')}")
    lines.append("")
    lines.append(f"- 生成时间：`{report.get('generated_at', '')}`")
    lines.append(f"- 模式：`{report.get('mode', 'paper')}`")
    lines.append(f"- 选股同步：`{'通过' if report.get('selection_sync', {}).get('ok') else '不一致'}`")
    lines.append(f"- 价格范围：`{_fmt_money((report.get('quality') or {}).get('price_band', {}).get('min'))} - {_fmt_money((report.get('quality') or {}).get('price_band', {}).get('max'))}`")
    lines.append(f"- fallback_used：`{_fmt_bool((report.get('quality') or {}).get('fallback_used'))}`")
    lines.append(f"- 执行状态：`{report.get('selection_execution_status') or 'COMPLETED'}`")
    lines.append(f"- 结果质量：`{report.get('selection_result_quality') or 'COMPLETE'}`")
    lines.append(f"- 研究准入：`{report.get('selection_research_admission') or 'RESEARCH_READY'}`")
    lines.append("")
    lines.append("## 结论摘要")
    for note in report.get("summary_notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    lines.append("## TOP 研究视图")
    headers = ["Rank", "Ticker", "Entry", "Final", "AI", "Range", "Price", "RangePos", "观察/开仓", "成交方向", "状态", "Fallback/Mock"]
    rows = []
    for card in report.get("top_cards") or []:
        market = card.get("market") or {}
        entry = card.get("entry") or {}
        rows.append([
            str(card.get("rank") or card.get("final_rank") or ""),
            card.get("ticker", ""),
            str(card.get("entry_quality") or "unknown"),
            _fmt_num(card.get("final_score")),
            _fmt_num(card.get("ai_score")),
            _fmt_num(card.get("range_score")),
            _fmt_money(market.get("current_price")),
            _fmt_num(entry.get("range_position")),
            "可开仓" if card.get("entry_ready") else "观察",
            "杠杆/反向ETF" if card.get("leveraged_etf") else "普通标的",
            f"{card.get('trade_admission_status') or 'NOT_TRADABLE'}",
            f"{'fallback' if card.get('candidate_fallback') else 'direct'} / {'mock' if card.get('mock_used') else 'real'}",
        ])
    lines.append(_render_markdown_table(headers, rows) if rows else "- 暂无 TOP 数据")
    lines.append("")
    lines.append("## TOP 质量总结")
    quality = report.get("quality") or {}
    quality_rows = []
    for row in quality.get("top_quality_rows") or []:
        quality_rows.append([
            str(row.get("rank") or ""),
            row.get("ticker", ""),
            str(row.get("entry_quality") or "unknown"),
            _fmt_bool(row.get("good_for_entry_now")),
            _fmt_num(row.get("final_score")),
            str(row.get("entry_reason") or ""),
        ])
    lines.append(
        _render_markdown_table(["Rank", "Ticker", "Entry Quality", "Good For Entry", "Final", "Reason"], quality_rows)
        if quality_rows
        else "- 暂无质量总结"
    )
    lines.append("")
    lines.append("## 策略评分复盘")
    strategy = report.get("strategy_review") or {}
    strategy_rows = []
    for row in strategy.get("rows") or []:
        strategy_rows.append([
            str(row.get("rank") or ""),
            row.get("ticker", ""),
            _fmt_money(row.get("entry_price")),
            _fmt_money(row.get("day_high")),
            _fmt_money(row.get("day_low")),
            _fmt_money(row.get("close_price")),
            _fmt_pct(row.get("max_upside_pct")),
            _fmt_pct(row.get("max_drawdown_pct")),
            _fmt_bool(row.get("buy_triggered")),
            _fmt_bool(row.get("sell_triggered")),
            str(row.get("review_result") or ""),
        ])
    lines.append(
        _render_markdown_table(
            ["Rank", "Ticker", "Entry", "High", "Low", "Close", "Max Up", "Max DD", "Buy", "Sell", "Review"],
            strategy_rows,
        )
        if strategy_rows
        else "- 暂无策略评分复盘数据"
    )
    lines.append("")
    lines.append("## 复盘评分统计")
    if strategy:
        lines.append(f"- 选股成功：`{int(strategy.get('success_count', 0) or 0)}`")
        lines.append(f"- 观察正确：`{int(strategy.get('observation_correct_count', 0) or 0)}`")
        lines.append(f"- 失败：`{int(strategy.get('failure_count', 0) or 0)}`")
    else:
        lines.append("- 暂无复盘评分统计。")
    lines.append("")
    lines.append("## 无交易 / 风控拦截统计")
    decision = report.get("decision_summary") or {}
    if decision:
        lines.append(f"- BUY 信号：`{int(decision.get('buy_signal_count', 0) or 0)}`")
        lines.append(f"- BUY 允许：`{int(decision.get('buy_allowed_count', 0) or 0)}`")
        lines.append(f"- BUY 阻断：`{int(decision.get('buy_blocked_count', 0) or 0)}`")
        reason_rows = []
        for reason, count in (decision.get("no_trade_reason_counts") or {}).items():
            reason_rows.append([reason, str(count)])
        lines.append(
            _render_markdown_table(["Reason", "Count"], reason_rows)
            if reason_rows
            else "- 暂无未成交原因统计"
        )
        lines.append("")
        risk_rows = []
        for reason, count in (decision.get("risk_block_reason_counts") or {}).items():
            risk_rows.append([reason, str(count)])
        lines.append(
            _render_markdown_table(["Risk Block Reason", "Count"], risk_rows)
            if risk_rows
            else "- 暂无风控拦截统计"
        )
    else:
        lines.append("- 暂无交易统计。")
    lines.append("")
    lines.append("## 当前持仓")
    if report.get("positions"):
        headers = ["Ticker", "Qty", "Avg", "Price", "MV", "UPnL", "UPnL%"]
        rows = []
        for row in report["positions"]:
            rows.append([
                row.get("ticker", ""),
                str(row.get("quantity", "")),
                _fmt_money(row.get("avg_entry_price")),
                _fmt_money(row.get("current_price")),
                _fmt_money(row.get("market_value")),
                _fmt_money(row.get("unrealized_pnl")),
                _fmt_pct(row.get("unrealized_pnl_pct")),
            ])
        lines.append(_render_markdown_table(headers, rows))
    else:
        lines.append("- 暂无持仓快照")
    lines.append("")
    lines.append("## 交易活动")
    trade = report.get("trade_activity") or {}
    summary = trade.get("summary") or {}
    lines.extend([
        f"- 执行模式：`{summary.get('execution_mode', 'paper')}`",
        f"- 成交数：`{summary.get('execution_count', 0)}`",
        f"- 买单：`{summary.get('buy_count', 0)}`",
        f"- 卖单：`{summary.get('sell_count', 0)}`",
        f"- 未决订单：`{summary.get('broker_unresolved_count', 0)}`",
        f"- 最新成交线：`{summary.get('latest_line', '暂无')}`",
    ])
    lines.append("")
    lines.append("## 订单状态")
    order_state = report.get("order_state") or {}
    if order_state.get("active"):
        headers = ["Ticker", "Blocked", "Reason", "Failed", "Last Update"]
        rows = []
        for row in order_state["active"]:
            rows.append([
                row.get("ticker", ""),
                _fmt_bool(row.get("blocked")),
                row.get("blocked_reason", ""),
                str(row.get("failed_orders", 0)),
                row.get("updated_at", ""),
            ])
        lines.append(_render_markdown_table(headers, rows))
    else:
        lines.append("- 当前没有活跃阻断。")
    if order_state.get("historical"):
        lines.append("")
        lines.append("### 历史 / 非当前标的失败记录")
        headers = ["Ticker", "Reason", "Count", "Last Timestamp"]
        rows = []
        for row in order_state["historical"]:
            rows.append([
                row.get("ticker", ""),
                row.get("reason", ""),
                str(row.get("count", 0)),
                row.get("last_timestamp", ""),
            ])
        lines.append(_render_markdown_table(headers, rows))
    lines.append("")
    lines.append("## 原始 JSON")
    lines.append("```json")
    lines.append(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    lines.append("```")
    return "\n".join(lines) + "\n"


def _render_html(report: dict[str, Any]) -> str:
    top_cards_html = []
    for card in report.get("top_cards") or []:
        market = card.get("market") or {}
        entry = card.get("entry") or {}
        position = card.get("position") or {}
        labels = []
        if card.get("entry_ready"):
            labels.append('<span class="pill good">可开仓</span>')
        else:
            labels.append('<span class="pill warn">观察级</span>')
        if card.get("fallback_used"):
            labels.append('<span class="pill warn">fallback</span>')
        if card.get("leveraged_etf"):
            labels.append('<span class="pill muted">杠杆/反向ETF</span>')
        if card.get("reduce_only"):
            labels.append('<span class="pill muted">reduce_only</span>')
        entry_reason = html.escape(str(card.get("entry_reason") or ""))
        top_cards_html.append(
            f"""
            <article class="card">
                <div class="card-head">
                    <div>
                        <div class="card-title">TOP{html.escape(str(card.get('rank') or card.get('final_rank') or ''))} · {html.escape(card.get('ticker', ''))}</div>
                        <div class="card-subtitle">Final {html.escape(_fmt_num(card.get('final_score')))} · AI {html.escape(_fmt_num(card.get('ai_score')))} · Range {html.escape(_fmt_num(card.get('range_score')))}</div>
                    </div>
                    <div class="labels">{''.join(labels)}</div>
                </div>
                <div class="grid">
                    <div><span>Entry Quality</span><strong>{html.escape(str(card.get('entry_quality') or 'unknown'))}</strong></div>
                    <div><span>Good For Entry</span><strong>{_fmt_bool(card.get('entry_ready'))}</strong></div>
                    <div><span>Current Price</span><strong>{html.escape(_fmt_money(market.get('current_price')))}</strong></div>
                    <div><span>Range Position</span><strong>{html.escape(_fmt_num(entry.get('range_position')))}%</strong></div>
                    <div><span>Dist To Support</span><strong>{html.escape(_fmt_num(entry.get('dist_to_support')))}%</strong></div>
                    <div><span>Dist To Resistance</span><strong>{html.escape(_fmt_num(entry.get('dist_to_resistance')))}%</strong></div>
                    <div><span>Allocation Capital</span><strong>{html.escape(_fmt_money((card.get('allocation') or {}).get('target_capital')))}</strong></div>
                    <div><span>Allocation Shares</span><strong>{html.escape(str((card.get('allocation') or {}).get('target_shares', '0')))}</strong></div>
                </div>
                <p class="reason">{entry_reason or '暂无原因说明'}</p>
            """
        )
        if position:
            top_cards_html.append(
                f"""
                <div class="position-box">
                    当前持仓：{html.escape(str(position.get('quantity', 0)))} 股 · 成本 {html.escape(_fmt_money(position.get('avg_entry_price')))} · 现价 {html.escape(_fmt_money(position.get('current_price')))} · 浮盈亏 {html.escape(_fmt_money(position.get('unrealized_pnl')))}
                </div>
                """
            )
        top_cards_html.append("</article>")

    quality_rows = []
    for row in (report.get("quality") or {}).get("top_quality_rows") or []:
        quality_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('rank') or ''))}</td>"
            f"<td>{html.escape(str(row.get('ticker') or ''))}</td>"
            f"<td>{html.escape(str(row.get('entry_quality') or 'unknown'))}</td>"
            f"<td>{html.escape(_fmt_bool(row.get('good_for_entry_now')))}</td>"
            f"<td>{html.escape(_fmt_num(row.get('final_score')))}</td>"
            f"<td>{html.escape(str(row.get('entry_reason') or ''))}</td>"
            "</tr>"
        )

    strategy_rows = []
    strategy = report.get("strategy_review") or {}
    for row in strategy.get("rows") or []:
        strategy_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('rank') or ''))}</td>"
            f"<td>{html.escape(str(row.get('ticker') or ''))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('entry_price')))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('day_high')))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('day_low')))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('close_price')))}</td>"
            f"<td>{html.escape(_fmt_pct(row.get('max_upside_pct')))}</td>"
            f"<td>{html.escape(_fmt_pct(row.get('max_drawdown_pct')))}</td>"
            f"<td>{html.escape(_fmt_bool(row.get('buy_triggered')))}</td>"
            f"<td>{html.escape(_fmt_bool(row.get('sell_triggered')))}</td>"
            f"<td>{html.escape(str(row.get('review_result') or ''))}</td>"
            "</tr>"
        )

    no_trade_rows = []
    decision = report.get("decision_summary") or {}
    for reason, count in (decision.get("no_trade_reason_counts") or {}).items():
        no_trade_rows.append(
            "<tr>"
            f"<td>{html.escape(str(reason))}</td>"
            f"<td>{html.escape(str(count))}</td>"
            "</tr>"
        )

    risk_rows = []
    for reason, count in (decision.get("risk_block_reason_counts") or {}).items():
        risk_rows.append(
            "<tr>"
            f"<td>{html.escape(str(reason))}</td>"
            f"<td>{html.escape(str(count))}</td>"
            "</tr>"
        )

    position_rows = []
    for row in report.get("positions") or []:
        position_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('ticker', '')))}</td>"
            f"<td>{html.escape(str(row.get('quantity', '')))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('avg_entry_price')))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('current_price')))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('market_value')))}</td>"
            f"<td>{html.escape(_fmt_money(row.get('unrealized_pnl')))}</td>"
            f"<td>{html.escape(_fmt_pct(row.get('unrealized_pnl_pct')))}</td>"
            "</tr>"
        )
    active_order_rows = []
    for row in (report.get("order_state") or {}).get("active") or []:
        active_order_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('ticker', '')))}</td>"
            f"<td>{html.escape(_fmt_bool(row.get('blocked')))}</td>"
            f"<td>{html.escape(str(row.get('blocked_reason', '')))}</td>"
            f"<td>{html.escape(str(row.get('failed_orders', 0)))}</td>"
            f"<td>{html.escape(str(row.get('last_timestamp', '')))}</td>"
            "</tr>"
        )
    historical_order_rows = []
    for row in (report.get("order_state") or {}).get("historical") or []:
        historical_order_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('ticker', '')))}</td>"
            f"<td>{html.escape(str(row.get('reason', '')))}</td>"
            f"<td>{html.escape(str(row.get('count', 0)))}</td>"
            f"<td>{html.escape(str(row.get('last_timestamp', '')))}</td>"
            "</tr>"
        )
    trade = report.get("trade_activity") or {}
    summary = trade.get("summary") or {}
    quality = report.get("quality") or {}
    sync = report.get("selection_sync") or {}
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in (report.get("summary_notes") or []))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>每日研究报告 {html.escape(str(report.get('date', '')))}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #111827;
      --panel-2: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --accent: #38bdf8;
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --border: #334155;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top, #1e293b 0%, var(--bg) 55%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    .wrap {{ max-width: 1360px; margin: 0 auto; padding: 28px 20px 48px; }}
    .hero {{
      display: flex; justify-content: space-between; gap: 20px; align-items: flex-start;
      padding: 24px; border: 1px solid var(--border); border-radius: 20px;
      background: linear-gradient(180deg, rgba(17,24,39,.95), rgba(15,23,42,.95));
      box-shadow: 0 14px 40px rgba(0,0,0,.28);
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    .lead {{ color: var(--muted); margin: 0; }}
    .meta {{ display: grid; gap: 8px; text-align: right; color: var(--muted); }}
    .meta strong {{ color: var(--text); }}
    .grid-cards {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px;
      margin-top: 20px;
    }}
    .card {{
      background: rgba(17,24,39,.94); border: 1px solid var(--border); border-radius: 18px; padding: 18px;
      box-shadow: 0 10px 28px rgba(0,0,0,.18);
    }}
    .card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .card-title {{ font-size: 18px; font-weight: 700; }}
    .card-subtitle {{ color: var(--muted); margin-top: 4px; font-size: 13px; }}
    .labels {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .pill {{
      display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 10px; font-size: 12px;
      border: 1px solid var(--border); color: var(--text); background: rgba(31,41,55,.8);
    }}
    .pill.good {{ background: rgba(34,197,94,.15); color: #86efac; border-color: rgba(34,197,94,.35); }}
    .pill.warn {{ background: rgba(245,158,11,.15); color: #fcd34d; border-color: rgba(245,158,11,.35); }}
    .pill.muted {{ background: rgba(148,163,184,.14); color: #cbd5e1; border-color: rgba(148,163,184,.3); }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }}
    .grid div {{ background: rgba(15,23,42,.75); border: 1px solid rgba(51,65,85,.7); border-radius: 14px; padding: 12px; }}
    .grid span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .grid strong {{ font-size: 15px; }}
    .reason {{ color: var(--muted); margin: 14px 0 0; min-height: 2.5em; }}
    .position-box {{
      margin-top: 14px; padding: 12px 14px; border-radius: 14px; background: rgba(15,23,42,.88);
      border: 1px dashed rgba(96,165,250,.4); color: #cbd5e1;
    }}
    section {{ margin-top: 22px; }}
    section h2 {{ margin: 0 0 12px; font-size: 22px; }}
    .two-col {{ display: grid; grid-template-columns: 1.3fr .7fr; gap: 16px; }}
    table {{ width: 100%; border-collapse: collapse; overflow: hidden; border-radius: 14px; background: rgba(17,24,39,.92); }}
    th, td {{ border-bottom: 1px solid rgba(51,65,85,.75); padding: 10px 12px; text-align: left; }}
    th {{ font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: .04em; }}
    td {{ font-size: 14px; }}
    .panel {{ background: rgba(17,24,39,.92); border: 1px solid var(--border); border-radius: 18px; padding: 18px; }}
    ul {{ margin: 0; padding-left: 18px; color: #d1d5db; }}
    pre {{
      white-space: pre-wrap; background: rgba(15,23,42,.85); border: 1px solid var(--border); border-radius: 16px;
      padding: 16px; overflow-x: auto;
    }}
    .muted {{ color: var(--muted); }}
    a {{ color: var(--accent); }}
    @media (max-width: 980px) {{
      .hero, .two-col {{ grid-template-columns: 1fr; display: grid; }}
      .meta {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>每日研究报告 {html.escape(str(report.get('date', '')))}</h1>
        <p class="lead">只读研究副系统，聚焦“观察级标的”和“可开仓级标的”的区别，不参与下单。</p>
      </div>
      <div class="meta">
        <div>生成时间 <strong>{html.escape(str(report.get('generated_at', '')))}</strong></div>
        <div>模式 <strong>{html.escape(str(report.get('mode', 'unknown')))}</strong></div>
        <div>选股同步 <strong>{'通过' if sync.get('ok') else '不一致'}</strong></div>
        <div>价格带 <strong>{html.escape(_fmt_money(quality.get('price_band', {}).get('min')))} - {html.escape(_fmt_money(quality.get('price_band', {}).get('max')))}</strong></div>
      </div>
    </div>

    <section class="grid-cards">
      <div class="card">
        <div class="card-title">结论摘要</div>
        <p class="muted">研究报告自动从选股、成交、账户快照和订单状态中生成，只读且不影响交易。</p>
        <ul>{notes}</ul>
      </div>
      <div class="card">
        <div class="card-title">数据健康度</div>
        <div class="grid">
          <div><span>TOP 数量</span><strong>{len(report.get('top_cards') or [])}</strong></div>
          <div><span>可开仓级</span><strong>{len(quality.get('entry_ready_symbols') or [])}</strong></div>
          <div><span>观察级</span><strong>{len(quality.get('observation_only_symbols') or [])}</strong></div>
          <div><span>成交笔数</span><strong>{int(summary.get('execution_count', 0) or 0)}</strong></div>
        </div>
      </div>
    </section>

    <section>
      <h2>TOP 研究视图</h2>
      <div class="grid-cards">
        {''.join(top_cards_html) if top_cards_html else '<div class="card">暂无 TOP 数据</div>'}
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <h2>TOP 质量总结</h2>
        {('<table><thead><tr><th>Rank</th><th>Ticker</th><th>Entry Quality</th><th>Good For Entry</th><th>Final</th><th>Reason</th></tr></thead><tbody>' + ''.join(quality_rows) + '</tbody></table>') if quality_rows else '<p class="muted">暂无质量总结。</p>'}
      </div>
      <div class="panel">
        <h2>无交易 / 风控拦截统计</h2>
        <div class="grid">
          <div><span>BUY 信号</span><strong>{int(decision.get('buy_signal_count', 0) or 0)}</strong></div>
          <div><span>BUY 允许</span><strong>{int(decision.get('buy_allowed_count', 0) or 0)}</strong></div>
          <div><span>BUY 阻断</span><strong>{int(decision.get('buy_blocked_count', 0) or 0)}</strong></div>
          <div><span>未成交原因</span><strong>{len(no_trade_rows)}</strong></div>
        </div>
        {('<table><thead><tr><th>Reason</th><th>Count</th></tr></thead><tbody>' + ''.join(no_trade_rows) + '</tbody></table>') if no_trade_rows else '<p class="muted">暂无未成交原因统计。</p>'}
        <div style="height:12px"></div>
        {('<table><thead><tr><th>Risk Block Reason</th><th>Count</th></tr></thead><tbody>' + ''.join(risk_rows) + '</tbody></table>') if risk_rows else '<p class="muted">暂无风控拦截统计。</p>'}
      </div>
    </section>

    <section>
      <h2>策略评分复盘</h2>
      {('<table><thead><tr><th>Rank</th><th>Ticker</th><th>Entry</th><th>High</th><th>Low</th><th>Close</th><th>Max Up</th><th>Max DD</th><th>Buy</th><th>Sell</th><th>Review</th></tr></thead><tbody>' + ''.join(strategy_rows) + '</tbody></table>') if strategy_rows else '<p class="muted">暂无策略评分复盘数据。</p>'}
      <div class="grid" style="margin-top: 14px;">
        <div><span>选股成功</span><strong>{int(strategy.get('success_count', 0) or 0)}</strong></div>
        <div><span>观察正确</span><strong>{int(strategy.get('observation_correct_count', 0) or 0)}</strong></div>
        <div><span>失败</span><strong>{int(strategy.get('failure_count', 0) or 0)}</strong></div>
        <div><span>BUY / SELL</span><strong>{int(strategy.get('buy_triggered_count', 0) or 0)} / {int(strategy.get('sell_triggered_count', 0) or 0)}</strong></div>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <h2>当前持仓</h2>
        {('<table><thead><tr><th>Ticker</th><th>Qty</th><th>Avg</th><th>Price</th><th>MV</th><th>UPnL</th><th>UPnL%</th></tr></thead><tbody>' + ''.join(position_rows) + '</tbody></table>') if position_rows else '<p class="muted">暂无持仓快照。</p>'}
      </div>
      <div class="panel">
        <h2>交易活动</h2>
        <div class="grid">
          <div><span>执行模式</span><strong>{html.escape(str(summary.get('execution_mode', 'paper')))}</strong></div>
          <div><span>成交数</span><strong>{int(summary.get('execution_count', 0) or 0)}</strong></div>
          <div><span>买单</span><strong>{int(summary.get('buy_count', 0) or 0)}</strong></div>
          <div><span>卖单</span><strong>{int(summary.get('sell_count', 0) or 0)}</strong></div>
          <div><span>未决订单</span><strong>{int(summary.get('broker_unresolved_count', 0) or 0)}</strong></div>
          <div><span>最新成交</span><strong>{html.escape(str(summary.get('latest_line', '暂无')))}</strong></div>
        </div>
      </div>
    </section>

    <section class="two-col">
      <div class="panel">
        <h2>当前活跃阻断</h2>
        {('<table><thead><tr><th>Ticker</th><th>Blocked</th><th>Reason</th><th>Failed</th><th>Last Update</th></tr></thead><tbody>' + ''.join(active_order_rows) + '</tbody></table>') if active_order_rows else '<p class="muted">当前没有活跃阻断。</p>'}
      </div>
      <div class="panel">
        <h2>历史 / 非当前标的失败记录</h2>
        {('<table><thead><tr><th>Ticker</th><th>Reason</th><th>Count</th><th>Last Timestamp</th></tr></thead><tbody>' + ''.join(historical_order_rows) + '</tbody></table>') if historical_order_rows else '<p class="muted">暂无历史失败记录。</p>'}
      </div>
    </section>

    <section>
      <h2>原始 JSON</h2>
      <pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2, default=str))}</pre>
    </section>
  </div>
</body>
</html>
"""


def _build_report_payload(
    *,
    project_dir: Path,
    report_day: date,
    fetcher_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    ai_report = _load_ai_selection_report(project_dir)
    selection_state = _load_selection_state(project_dir)
    top_configs = _load_top_configs(project_dir)
    cached = _load_cached_account_and_positions(project_dir)
    positions = cached["positions"]
    selection_sync = _selection_sync(project_dir, report_day, top_configs, ai_report, selection_state)
    trade_activity = _trade_summary(project_dir, report_day)
    trade_summary = trade_activity["summary"]
    top_cards: list[dict[str, Any]] = []
    market_snapshots: dict[str, dict[str, Any]] = {}
    for top in top_configs:
        ticker = _normalize_ticker(top.get("ticker"))
        if not ticker:
            continue
        market = _build_market_snapshot(ticker, fetcher_factory=fetcher_factory)
        market_snapshots[ticker] = market
        top_cards.append(_merge_top_card(top, market, positions))
    focus_symbols = _compute_focus_symbols(top_configs, positions, ai_report)
    order_states = _load_order_state(project_dir)
    now_et = _et_now()
    active: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    for state in order_states:
        ticker = _normalize_ticker(state.get("ticker"))
        if not ticker:
            continue
        if ticker in IGNORE_TICKERS:
            continue
        blocked = state.get("blocked") or {}
        blocked_until = str(blocked.get("blocked_until") or "")
        reason = str(blocked.get("reason") or "")
        failed = state.get("failed_orders_today") or []
        failed_count = len(failed) if isinstance(failed, list) else 0
        last_failed = failed[-1] if failed else {}
        active_block = False
        if blocked_until:
            try:
                active_block = datetime.fromisoformat(blocked_until).replace(tzinfo=None) > now_et.replace(tzinfo=None)
            except Exception:
                active_block = bool(reason)
        last_ts = str(last_failed.get("timestamp") or state.get("updated_at") or "")
        bucket = {
            "ticker": ticker,
            "blocked": active_block,
            "blocked_reason": reason,
            "failed_orders": failed_count,
            "last_timestamp": last_ts,
            "updated_at": str(state.get("updated_at") or ""),
            "focus_symbol": ticker in focus_symbols,
        }
        if ticker in focus_symbols and active_block:
            active.append(bucket)
        elif ticker in focus_symbols and failed_count > 0:
            active.append(bucket)
        elif failed_count > 0:
            historical.append(
                {
                    "ticker": ticker,
                    "reason": reason or str(last_failed.get("reason") or "unknown"),
                    "count": failed_count,
                    "last_timestamp": last_ts,
                    "focus_symbol": False,
                }
            )

    top3_cards = top_cards[:3]
    quality = _quality_summary(top3_cards, ai_report)
    decision_summary = _trade_decision_summary(project_dir, report_day, trade_summary)
    strategy_review = _strategy_review_summary(project_dir, report_day, top3_cards, trade_activity)
    fallback_used = bool(ai_report.get("fallback_used", False))
    execution_status = str(
        ai_report.get("execution_status") or ("COMPLETED" if fallback_used else "COMPLETED")
    ).strip().upper()
    result_quality = str(
        ai_report.get("result_quality") or ("DEGRADED" if fallback_used else "COMPLETE")
    ).strip().upper()
    research_admission = str(
        ai_report.get("research_admission") or ("RESEARCH_ONLY" if fallback_used else "RESEARCH_READY")
    ).strip().upper()
    report = {
        "date": report_day.isoformat(),
        "generated_at": _et_now().isoformat(),
        "mode": "paper"
        if top_configs and all(str(item.get("mode") or "").lower() == "paper" for item in top_configs)
        else "live"
        if top_configs and all(str(item.get("mode") or "").lower() == "live" for item in top_configs)
        else "mixed" if top_configs else "unknown",
        "source_paths": {
            "project_dir": str(project_dir),
            "ai_selection_report": str(project_dir / "reports" / "ai_selection_latest.json"),
            "selection_state": str(project_dir / "state" / "ai_selection_state.json"),
            "broker_cache_dir": str(project_dir / "state" / "broker_cache"),
            "order_state_dir": str(project_dir / "state" / "order_state"),
            "trade_log_dir": str(project_dir / "logs"),
        },
        "settings": {
            "min_price": _safe_float((ai_report.get("settings") or {}).get("min_price"), FALLBACK_PRICE_BAND[0]),
            "max_price": _safe_float((ai_report.get("settings") or {}).get("max_price"), FALLBACK_PRICE_BAND[1]),
            "price_band": {
                "min": _safe_float(((ai_report.get("settings") or {}).get("price_band") or {}).get("min"), FALLBACK_PRICE_BAND[0]),
                "max": _safe_float(((ai_report.get("settings") or {}).get("price_band") or {}).get("max"), FALLBACK_PRICE_BAND[1]),
            },
            "entry_proximity_enabled": bool((ai_report.get("settings") or {}).get("entry_proximity_enabled", True)),
            "entry_proximity_weight": _safe_float((ai_report.get("settings") or {}).get("entry_proximity_weight"), 0.0),
        },
        "selection_sync": selection_sync,
        "selection_state": selection_state,
        "ai_selection": ai_report,
        "selection_execution_status": execution_status,
        "selection_result_quality": result_quality,
        "selection_research_admission": research_admission,
        "selection_stage": str(ai_report.get("selection_stage") or "").strip().upper(),
        "selection_top_n_complete": bool(ai_report.get("top_n_complete", False)),
        "selection_top_n_missing_count": int(ai_report.get("top_n_missing_count") or 0),
        "selection_fallback_used": bool(ai_report.get("fallback_used", False)),
        "selection_provider_audit": ai_report.get("provider_audit") or {},
        "selection_provider_outputs": ai_report.get("provider_outputs") or {},
        "selection_warnings_structured": list(ai_report.get("warnings_structured") or []),
        "selection_warnings": list(ai_report.get("warnings") or []),
        "top_configs": top_configs,
        "top_cards": top3_cards,
        "quality": quality,
        "decision_summary": decision_summary,
        "strategy_review": strategy_review,
        "market_snapshots": market_snapshots,
        "trade_activity": trade_activity,
        "account": cached["account"],
        "positions": positions,
        "focus_symbols": focus_symbols,
        "order_state": {
            "active": active,
            "historical": historical,
            "focus_symbols": focus_symbols,
        },
        "summary_notes": [],
        "no_trade_reason": _no_trade_reason(top3_cards, trade_summary),
    }
    report["summary_notes"] = _format_summary_note(report)
    return report


def _write_outputs(report: dict[str, Any], output_dir: Path) -> ResearchPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    day = str(report.get("date") or _et_now().date().isoformat())
    json_path = output_dir / f"daily-paper-report-{day}.json"
    md_path = output_dir / f"daily-paper-report-{day}.md"
    html_path = output_dir / f"daily-paper-report-{day}.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    html_path.write_text(_render_html(report), encoding="utf-8")
    return ResearchPaths(report_json=json_path, report_md=md_path, report_html=html_path)


def generate_daily_research_report(
    report_date: date | None = None,
    *,
    project_dir: Path | None = None,
    reports_dir: Path | None = None,
    fetcher_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    project_root = Path(project_dir or PROJECT_DIR)
    ai_report = _load_ai_selection_report(project_root)
    selection_state = _load_selection_state(project_root)
    resolved_date = _current_report_date(report_date, ai_report, selection_state)
    report = _build_report_payload(
        project_dir=project_root,
        report_day=resolved_date,
        fetcher_factory=fetcher_factory,
    )
    output_dir = Path(reports_dir or DEFAULT_REPORTS_DIR)
    paths = _write_outputs(report, output_dir)
    report["output_paths"] = {
        "json": str(paths.report_json),
        "markdown": str(paths.report_md),
        "html": str(paths.report_html),
    }
    report["report_generated"] = True
    return report
