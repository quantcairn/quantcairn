import math
import os
from typing import Dict, List, Sequence

import numpy as np

from src.universe.universe import Universe
from src.scoring.scorer import Scorer
from src.news_agent.news_collector import NewsCollector
from src.ai_selector.settings import load_runtime_settings


class AIStrategySelector:
    def __init__(self, config=None):
        self.universe = Universe()
        self.news = NewsCollector()
        self.scorer = Scorer()
        self.selection_size = self._selection_size_from_env()
        self.max_symbols = self._max_symbols_from_env()

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

    def run_selection(self):
        # 1. build universe
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
        data_mode = "live" if live_requested else "fallback"
        fallback_used = False

        if live_requested and len(scored) < self.selection_size:
            fallback_scored = self._score_with_live_flag(symbols, news_map, live_enabled=False)
            if fallback_scored:
                fallback_used = True
                existing = {item.get("ticker") for item in scored}
                scored.extend(item for item in fallback_scored if item.get("ticker") not in existing)
                scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
                data_mode = "mixed" if existing else "fallback"

        # 4. sort by base score, then diversify TopK by sector/correlation
        scored_sorted = sorted(scored, key=lambda x: x.get("score", 0.0), reverse=True)
        top10 = scored_sorted[:10]
        topk = self._select_diversified_top_k(top10, self.selection_size)

        # write configs for selected TopK
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
            },
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
            out.append({
                "rank": idx,
                "ticker": row.get("ticker"),
                "score": float(round(row.get("score", 0.0), 2)),
                "volatility": float(round(row.get("volatility_score", 0.0), 2)),
                "volume": float(round(row.get("volume_score", 0.0), 2)),
                "trend_fit": float(round(row.get("trend_fit_score", 0.0), 2)),
                "repeatability": float(round(row.get("repeatability_score", 0.0), 2)),
                "drawdown": float(round(row.get("drawdown_safety_score", 0.0), 2)),
                "correlation_penalty": float(round(row.get("correlation_penalty", 0.0), 2)),
                "diversity_bonus": float(round(row.get("diversity_bonus", 0.0), 2)),
                "suggested_range": row.get("suggested_range"),
                "sector": row.get("sector"),
            })
        return out
