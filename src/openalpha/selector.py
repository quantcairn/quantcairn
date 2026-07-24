import json
import math
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Any

import numpy as np

from src.config.runtime_values import get_runtime_env
from src.universe.universe import Universe
from src.scoring.scorer import Scorer
from src.news_agent.news_collector import NewsCollector
from src.openalpha.settings import load_runtime_settings
from src.data.fetcher import PriceFetcher
from src.openalpha.candidate_ranking import score_candidate
from src.openalpha.funnel_tracker import FunnelTracker, FunnelStageRecord

PROJECT_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_DIR / "logs"


def _load_managed_universe() -> list[str] | None:
    """Load enabled symbols from the managed universe snapshot (Stage 10).

    Returns None if the managed universe is unavailable, signalling the
    caller to fall back to the legacy sample/SP500 universe.
    """
    try:
        from src.universe.manager import UniverseManager
        mgr = UniverseManager()
        snap = mgr.load_snapshot()
        if snap is None:
            snap = mgr.build_snapshot(dry_run=True)
        if snap is None or snap.enabled_symbols <= 0:
            return None
        return sorted(s.symbol for s in snap.symbols if s.enabled)
    except Exception:
        return None
INVERSE_REDUCE_ONLY = {"SOXS"}
LIQUID_SPECIAL_ETFS = {
    "SOXL", "SOXS", "LABU", "LABD", "TQQQ", "SQQQ",
    "TNA", "TZA", "FAS", "FAZ", "GUSH", "DRIP", "YINN", "YANG", "NAIL", "DPST",
}


