import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Any

import numpy as np

from src.config.runtime_values import get_runtime_env
from src.universe.universe import Universe
from src.scoring.scorer import Scorer
from src.news_agent.news_collector import NewsCollector
from src.ai_selector.settings import load_runtime_settings
from src.data.fetcher import PriceFetcher
from src.ai_selector.candidate_ranking import score_candidate

PROJECT_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_DIR / "logs"
INVERSE_REDUCE_ONLY = {"SOXS"}
LIQUID_SPECIAL_ETFS = {
    "SOXL", "SOXS", "LABU", "LABD", "TQQQ", "SQQQ",
    "TNA", "TZA", "FAS", "FAZ", "GUSH", "DRIP", "YINN", "YANG", "NAIL", "DPST",
}


def _selection_log_path(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    return LOG_DIR / f"selection_{stamp}.log"


def _json_log_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalize_ticker(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


class _QualityFilterContext:
    def __init__(self):
        self._quotes: dict[str, tuple[float | None, float | None, float | None, bool]] = {}
        self._history: dict[str, tuple[float | None, float | None, float | None]] = {}
        self._longbridge_quote_ctx = None
        self._longbridge_available = None

    def _get_longbridge_quote_ctx(self):
        if self._longbridge_available is False:
            return None
        if self._longbridge_quote_ctx is not None:
            return self._longbridge_quote_ctx
        app_key = get_runtime_env("LONGBRIDGE_APP_KEY") or get_runtime_env("LONGBRIDGE_API_KEY")
        app_secret = get_runtime_env("LONGBRIDGE_APP_SECRET") or get_runtime_env("LONGBRIDGE_API_SECRET")
        access_token = get_runtime_env("LONGBRIDGE_ACCESS_TOKEN")
        if not (app_key and app_secret and access_token):
            self._longbridge_available = False
            return None
        try:
            import longbridge.openapi as lb

            cfg = lb.Config.from_apikey(
                app_key,
                app_secret,
                access_token,
                http_url=get_runtime_env("LONGBRIDGE_HTTP_URL")
                or get_runtime_env("LONGBRIDGE_BASE_URL")
                or "https://openapi.longbridgeapp.com",
                quote_ws_url=get_runtime_env("LONGBRIDGE_QUOTE_WS_URL"),
                trade_ws_url=get_runtime_env("LONGBRIDGE_TRADE_WS_URL"),
                log_path=get_runtime_env("LONGBRIDGE_LOG_PATH"),
            )
            self._longbridge_quote_ctx = lb.QuoteContext(cfg)
            self._longbridge_available = True
            return self._longbridge_quote_ctx
        except Exception:
            self._longbridge_available = False
            return None

    def close(self) -> None:
        ctx = self._longbridge_quote_ctx
        self._longbridge_quote_ctx = None
        if ctx is None:
            return
        for attr in ("close", "dispose", "release"):
            fn = getattr(ctx, attr, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break

    def _longbridge_depth(self, symbol: str) -> tuple[float | None, float | None, bool]:
        ctx = self._get_longbridge_quote_ctx()
        if ctx is None:
            return None, None, False
        try:
            depth = ctx.depth(symbol=f"{_normalize_ticker(symbol)}.US")
            asks = list(getattr(depth, "asks", []) or [])
            bids = list(getattr(depth, "bids", []) or [])
            best_ask = float(getattr(asks[0], "price", 0.0) or 0.0) if asks else 0.0
            best_bid = float(getattr(bids[0], "price", 0.0) or 0.0) if bids else 0.0
            if best_bid > 0 and best_ask > 0 and best_ask >= best_bid:
                return best_bid, best_ask, True
        except Exception:
            pass
        return None, None, False

    def _longbridge_last_price(self, symbol: str) -> float | None:
        ctx = self._get_longbridge_quote_ctx()
        if ctx is None:
            return None
        try:
            resp = ctx.quote(symbols=[f"{_normalize_ticker(symbol)}.US"])
            items = resp if isinstance(resp, (list, tuple)) else [resp]
            item = items[0] if items else None
            if item is None:
                return None
            price = float(getattr(item, "last_done", 0.0) or 0.0)
            return price if price > 0 else None
        except Exception:
            return None

    def quote_metrics(self, symbol: str) -> tuple[float | None, float | None, float | None, bool]:
        key = _normalize_ticker(symbol)
        if key in self._quotes:
            return self._quotes[key]
        longbridge_price = self._longbridge_last_price(key)
        longbridge_bid, longbridge_ask, longbridge_confirmed = self._longbridge_depth(key)
        if longbridge_price and longbridge_confirmed:
            result = (
                longbridge_price,
                longbridge_bid,
                longbridge_ask,
                True,
            )
        else:
            fetcher = PriceFetcher(key, poll_interval=0)
            quote = fetcher.get_quote()
            if quote is None or quote.price <= 0:
                result = (longbridge_price, longbridge_bid, longbridge_ask, bool(longbridge_confirmed))
            else:
                bid = longbridge_bid if longbridge_confirmed else (float(quote.bid or 0.0) or None)
                ask = longbridge_ask if longbridge_confirmed else (float(quote.ask or 0.0) or None)
                result = (
                    longbridge_price or (float(quote.price or 0.0) or None),
                    bid,
                    ask,
                    bool(longbridge_confirmed or getattr(quote, "bid_ask_confirmed", False)),
                )
        self._quotes[key] = result
        return result

    def history_metrics(self, symbol: str) -> tuple[float | None, float | None, float | None]:
        key = _normalize_ticker(symbol)
        if key in self._history:
            return self._history[key]
        fetcher = PriceFetcher(key, poll_interval=0)
        candles = fetcher.get_ohlcv(period="1mo", interval="1d")
        if len(candles) < 4:
            result = (None, None, None)
        else:
            volumes = [float(c.volume or 0.0) for c in candles[-10:] if float(c.volume or 0.0) > 0]
            closes = [float(c.close or 0.0) for c in candles if float(c.close or 0.0) > 0]
            avg_volume_10 = (sum(volumes) / len(volumes)) if volumes else None
            three_day_change_pct = None
            if len(closes) >= 4 and closes[-4] > 0:
                three_day_change_pct = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0
            current_price = closes[-1] if closes else None
            result = (avg_volume_10, three_day_change_pct, current_price)
        self._history[key] = result
        return result


def _apply_quality_filters_with_report(
    candidates: Sequence[dict],
    *,
    max_seconds: float | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    context = _QualityFilterContext()
    rows: list[dict[str, Any]] = []
    filtered: list[dict] = []
    removed_volume = 0
    removed_spread = 0
    removed_volatility = 0
    removed_missing = 0
    started_at = datetime.now().timestamp()
    timed_out = False

    try:
        for raw in candidates:
            if max_seconds is not None and max_seconds > 0:
                if (datetime.now().timestamp() - started_at) >= max_seconds:
                    timed_out = True
                    break
            item = dict(raw)
            symbol = _normalize_ticker(item.get("ticker"))
            existing_position = bool(item.get("existing_position"))
            ai_selected = bool(item.get("ai_selected", True))

            avg_volume_10, three_day_change_pct, hist_price = context.history_metrics(symbol)
            current_price, bid, ask, bid_ask_confirmed = context.quote_metrics(symbol)
            if item.get("data_source") == "fallback":
                avg_volume_10 = avg_volume_10 or _safe_float(item.get("avg_daily_volume_hint"), 0.0) or None
                current_price = current_price or _safe_float(item.get("price_midpoint_hint"), 0.0) or hist_price
                if three_day_change_pct is None:
                    three_day_change_pct = 0.0
            current_price = current_price or hist_price
            spread_pct = None
            if (
                bid_ask_confirmed
                and current_price
                and bid
                and ask
                and current_price > 0
                and ask >= bid > 0
            ):
                spread_pct = ((ask - bid) / current_price) * 100.0
            if (
                spread_pct is None
                and symbol in LIQUID_SPECIAL_ETFS
                and current_price
                and bid
                and ask
                and current_price > 0
                and ask >= bid > 0
            ):
                spread_pct = ((ask - bid) / current_price) * 100.0
            liquidity_score = (
                float(avg_volume_10) * float(current_price)
                if avg_volume_10 is not None and current_price is not None
                else None
            )
            volatility_limit = 35.0 if symbol in LIQUID_SPECIAL_ETFS else 15.0

            removed = False
            reason = "passed"

            if existing_position:
                reason = "existing_position_bypass"
            elif avg_volume_10 is None or three_day_change_pct is None or current_price is None:
                removed = True
                reason = "missing_market_data"
                removed_missing += 1
            elif avg_volume_10 <= 500_000:
                removed = True
                reason = "volume_filter"
                removed_volume += 1
            elif not bid_ask_confirmed or spread_pct is None:
                if symbol in LIQUID_SPECIAL_ETFS and spread_pct is not None:
                    reason = "special_etf_quote_override"
                else:
                    removed = True
                    reason = "spread_unavailable"
                    removed_missing += 1
            elif spread_pct >= 0.5:
                removed = True
                reason = "spread_filter"
                removed_spread += 1
            elif abs(float(three_day_change_pct)) > volatility_limit:
                removed = True
                reason = "volatility_filter"
                removed_volatility += 1

            item["existing_position"] = existing_position
            item["avg_10d_volume"] = round(avg_volume_10, 2) if avg_volume_10 is not None else None
            item["spread_pct_live"] = round(spread_pct, 4) if spread_pct is not None else None
            item["three_day_change_pct"] = round(three_day_change_pct, 4) if three_day_change_pct is not None else None
            item["liquidity_score"] = round(liquidity_score, 2) if liquidity_score is not None else None
            item["current_price"] = round(current_price, 4) if current_price is not None else None
            item["reduce_only"] = bool(item.get("reduce_only", False))
            item["ai_selected"] = ai_selected

            rows.append(
                {
                    "symbol": symbol,
                    "removed": removed,
                    "reason": reason,
                    "avg_10d_volume": item["avg_10d_volume"],
                    "spread_pct": item["spread_pct_live"],
                    "three_day_change_pct": item["three_day_change_pct"],
                    "liquidity_score": item["liquidity_score"],
                    "existing_position": existing_position,
                }
            )
            if not removed:
                filtered.append(item)
    finally:
        close_fn = getattr(context, "close", None)
        if callable(close_fn):
            close_fn()

    filtered.sort(
        key=lambda item: (
            _safe_float(item.get("liquidity_score"), 0.0),
            _safe_float(item.get("score"), 0.0),
        ),
        reverse=True,
    )
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_candidates_before_filters": len(list(candidates)),
        "removed_by_volume_filter": removed_volume,
        "removed_by_spread_filter": removed_spread,
        "removed_by_volatility_filter": removed_volatility,
        "removed_due_to_missing_data": removed_missing,
        "final_selected_symbols": [],
        "existing_real_positions_preserved": [],
        "timed_out": timed_out,
        "rows": rows,
    }
    return filtered, report


def apply_quality_filters(candidates):
    filtered, _report = _apply_quality_filters_with_report(candidates)
    return filtered


def write_selection_filter_log(report: dict[str, Any], now: datetime | None = None) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _selection_log_path(now=now)
    lines = [
        _json_log_line(
            {
                "summary": {
                    "total_candidates_before_filters": int(report.get("total_candidates_before_filters", 0) or 0),
                    "removed_by_volume_filter": int(report.get("removed_by_volume_filter", 0) or 0),
                    "removed_by_spread_filter": int(report.get("removed_by_spread_filter", 0) or 0),
                    "removed_by_volatility_filter": int(report.get("removed_by_volatility_filter", 0) or 0),
                    "removed_due_to_missing_data": int(report.get("removed_due_to_missing_data", 0) or 0),
                    "final_selected_symbols": list(report.get("final_selected_symbols") or []),
                    "backfilled_symbols": list(report.get("backfilled_symbols") or []),
                    "existing_real_positions_preserved": list(report.get("existing_real_positions_preserved") or []),
                    "selection_stage": str(report.get("selection_stage") or ""),
                    "timed_out": bool(report.get("timed_out", False)),
                    "generated_at": report.get("generated_at"),
                }
            }
        )
    ]
    for row in report.get("rows") or []:
        lines.append(_json_log_line(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class AIStrategySelector:
    def __init__(self, config=None):
        self.universe = Universe()
        self.news = NewsCollector()
        self.scorer = Scorer()
        self.selection_size = self._selection_size_from_env()
        self.max_symbols = self._max_symbols_from_env()
        self._last_quality_filter_report: dict[str, Any] = {}

    def _selection_size_from_env(self) -> int:
        raw = os.environ.get("AI_SELECTOR_TOP_K", "5")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 5
        return max(1, value)

    def _max_symbols_from_env(self) -> int:
        raw = os.environ.get("AI_SELECTOR_MAX_SYMBOLS", "50")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 50
        return max(1, value)

    def _filter_candidate_limit_from_env(self) -> int:
        raw = os.environ.get("AI_SELECTOR_FILTER_CANDIDATE_LIMIT", "12")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 12
        return max(self.selection_size, value)

    def _total_budget_seconds_from_env(self) -> float:
        raw = os.environ.get("AI_SELECTOR_TOTAL_BUDGET_SECONDS", "15")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 15.0
        return max(3.0, value)

    def _quality_budget_seconds_from_env(self) -> float:
        raw = os.environ.get("AI_SELECTOR_QUALITY_BUDGET_SECONDS", "8")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 8.0
        return max(1.0, value)

    def _default_reduce_only(self) -> bool:
        raw = str(os.environ.get("SOXS_SELECTOR_REDUCE_ONLY_DEFAULT", "0") or "0").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _live_data_requested(self) -> bool:
        return os.environ.get("AI_SELECTOR_LIVE_DATA", "1") != "0"

    def _score_with_live_flag(self, symbols: List[str], news_map: Dict[str, List[str]], live_enabled: bool) -> List[dict]:
        previous = os.environ.get("AI_SELECTOR_LIVE_DATA")
        os.environ["AI_SELECTOR_LIVE_DATA"] = "1" if live_enabled else "0"
        try:
            self.scorer = Scorer()
            return self.scorer.score_universe(symbols, news_map)
        finally:
            if previous is None:
                os.environ.pop("AI_SELECTOR_LIVE_DATA", None)
            else:
                os.environ["AI_SELECTOR_LIVE_DATA"] = previous
            self.scorer = Scorer()

    def run_selection(self, write_configs: bool = True, symbols_override: List[str] | None = None):
        selection_started_at = datetime.now().timestamp()
        # 1. build universe
        if symbols_override:
            symbols = []
            seen = set()
            for raw in symbols_override:
                symbol = _normalize_ticker(raw)
                if symbol and symbol not in seen:
                    symbols.append(symbol)
                    seen.add(symbol)
        else:
            source = os.environ.get("AI_SELECTOR_UNIVERSE", "sample")
            if source == "sample":
                symbols = self.universe._load_local_snapshot()
            else:
                symbols = self.universe.build_universe(source=source)

        symbols = symbols[:self.max_symbols]

        # 2. collect data & news. News scraping is optional because it can be
        # slow/unreliable before the open; technical/volume scoring still works.
        if os.environ.get("AI_SELECTOR_FETCH_NEWS", "0") == "1":
            news_map = self.news.collect_for_symbols(symbols)
        else:
            news_map = {symbol: [] for symbol in symbols}

        # 3. score
        live_requested = self._live_data_requested()
        scored = self._score_with_live_flag(symbols, news_map, live_enabled=live_requested)
        scored = [score_candidate(item) for item in scored]
        data_mode = "live" if live_requested else "fallback"
        fallback_used = False

        if live_requested and len(scored) < self.selection_size:
            fallback_scored = self._score_with_live_flag(symbols, news_map, live_enabled=False)
            if fallback_scored:
                fallback_used = True
                existing = {item.get("ticker") for item in scored}
                scored.extend(score_candidate(item) for item in fallback_scored if item.get("ticker") not in existing)
                scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
                data_mode = "mixed" if existing else "fallback"

        scored = sorted(scored, key=lambda item: item.get("score", 0.0), reverse=True)
        preliminary_pool = [dict(item) for item in scored[: max(self.selection_size, self._filter_candidate_limit_from_env())]]
        preliminary_topk = self._select_diversified_top_k(preliminary_pool, self.selection_size)
        default_reduce_only = self._default_reduce_only()
        for item in preliminary_topk:
            item["reduce_only"] = default_reduce_only
            item["selection_penalty_reason"] = item.get("selection_penalty_reason") or "fast_start_preliminary"
            item["fast_start_preliminary"] = True

        candidate_limit = self._filter_candidate_limit_from_env()
        candidates_for_filter = scored[:candidate_limit]
        total_budget = self._total_budget_seconds_from_env()
        elapsed_before_quality = max(0.0, datetime.now().timestamp() - selection_started_at)
        quality_budget = min(
            self._quality_budget_seconds_from_env(),
            max(0.0, total_budget - elapsed_before_quality),
        )
        selection_stage = "quality_refined"
        if quality_budget <= 0 or os.environ.get("AI_SELECTOR_FAST_START_ONLY", "0") == "1":
            filtered_candidates = []
            filter_report = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "total_candidates_before_filters": len(list(candidates_for_filter)),
                "removed_by_volume_filter": 0,
                "removed_by_spread_filter": 0,
                "removed_by_volatility_filter": 0,
                "removed_due_to_missing_data": 0,
                "final_selected_symbols": [],
                "existing_real_positions_preserved": [],
                "timed_out": True,
                "rows": [],
            }
            selection_stage = "fast_preliminary"
        else:
            filtered_candidates, filter_report = _apply_quality_filters_with_report(
                candidates_for_filter,
                max_seconds=quality_budget,
            )
        filter_report["pre_filter_candidate_limit"] = candidate_limit
        self._last_quality_filter_report = filter_report

        # 4. prefer liquidity after quality checks, then diversify TopK by sector/correlation
        scored_sorted = list(filtered_candidates)
        top10 = scored_sorted[:10]
        topk = self._select_diversified_top_k(top10, self.selection_size)
        if not topk:
            topk = [dict(item) for item in preliminary_topk]
            selection_stage = "fast_preliminary"
        backfilled_symbols: list[str] = []
        if len(topk) < self.selection_size:
            selected_tickers = {_normalize_ticker(item.get("ticker")) for item in topk}
            for item in scored:
                ticker = _normalize_ticker(item.get("ticker"))
                if not ticker or ticker in selected_tickers:
                    continue
                candidate = dict(item)
                candidate["reduce_only"] = default_reduce_only
                candidate["quality_backfill"] = True
                candidate["selection_penalty_reason"] = "quality_filter_backfill"
                topk.append(candidate)
                selected_tickers.add(ticker)
                backfilled_symbols.append(ticker)
                if len(topk) >= self.selection_size:
                    break
            if filter_report.get("timed_out"):
                selection_stage = "quality_timed_out_backfilled"
            elif selection_stage != "fast_preliminary":
                selection_stage = "quality_backfilled"
        filter_report["final_selected_symbols"] = [_normalize_ticker(item.get("ticker")) for item in topk]
        filter_report["backfilled_symbols"] = backfilled_symbols
        filter_report["selection_stage"] = selection_stage
        write_selection_filter_log(filter_report)

        # write configs for selected TopK
        if write_configs:
            from src.ai_selector.config_writer import write_top_configs
            write_top_configs(topk)

        report_rows = self._format_report_rows(topk)
        return {
            "top10": top10,
            "top5": topk,
            "top3": topk[:3],
            "report": report_rows,
            "settings": {
                "max_price": float(round(self.scorer.max_price, 2)),
                "min_price": float(round(self.scorer.min_price, 2)),
                "auto_refresh_minutes": int(load_runtime_settings().get("auto_refresh_minutes", 5) or 5),
                "top_k": self.selection_size,
                "max_symbols": self.max_symbols,
                "data_mode": data_mode,
                "fallback_used": fallback_used,
                "selection_stage": selection_stage,
            },
            "quality_filter_report": filter_report,
        }

    def _select_diversified_top_k(self, candidates: List[dict], max_items: int) -> List[dict]:
        remaining = [dict(item) for item in candidates]
        selected: List[dict] = []

        while remaining and len(selected) < max_items:
            best_idx = 0
            best_item = None
            best_score = -math.inf
            selected_sectors = {item.get("sector") or "Unknown" for item in selected}
            for idx, item in enumerate(remaining):
                penalty, max_corr = self._correlation_penalty(item, selected)
                final_score = self._final_score(item, penalty)
                sector_bonus = 8.0 if (item.get("sector") or "Unknown") not in selected_sectors else 0.0
                candidate = dict(item)
                candidate["correlation_penalty"] = float(round(penalty, 2))
                candidate["max_pairwise_correlation"] = float(round(max_corr, 4))
                candidate["diversity_bonus"] = float(round(sector_bonus, 2))
                candidate["score"] = float(round(final_score + sector_bonus, 2))
                candidate["base_score"] = float(round(item.get("base_score", item.get("score", 0.0)), 2))
                candidate["selection_penalty_reason"] = self._selection_penalty_reason(item, selected, penalty, max_corr)
                candidate = score_candidate(candidate)
                if candidate["score"] > best_score:
                    best_score = candidate["score"]
                    best_item = candidate
                    best_idx = idx
            if best_item is None:
                break
            selected.append(best_item)
            del remaining[best_idx]

        selected.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return selected

    def _select_diversified_top3(self, candidates: List[dict]) -> List[dict]:
        return self._select_diversified_top_k(candidates, 3)

    def _final_score(self, item: dict, correlation_penalty: float) -> float:
        vol = float(item.get("volatility_score", 0.0))
        volume = float(item.get("volume_score", 0.0))
        trend = float(item.get("trend_fit_score", 0.0))
        repeat = float(item.get("repeatability_score", 0.0))
        drawdown = float(item.get("drawdown_safety_score", 0.0))
        correlation_bonus = max(0.0, 100.0 - correlation_penalty)
        return (
            0.30 * vol
            + 0.20 * volume
            + 0.20 * trend
            + 0.15 * repeat
            + 0.10 * drawdown
            + 0.05 * correlation_bonus
        )

    def _correlation_penalty(self, item: dict, selected: Sequence[dict]) -> tuple[float, float]:
        if not selected:
            return 0.0, 0.0

        penalty = 0.0
        max_corr = 0.0
        item_sector = item.get("sector") or "Unknown"
        item_returns = item.get("series", {}).get("returns", [])

        for other in selected:
            other_returns = other.get("series", {}).get("returns", [])
            corr = self._series_correlation(item_returns, other_returns)
            max_corr = max(max_corr, abs(corr))
            if corr >= 0.90:
                penalty = max(penalty, 60.0)
            elif corr >= 0.80:
                penalty = max(penalty, 40.0)
            elif corr >= 0.65:
                penalty = max(penalty, 20.0)
            elif corr >= 0.50:
                penalty = max(penalty, 10.0)

            other_sector = other.get("sector") or "Unknown"
            if item_sector != "Unknown" and item_sector == other_sector:
                penalty = max(penalty, 35.0)

        return float(min(100.0, penalty)), float(min(1.0, max_corr))

    def _series_correlation(self, a: Sequence[float], b: Sequence[float]) -> float:
        if len(a) < 5 or len(b) < 5:
            return 0.0
        arr_a = np.array(a[-60:], dtype=float)
        arr_b = np.array(b[-60:], dtype=float)
        n = min(len(arr_a), len(arr_b))
        if n < 5:
            return 0.0
        arr_a = arr_a[-n:]
        arr_b = arr_b[-n:]
        if np.std(arr_a) == 0 or np.std(arr_b) == 0:
            return 0.0
        corr = float(np.corrcoef(arr_a, arr_b)[0, 1])
        if math.isnan(corr):
            return 0.0
        return max(-1.0, min(1.0, corr))

    def _selection_penalty_reason(self, item: dict, selected: Sequence[dict], penalty: float, max_corr: float) -> str:
        if not selected:
            return "first pick"
        reasons = []
        item_sector = item.get("sector") or "Unknown"
        if any((other.get("sector") or "Unknown") == item_sector for other in selected):
            reasons.append("same sector")
        if max_corr >= 0.90:
            reasons.append("high correlation")
        elif max_corr >= 0.80:
            reasons.append("elevated correlation")
        elif max_corr >= 0.65:
            reasons.append("moderate correlation")
        if not reasons and penalty <= 0:
            reasons.append("diversified")
        return ", ".join(reasons)

    def _format_report_rows(self, rows: List[dict]) -> List[dict]:
        out = []
        for idx, row in enumerate(rows, start=1):
            entry = row.get("entry") if isinstance(row.get("entry"), dict) else {}
            entry_score = entry.get("entry_proximity_score")
            if entry_score is None:
                entry_score = row.get("entry_proximity_score", 50.0)
            entry_quality = entry.get("entry_quality")
            if entry_quality is None:
                entry_quality = row.get("entry_quality", "unknown")
            entry_reason = entry.get("entry_reason")
            if entry_reason is None:
                entry_reason = row.get("entry_reason", "")
            out.append({
                "rank": idx,
                "ticker": row.get("ticker"),
                "score": float(round(row.get("score", 0.0), 2)),
                "candidate_score": float(round(row.get("candidate_score", row.get("score", 0.0)), 2)),
                "liquidity_score": float(round(row.get("liquidity_score", 0.0), 2)),
                "trend_score": float(round(row.get("trend_score", row.get("trend_fit_score", 0.0)), 2)),
                "volatility": float(round(row.get("volatility_score", 0.0), 2)),
                "volume": float(round(row.get("volume_score", 0.0), 2)),
                "trend_fit": float(round(row.get("trend_fit_score", 0.0), 2)),
                "risk_score": float(round(row.get("risk_score", 0.0), 2)),
                "strategy_fit_score": float(round(row.get("strategy_fit_score", 0.0), 2)),
                "recommended_strategy": row.get("recommended_strategy"),
                "score_reason": row.get("score_reason"),
                "repeatability": float(round(row.get("repeatability_score", 0.0), 2)),
                "drawdown": float(round(row.get("drawdown_safety_score", 0.0), 2)),
                "correlation_penalty": float(round(row.get("correlation_penalty", 0.0), 2)),
                "diversity_bonus": float(round(row.get("diversity_bonus", 0.0), 2)),
                "suggested_range": row.get("suggested_range"),
                "sector": row.get("sector"),
                "entry": {
                    "entry_proximity_score": float(round(float(entry_score), 2)),
                    "good_for_entry_now": bool(entry.get("good_for_entry_now", row.get("good_for_entry_now", False))),
                    "entry_quality": entry_quality,
                    "entry_reason": entry_reason,
                    "range_position": entry.get("range_position", row.get("range_position")),
                    "dist_to_support": entry.get("dist_to_support", row.get("dist_to_support")),
                    "dist_to_resistance": entry.get("dist_to_resistance", row.get("dist_to_resistance")),
                },
            })
        return out
