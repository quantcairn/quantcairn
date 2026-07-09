from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import AISelectorRuntimeConfig, load_runtime_config
from .composition_filter import CompositionFilter
from .providers.finrobot_provider import FinRobotProvider
from .providers.openbb_provider import OpenBBProvider
from .providers.tradingagents_provider import TradingAgentsProvider
from .range_score import RangeFitnessScorer
from .trade_filter import TradeEligibilityFilter
from .scoring import combine_scores
from .settings import load_runtime_settings, resolve_price_band


logger = logging.getLogger(__name__)


def _coalesce_float(*values: object, default: float = 50.0) -> float:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return float(default)


class AISelector:
    def __init__(
        self,
        config: Optional[AISelectorRuntimeConfig] = None,
        tradingagents_provider: Optional[TradingAgentsProvider] = None,
        finrobot_provider: Optional[FinRobotProvider] = None,
        openbb_provider: Optional[OpenBBProvider] = None,
    ) -> None:
        self.config = config or load_runtime_config()
        self.tradingagents_provider = tradingagents_provider or TradingAgentsProvider(self.config)
        self.finrobot_provider = finrobot_provider or FinRobotProvider(self.config)
        self.openbb_provider = openbb_provider or OpenBBProvider(self.config)
        self.range_scorer = RangeFitnessScorer()
        self.trade_filter = TradeEligibilityFilter()
        self.composition_filter = CompositionFilter()
        self.last_top10: list[dict] = []
        self.last_run_metadata: dict[str, object] = {}

    def get_signals(self) -> list:
        if not self.config.enabled:
            logger.info("AI selector enabled false")
            self.last_top10 = []
            return []

        universe = [str(item or "").strip().upper() for item in self.config.universe if str(item or "").strip()]
        if not universe:
            logger.warning("AI selector enabled but universe is empty")
            self.last_top10 = []
            return []
        analysis_limit = max(1, int(getattr(self.config, "analysis_universe_limit", 1) or 1))
        analyzed_universe = universe[:analysis_limit]

        try:
            providers_used = ["tradingagents", "finrobot"]
            providers_disabled: list[str] = []
            if self.config.openbb_enabled:
                providers_used.append("openbb")
            else:
                providers_disabled.append("openbb")
            if self.config.fmp_enabled:
                providers_used.append("fmp")
            else:
                providers_disabled.append("fmp")
                logger.warning("FMP disabled: missing FMP_API_KEY or SOXS_FMP_ENABLED=0")
            ta_result = self._safe_analyze(self.tradingagents_provider, analyzed_universe, "tradingagents")
            fr_result = self._safe_analyze(self.finrobot_provider, analyzed_universe, "finrobot")
            ob_result = (
                self._safe_analyze(self.openbb_provider, analyzed_universe, "openbb")
                if self.config.openbb_enabled
                else {}
            )
            ranked = combine_scores(ta_result, fr_result, ob_result)
            provider_fallback_used = any(
                bool(item.get("fallback"))
                for item in [*ta_result.values(), *fr_result.values(), *ob_result.values()]
            )
            if not ranked:
                provider_fallback_used = True
                ranked = [
                    {
                        "ticker": ticker,
                        "score": 50.0,
                        "ai_score": 50.0,
                        "confidence": 0.5,
                        "reason": "Neutral AI fallback",
                        "source": "ai_selector_fallback",
                        "fallback": True,
                    }
                    for ticker in analyzed_universe
                ]
        except Exception as exc:
            logger.exception("AI selector failed, fallback to original config: %s", exc)
            self.last_top10 = []
            self.last_run_metadata = {
                "providers_used": [],
                "providers_disabled": ["tradingagents", "finrobot", "openbb", "fmp"],
                "fmp_enabled": False,
                "provider_fallback_used": True,
                "fallback_used": True,
            }
            return []

        ranked = self._apply_range_scores(ranked)
        ranked = self._apply_trade_filter(ranked)
        ranked = self._apply_composition_filter(ranked)
        self.last_top10 = ranked[:10]
        self.last_run_metadata = {
            "providers_used": providers_used,
            "providers_disabled": providers_disabled,
            "fmp_enabled": bool(self.config.fmp_enabled),
            "provider_fallback_used": provider_fallback_used,
            "fallback_used": provider_fallback_used,
            "range_score_enabled": True,
            "entry_proximity_enabled": bool(getattr(self.config, "entry_proximity_enabled", True)),
            "entry_proximity_weight": float(getattr(self.config, "entry_proximity_weight", 0.0) or 0.0),
            "trade_filter_enabled": True,
            "trade_filter_fallback_used": bool(self.last_run_metadata.get("trade_filter_fallback_used", False)),
            "composition_filter_enabled": True,
            "composition_filter_max_leveraged_etf_in_top3": 1,
            "composition_filter_rejected": list(self.last_run_metadata.get("composition_filter_rejected") or []),
            "composition_filter_warnings": list(self.last_run_metadata.get("composition_filter_warnings") or []),
            "analysis_universe": analyzed_universe,
            "analysis_universe_limit": analysis_limit,
        }
        self._write_report(self.last_top10)
        top_n = min(max(1, self.config.top_n), len(self.last_top10))
        return self.last_top10[:top_n]

    def _safe_analyze(self, provider, tickers: list[str], label: str) -> dict:
        try:
            return dict(provider.analyze(tickers) or {})
        except Exception as exc:
            logger.warning("%s provider failed, falling back to neutral data: %s", label, exc)
            return {}

    def _apply_range_scores(self, ranked: list[dict]) -> list[dict]:
        scored: list[dict] = []
        entry_weight = max(0.0, min(1.0, float(getattr(self.config, "entry_proximity_weight", 0.0) or 0.0)))
        entry_enabled = bool(getattr(self.config, "entry_proximity_enabled", True))
        for item in ranked:
            candidate = dict(item)
            ticker = str(candidate.get("ticker") or "").strip().upper()
            market_data = self._market_data_snapshot(ticker)
            range_result = self.range_scorer.calculate(ticker, market_data)
            ai_score = _coalesce_float(candidate.get("ai_score"), candidate.get("score"), default=50.0)
            range_score = _coalesce_float(range_result.get("range_score"), default=50.0)
            final_score = round(0.6 * ai_score + 0.4 * range_score, 2)
            entry = dict(range_result.get("entry") or {})
            entry_score = _coalesce_float(entry.get("entry_proximity_score"), default=50.0)
            if entry_enabled and entry_weight > 0.0:
                final_score = round(final_score * (1.0 - entry_weight) + entry_score * entry_weight, 2)
            candidate.update(range_result)
            candidate["ai_score"] = round(ai_score, 2)
            candidate["final_score"] = final_score
            candidate["score"] = final_score
            candidate["entry"] = entry
            candidate["trade_market_data"] = market_data
            scored.append(candidate)
        scored.sort(key=lambda item: (-float(item.get("final_score") or 0.0), item.get("ticker") or ""))
        return scored

    def _apply_trade_filter(self, ranked: list[dict]) -> list[dict]:
        market_data = {
            str(item.get("ticker") or "").strip().upper(): dict(item.get("trade_market_data") or {})
            for item in ranked
            if str(item.get("ticker") or "").strip()
        }
        result = self.trade_filter.filter(ranked, market_data)
        accepted = [dict(item) for item in (result.get("accepted") or [])]
        accepted_tickers = {str(item.get("ticker") or "").strip().upper() for item in accepted}
        fallback_used = bool(result.get("fallback_used", False))

        if len(accepted) < 3:
            fallback_pool = [
                dict(item)
                for item in ranked
                if str(item.get("ticker") or "").strip().upper() not in accepted_tickers
            ]
            fallback_pool.sort(
                key=lambda item: (
                    -float(item.get("final_score") or item.get("score") or 0.0),
                    item.get("ticker") or "",
                )
            )
            for item in fallback_pool:
                if len(accepted) >= 3:
                    break
                item["trade_filter_passed"] = False
                item["reject_reason"] = item.get("reject_reason") or "fallback_pool"
                item["fallback_used"] = True
                accepted.append(item)
                fallback_used = True

        for item in accepted:
            if "trade_filter_passed" not in item:
                item["trade_filter_passed"] = False
            if "reject_reason" not in item:
                item["reject_reason"] = ""
            if "fallback_used" not in item:
                item["fallback_used"] = False

        accepted.sort(
            key=lambda item: (
                -float(item.get("final_score") or item.get("score") or 0.0),
                item.get("ticker") or "",
            )
        )
        self.last_run_metadata["trade_filter_fallback_used"] = fallback_used
        self.last_run_metadata["trade_filter_accepted"] = [str(item.get("ticker") or "").strip().upper() for item in accepted]
        return accepted

    def _apply_composition_filter(self, ranked: list[dict]) -> list[dict]:
        result = self.composition_filter.filter_top_n(ranked, top_n=self.config.top_n)
        accepted = [dict(item) for item in (result.get("accepted") or [])]
        rejected = [dict(item) for item in (result.get("rejected") or [])]
        warnings = list(result.get("warnings") or [])
        self.last_run_metadata["composition_filter_rejected"] = rejected
        self.last_run_metadata["composition_filter_warnings"] = warnings
        self.last_run_metadata["composition_filter_passed"] = len(rejected) == 0
        self.last_run_metadata["composition_filter_accepted"] = [
            str(item.get("ticker") or "").strip().upper() for item in accepted
        ]
        return accepted

    def _market_data_snapshot(self, ticker: str) -> dict:
        if not ticker:
            return {}
        try:
            from src.data.fetcher import PriceFetcher

            fetcher = PriceFetcher(ticker, poll_interval=0)
            quote = fetcher.get_quote()
            ohlcv = fetcher.get_ohlcv(period="1mo", interval="1d")
        except Exception as exc:
            logger.debug("range market snapshot failed for %s: %s", ticker, exc)
            return {}

        closes = [float(getattr(item, "close", 0.0) or 0.0) for item in ohlcv or [] if float(getattr(item, "close", 0.0) or 0.0) > 0]
        volumes = [float(getattr(item, "volume", 0.0) or 0.0) for item in ohlcv or [] if float(getattr(item, "volume", 0.0) or 0.0) > 0]
        current_price = float(getattr(quote, "price", 0.0) or 0.0) if quote is not None else 0.0
        if current_price <= 0 and closes:
            current_price = closes[-1]
        spread_pct = None
        bid = float(getattr(quote, "bid", 0.0) or 0.0) if quote is not None else 0.0
        ask = float(getattr(quote, "ask", 0.0) or 0.0) if quote is not None else 0.0
        if current_price > 0 and bid > 0 and ask > 0 and ask >= bid:
            spread_pct = ((ask - bid) / current_price) * 100.0
        return {
            "current_price": current_price or None,
            "avg_10d_volume": (sum(volumes[-10:]) / len(volumes[-10:])) if volumes[-10:] else None,
            "spread_pct": spread_pct,
            "bid": bid or None,
            "ask": ask or None,
            "close_history": closes,
            "returns": [
                ((closes[i] - closes[i - 1]) / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ],
            "recent_low": min(closes[-10:]) if closes else None,
            "recent_high": max(closes[-10:]) if closes else None,
            "three_day_change_pct": (
                ((closes[-1] - closes[-4]) / closes[-4]) * 100.0
                if len(closes) >= 4 and closes[-4] > 0
                else None
            ),
            "price": current_price or None,
        }

    def _write_report(self, ranked: list[dict]) -> None:
        top3 = ranked[: min(3, len(ranked))]
        now_utc = datetime.now(timezone.utc)
        payload = {
            "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "selection_date": now_utc.date().isoformat(),
            "providers_used": list(self.last_run_metadata.get("providers_used") or []),
            "providers_disabled": list(self.last_run_metadata.get("providers_disabled") or []),
            "fmp_enabled": bool(self.last_run_metadata.get("fmp_enabled", False)),
            "fallback_used": bool(self.last_run_metadata.get("fallback_used", False)),
            "settings": {
                "min_price": float(resolve_price_band(load_runtime_settings())[0]),
                "max_price": float(resolve_price_band(load_runtime_settings())[1]),
                "price_band": {
                    "min": float(resolve_price_band(load_runtime_settings())[0]),
                    "max": float(resolve_price_band(load_runtime_settings())[1]),
                },
                "range_score_enabled": bool(self.last_run_metadata.get("range_score_enabled", True)),
                "entry_proximity_enabled": bool(self.last_run_metadata.get("entry_proximity_enabled", True)),
                "entry_proximity_weight": float(self.last_run_metadata.get("entry_proximity_weight", 0.0) or 0.0),
                "trade_filter_enabled": bool(self.last_run_metadata.get("trade_filter_enabled", True)),
                "composition_filter_enabled": bool(self.last_run_metadata.get("composition_filter_enabled", True)),
            },
            "composition_filter": {
                "max_leveraged_etf_in_top3": int(
                    self.last_run_metadata.get("composition_filter_max_leveraged_etf_in_top3") or 1
                ),
                "rejected": list(self.last_run_metadata.get("composition_filter_rejected") or []),
                "warnings": list(self.last_run_metadata.get("composition_filter_warnings") or []),
            },
            "analysis_universe": list(self.last_run_metadata.get("analysis_universe") or []),
            "analysis_universe_limit": int(self.last_run_metadata.get("analysis_universe_limit") or self.config.analysis_universe_limit),
            "top10": ranked,
            "top3": top3,
            "universe": list(self.config.universe),
            "top_n": self.config.top_n,
        }
        path = Path(self.config.top10_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_top10(self) -> list[dict]:
        if not self.last_top10:
            self.get_signals()
        return list(self.last_top10)