def _selection_log_path(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    return LOG_DIR / f"selection_{stamp}.log"


def _write_text_atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


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


def _build_market_data_diagnostics(
    universe_symbols: list[str],
    scored_symbols_set: set[str],
) -> list[dict[str, Any]]:
    """Build per-symbol diagnostic records for every universe symbol that
    didn't reach the scoring pipeline.

    Each record carries the exact failure path:
      - OHLCV fetch error / available rows
      - Whether a fallback profile exists
      - Whether the fallback profile was blocked by the universe filter
    """
    try:
        from src.openalpha.data_diagnostics import diagnose_market_data_drops
        return diagnose_market_data_drops(
            universe_symbols=[_normalize_ticker(s) for s in universe_symbols],
            scored_symbols=[_normalize_ticker(s) for s in scored_symbols_set],
        )
    except Exception:
        # Never let diagnostics break selection — fall back to simple reason
        return [
            {"symbol": _normalize_ticker(sym),
             "reason_code": "market_data_sufficiency_failed",
             "reason_detail": "diagnostics unavailable; scoring returned no result"}
            for sym in universe_symbols
            if _normalize_ticker(sym) not in scored_symbols_set
        ]


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
            try:
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
            finally:
                fetcher.close()
        self._quotes[key] = result
        return result

    def history_metrics(self, symbol: str) -> tuple[float | None, float | None, float | None]:
        key = _normalize_ticker(symbol)
        if key in self._history:
            return self._history[key]
        fetcher = PriceFetcher(key, poll_interval=0)
        try:
            candles = fetcher.get_ohlcv(period="1mo", interval="1d")
        finally:
            fetcher.close()
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


def _quality_mode_is_strict(run_mode: str) -> bool:
    """Quality checks are strict only in FULL mode when live quotes are available."""
    return str(run_mode or "").strip().upper() == "FULL"


def _apply_quality_filters_with_report(
    candidates: Sequence[dict],
    *,
    max_seconds: float | None = None,
    run_mode: str = "FULL",
) -> tuple[list[dict], dict[str, Any]]:
    strict = _quality_mode_is_strict(run_mode)
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
            is_fallback = item.get("data_source") == "fallback"

            avg_volume_10, three_day_change_pct, hist_price = context.history_metrics(symbol)
            current_price, bid, ask, bid_ask_confirmed = context.quote_metrics(symbol)

            # ── Fallback data augmentation ──────────────────────────────────
            if is_fallback or not strict:
                volume_hint = _safe_float(item.get("avg_daily_volume_hint"), 0.0) or None
                price_hint = _safe_float(item.get("price_midpoint_hint"), 0.0) or None
                avg_volume_10 = avg_volume_10 or volume_hint
                current_price = current_price or price_hint or hist_price
                if three_day_change_pct is None:
                    three_day_change_pct = 0.0
            else:
                if item.get("data_source") == "fallback":
                    avg_volume_10 = avg_volume_10 or _safe_float(item.get("avg_daily_volume_hint"), 0.0) or None
                    current_price = current_price or _safe_float(item.get("price_midpoint_hint"), 0.0) or hist_price
                    if three_day_change_pct is None:
                        three_day_change_pct = 0.0

            current_price = current_price or hist_price

            spread_pct = None
            # Compute bid/ask spread only when quotes are confirmed
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
            elif avg_volume_10 is None or current_price is None:
                # Always reject if we have zero data (no live, no history, no hints)
                removed = True
                reason = "missing_market_data"
                removed_missing += 1
            elif avg_volume_10 <= 500_000:
                # Volume filter — always enforced, uses hint data when available
                removed = True
                reason = "volume_filter"
                removed_volume += 1
            elif strict:
                # ── STRICT (FULL) mode: require realtime quotes ──────────
                if three_day_change_pct is None:
                    removed = True
                    reason = "missing_market_data"
                    removed_missing += 1
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
            else:
                # ── RELAXED (EOD / AFTER_MARKET / DEGRADED) mode ────────
                # Skip spread and volatility checks — only enforce:
                #  - basic data presence (price + volume hints)
                #  - volume filter
                #  - existing position bypass
                if three_day_change_pct is not None and abs(float(three_day_change_pct)) > volatility_limit:
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


def apply_quality_filters(candidates, run_mode: str = "FULL"):
    filtered, _report = _apply_quality_filters_with_report(candidates, run_mode=run_mode)
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
    return _write_text_atomic(path, "\n".join(lines) + "\n")


class AIStrategySelector:
    def __init__(self, config=None):
        self.universe = Universe()
        self.news = NewsCollector()
        self.scorer = Scorer()
        self.selection_size = self._selection_size_from_env()
        self.max_symbols = self._max_symbols_from_env()
        self._last_quality_filter_report: dict[str, Any] = {}

    def _selection_size_from_env(self) -> int:
        raw = os.environ.get("OPENALPHA_TOP_K", "5")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 5
        return max(1, value)

    def _max_symbols_from_env(self) -> int:
        raw = os.environ.get("OPENALPHA_MAX_SYMBOLS", "50")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 50
        return max(1, value)

    def _filter_candidate_limit_from_env(self) -> int:
        raw = os.environ.get("OPENALPHA_FILTER_CANDIDATE_LIMIT", "12")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 12
        return max(self.selection_size, value)

    def _total_budget_seconds_from_env(self) -> float:
        raw = os.environ.get("OPENALPHA_TOTAL_BUDGET_SECONDS", "15")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 15.0
        return max(3.0, value)

    def _quality_budget_seconds_from_env(self) -> float:
        raw = os.environ.get("OPENALPHA_QUALITY_BUDGET_SECONDS", "8")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 8.0
        return max(1.0, value)

    def _default_reduce_only(self) -> bool:
        raw = str(os.environ.get("SOXS_SELECTOR_REDUCE_ONLY_DEFAULT", "0") or "0").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _live_data_requested(self) -> bool:
        return os.environ.get("OPENALPHA_LIVE_DATA", "1") != "0"

    def _score_with_live_flag(self, symbols: List[str], news_map: Dict[str, List[str]], live_enabled: bool) -> List[dict]:
        previous = os.environ.get("OPENALPHA_LIVE_DATA")
        os.environ["OPENALPHA_LIVE_DATA"] = "1" if live_enabled else "0"
        try:
            self.scorer = Scorer()
            return self.scorer.score_universe(symbols, news_map)
        finally:
            if previous is None:
                os.environ.pop("OPENALPHA_LIVE_DATA", None)
            else:
                os.environ["OPENALPHA_LIVE_DATA"] = previous
            self.scorer = Scorer()

    def run_selection(self, write_configs: bool = True, symbols_override: List[str] | None = None):
        selection_started_at = datetime.now().timestamp()
        selection_run_id = uuid.uuid4().hex
        selection_date = datetime.now().strftime("%Y-%m-%d")
        tracker = FunnelTracker(
            selection_run_id=selection_run_id,
            selection_date=selection_date,
        )

        # ── Preflight: check market state before building universe ───────
        _preflight_report: dict[str, Any] = {}
        _run_mode: str = "FULL"
        try:
            from src.openalpha.preflight import run_preflight as _run_preflight, PreflightReport
            _pf = _run_preflight(dry_run=True)
            _preflight_report = _pf.to_dict()
            _run_mode = str(_preflight_report.get("run_mode") or "FULL").strip().upper()
        except Exception:
            _preflight_report = {"market_state": "UNKNOWN", "run_mode": "FULL"}

        # 1. build universe
        source = "override"
        if symbols_override:
            symbols = []
            seen = set()
            for raw in symbols_override:
                symbol = _normalize_ticker(raw)
                if symbol and symbol not in seen:
                    symbols.append(symbol)
                    seen.add(symbol)
        else:
            source = os.environ.get("OPENALPHA_UNIVERSE", "managed")
            if source == "managed":
                managed = _load_managed_universe()
                if managed:
                    symbols = managed
                else:
                    # Fallback to legacy sample
                    symbols = self.universe._load_local_snapshot()
            elif source == "sample":
                symbols = self.universe._load_local_snapshot()
            else:
                symbols = self.universe.build_universe(source=source)

        tracker.add_stage("UNIVERSE", symbols, symbols)

        symbols = symbols[:self.max_symbols]
        tracker.add_stage("UNIVERSE_FILTER", symbols, symbols)

        # 2. Market data: validate OHLCV availability independently of scoring.
        #    MARKET_DATA output = symbols with >= 60 rows of OHLCV data,
        #    regardless of whether scoring succeeds.
        #    SCORING_ELIGIBLE then tracks which of those symbols produce a score.
        if os.environ.get("OPENALPHA_FETCH_NEWS", "0") == "1":
            news_map = self.news.collect_for_symbols(symbols)
        else:
            news_map = {symbol: [] for symbol in symbols}

        # Determine data availability per symbol (pre-scoring check)
        try:
            from src.openalpha.data_diagnostics import check_data_availability
            _data_available, _market_data_dropped = check_data_availability(symbols)
        except Exception:
            _data_available = list(symbols)
            _market_data_dropped = []
        # Never let data availability reduce the pool to zero — that prevents
        # scoring from running at all.  Scoring will still reject bad symbols.
        if not _data_available:
            _data_available = list(symbols)
        tracker.add_stage("MARKET_DATA", symbols, _data_available, dropped=_market_data_dropped)

        # 3. score only data-available symbols
        live_requested = self._live_data_requested()
        scored = self._score_with_live_flag(_data_available, news_map, live_enabled=live_requested)
        scored = [score_candidate(item) for item in scored]

        data_mode = "live" if live_requested else "fallback"
        fallback_used = False

        # ── Fallback scoring (live→EOD) run BEFORE pipeline stages so chain stays consistent ──
        if live_requested and len(scored) < self.selection_size:
            fallback_scored = self._score_with_live_flag(_data_available, news_map, live_enabled=False)
            if fallback_scored:
                fallback_used = True
                existing = {item.get("ticker") for item in scored}
                scored.extend(score_candidate(item) for item in fallback_scored if item.get("ticker") not in existing)
                scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
                data_mode = "mixed" if existing else "fallback"

        scored = sorted(scored, key=lambda item: item.get("score", 0.0), reverse=True)

        scoring_eligible = [item for item in scored if bool(item.get("scoring_eligible", True))]
        scoring_dropped = [
            {"symbol": _normalize_ticker(item.get("ticker")),
             "reason_code": item.get("scoring_block_reason") or "scoring_ineligible"}
            for item in scored if not bool(item.get("scoring_eligible", True))
        ]
        tracker.add_stage("SCORING_ELIGIBLE", _data_available, scoring_eligible, dropped=scoring_dropped)
        tracker.add_stage("BASE_RANKING", scoring_eligible, scoring_eligible)

        # Track formal eligibility
        formal_eligible = [item for item in scored if item.get("formal_scoring_eligibility", True)]
        formal_dropped = [
            {"symbol": _normalize_ticker(item.get("ticker")),
             "reason_code": item.get("scoring_block_reason") or "formal_scoring_ineligible"}
            for item in scored if not item.get("formal_scoring_eligibility", True)
        ]
        tracker.add_stage("FORMAL_ELIGIBILITY", scored, formal_eligible, dropped=formal_dropped)

        # 4. DATA_QUALITY: apply market data quality checks (spread, volume, volatility).
        #    Runs BEFORE COMPOSITION_FILTER so diversity picks come from quality-passed pool.
        default_reduce_only = self._default_reduce_only()
        candidate_limit = self._filter_candidate_limit_from_env()
        total_budget = self._total_budget_seconds_from_env()
        elapsed_before_quality = max(0.0, datetime.now().timestamp() - selection_started_at)
        quality_budget = min(
            self._quality_budget_seconds_from_env(),
            max(0.0, total_budget - elapsed_before_quality),
        )

        candidates_for_filter = formal_eligible[:candidate_limit]
        quality_fallback_active = False
        selection_stage = "quality_refined"

        if quality_budget <= 0 or os.environ.get("OPENALPHA_FAST_START_ONLY", "0") == "1":
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
                run_mode=_run_mode,
            )

        quality_dropped = [
            row for row in (filter_report.get("rows") or [])
            if row.get("removed")
        ]
        # Save pre-quality pool for fallback before recording the stage
        _pre_quality_pool = [dict(item) for item in formal_eligible[:max(self.selection_size, candidate_limit)]]
        tracker.add_stage("DATA_QUALITY", candidates_for_filter, filtered_candidates,
                          dropped=quality_dropped)
        filter_report["pre_filter_candidate_limit"] = candidate_limit
        self._last_quality_filter_report = filter_report

        # 5. COMPOSITION_FILTER: diversify the quality-passed pool by sector/correlation.
        #    When quality rejects all in FULL mode: use pre-quality pool as Preview Candidates
        #    (research-only). In non-FULL modes, relaxed quality checks keep candidates alive.
        quality_mode_is_strict = _quality_mode_is_strict(_run_mode)
        if not filtered_candidates and _pre_quality_pool and quality_mode_is_strict:
            # FULL mode: quality gate rejected all → preview only, no formal TOP
            topk = self._select_diversified_top_k(_pre_quality_pool, self.selection_size)
            for item in topk:
                item["reduce_only"] = default_reduce_only
                item["selection_penalty_reason"] = "quality_fallback_preview"
                item["data_source"] = item.get("data_source", "fallback")
            tracker.add_stage("COMPOSITION_FILTER", _pre_quality_pool, _pre_quality_pool,
                              dropped=[{"symbol": _normalize_ticker(item.get("ticker")),
                                        "reason_code": "quality_fallback_preview"}
                                       for item in _pre_quality_pool
                                       if not any(_normalize_ticker(item.get("ticker")) == _normalize_ticker(t.get("ticker"))
                                                  for t in topk)])
            selection_stage = "fast_preliminary"
            quality_fallback_active = True
        elif not filtered_candidates and _pre_quality_pool:
            # Non-FULL mode: relaxed quality still rejected (e.g., no data at all)
            # Use pre-quality pool, but mark as EOD mode — these become formal in EOD context
            quality_passed = [dict(item) for item in _pre_quality_pool]
            for item in quality_passed:
                item["data_source"] = item.get("data_source", "eod_fallback")
                item["candidate_type"] = "RESEARCH_ONLY"
            topk_input = quality_passed[:max(self.selection_size, candidate_limit)]
            topk = self._select_diversified_top_k(topk_input, self.selection_size)
            for item in topk:
                item["reduce_only"] = default_reduce_only
            tracker.add_stage("COMPOSITION_FILTER", topk_input, topk,
                              dropped=[{"symbol": _normalize_ticker(item.get("ticker")),
                                        "reason_code": "composition_limit"}
                                       for item in topk_input
                                       if not any(_normalize_ticker(item.get("ticker")) == _normalize_ticker(t.get("ticker"))
                                                  for t in topk)])
            selection_stage = "eod_quality_relaxed"
        else:
            quality_passed = list(filtered_candidates)
            if not quality_passed:
                quality_passed = [dict(item) for item in _pre_quality_pool]
                for item in quality_passed:
                    item["data_source"] = item.get("data_source", "eod_fallback")
                selection_stage = "eod_no_quality_pass"

            # In non-FULL modes, mark candidates as EOD-validated
            if not quality_mode_is_strict:
                for item in quality_passed:
                    if not item.get("data_source"):
                        item["data_source"] = "eod_validated"
                    item["candidate_type"] = "RESEARCH_ONLY"

            topk_input = quality_passed[:max(self.selection_size, candidate_limit)]
            topk = self._select_diversified_top_k(topk_input, self.selection_size)
            for item in topk:
                item["reduce_only"] = default_reduce_only

            tracker.add_stage("COMPOSITION_FILTER", topk_input, topk,
                              dropped=[{"symbol": _normalize_ticker(item.get("ticker")),
                                        "reason_code": "composition_limit"}
                                       for item in topk_input
                                       if not any(_normalize_ticker(item.get("ticker")) == _normalize_ticker(t.get("ticker"))
                                                  for t in topk)])

        backfilled_symbols: list[str] = []
        filter_report["final_selected_symbols"] = [_normalize_ticker(item.get("ticker")) for item in topk]
        filter_report["backfilled_symbols"] = backfilled_symbols
        filter_report["selection_stage"] = selection_stage
        write_selection_filter_log(filter_report)

        # ── FORMAL_TOP: output depends on mode ──
        preview_symbols: list[str] = []
        formal_symbols: list[str] = []
        if quality_fallback_active:
            # FULL mode only: quality gate rejected all → preview only
            tracker.add_stage("FORMAL_TOP", _pre_quality_pool, topk)
            preview_symbols = [_normalize_ticker(item.get("ticker")) for item in topk]
            formal_symbols: list[str] = []
            tracker.mark_quality_fallback(
                preview_symbols=preview_symbols,
                formal_symbols=formal_symbols,
            )
        elif not quality_mode_is_strict and not filtered_candidates:
            # Non-FULL mode: all quality-rejected but mode-aware — topk = pre_quality pool
            tracker.add_stage("FORMAL_TOP", _pre_quality_pool, topk)
            preview_symbols = [_normalize_ticker(item.get("ticker")) for item in topk]
            formal_symbols = [_normalize_ticker(item.get("ticker")) for item in topk]
            tracker.set_formal_candidates(formal_symbols)
            tracker.mark_quality_relaxed()
        elif not quality_mode_is_strict:
            # Non-FULL mode with quality-passed candidates → they become formal
            tracker.add_stage("FORMAL_TOP", quality_passed, topk)
            preview_symbols = [_normalize_ticker(item.get("ticker")) for item in quality_passed]
            formal_symbols = [_normalize_ticker(item.get("ticker")) for item in topk]
            tracker.set_formal_candidates(formal_symbols)
        else:
            # FULL mode, normal path: topk ⊆ quality_passed → invariant holds.
            tracker.add_stage("FORMAL_TOP", quality_passed, topk)
            preview_symbols = [_normalize_ticker(item.get("ticker")) for item in quality_passed]
            formal_symbols = [_normalize_ticker(item.get("ticker")) for item in topk]
            tracker.set_formal_candidates(formal_symbols)

        if not topk:
            topk = [dict(item) for item in (_pre_quality_pool if quality_fallback_active else (filtered_candidates or _pre_quality_pool))]
            selection_stage = "fast_preliminary"

        # Compute top10 for downstream consumers
        _pool_for_top10 = _pre_quality_pool if quality_fallback_active else (filtered_candidates or _pre_quality_pool)
        top10 = [dict(item) for item in (_pool_for_top10[: max(self.selection_size, self._filter_candidate_limit_from_env())])]
        funnel_summary = tracker.to_dict()
        try:
            tracker.write_report()
        except Exception:
            pass

        # ── Consistency report ──────────────────────────────────────────
        tracker.print_consistency_report()
        tracker.print_diagnostic_report()
        try:
            tracker.write_debug_artifact()
        except Exception:
            pass

        # write configs for selected TopK
        if write_configs:
            from src.openalpha.config_writer import write_top_configs
            write_top_configs(topk)

        report_rows = self._format_report_rows(topk)
        # Determine candidate type based on mode
        _candidate_type = "LIVE_TRADABLE" if quality_mode_is_strict and not quality_fallback_active else "RESEARCH_ONLY"
        # Mark all top candidates with their type
        for item in topk:
            if "candidate_type" not in item:
                item["candidate_type"] = _candidate_type
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
                "run_mode": _run_mode,
            },
            "quality_filter_report": filter_report,
            "selection_run_id": selection_run_id,
            "selection_funnel": funnel_summary,
            "rejection_reason_counts": funnel_summary.get("rejection_reason_counts", {}),
            "nearest_rejected_candidates": funnel_summary.get("nearest_rejected_candidates", []),
            "universe_source": "managed" if (source == "managed" and _load_managed_universe()) else source,
            "universe_symbol_count": len(symbols),
            "preview_candidates": preview_symbols,
            "formal_candidates": formal_symbols,
            "quality_fallback_active": quality_fallback_active,
            "quality_fallback_reason": "QUALITY_GATE" if quality_fallback_active else "",
            "run_mode": _run_mode,
            "candidate_type": _candidate_type,
            "preflight": _preflight_report,
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
                "data_mode": row.get("data_mode") or row.get("data_source") or "",
                "data_freshness": row.get("data_freshness") or row.get("freshness_status") or "",
                "data_status": row.get("data_status") or row.get("freshness_status") or "",
                "scoring_eligible": row.get("scoring_eligible"),
                "scoring_block_reason": row.get("scoring_block_reason") or "",
                "missing_fields": list(row.get("missing_fields") or []),
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
