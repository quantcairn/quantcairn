from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import AISelectorRuntimeConfig, load_runtime_config
from .composition_filter import CompositionFilter
from .data_sufficiency import evaluate_data_sufficiency
from .providers.finrobot_provider import FinRobotProvider
from .providers.openbb_provider import OpenBBProvider
from .providers.tradingagents_provider import TradingAgentsProvider
from .market_context import build_candidate_market_snapshot
from .candidate_ranking import score_candidate
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
        self.last_provider_audit: dict[str, dict[str, object]] = {}
        self.last_provider_outputs: dict[str, dict[str, dict[str, object]]] = {}

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
            self.last_provider_audit = {}
            self.last_provider_outputs = {}
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
                "provider_audit": {},
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
            "provider_audit": dict(self.last_provider_audit),
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

    def _build_provider_audit(
        self,
        label: str,
        tickers: list[str],
        result: dict[str, dict[str, object]],
        *,
        elapsed_seconds: float,
        error: Exception | None = None,
    ) -> dict[str, object]:
        rows = [dict(value) for value in (result or {}).values() if isinstance(value, dict)]
        contributor_fields: set[str] = set()
        affected_candidates: set[str] = set()
        critical_market_data_fields = {
            "current_price",
            "open",
            "high",
            "low",
            "close",
            "ohlcv",
            "quote",
            "bid",
            "ask",
            "spread_pct",
            "current_session",
            "previous_completed_session",
            "daily_data_as_of",
            "benchmark_data_as_of",
            "benchmark_alignment_status",
            "market_cap",
            "average_dollar_volume_20d",
            "avg_dollar_volume_20d",
            "avg_10d_volume",
            "volume",
            "atr_20_percentage",
            "atr_pct",
            "gap_pct",
        }
        non_critical_factor_fields = {
            "score",
            "ai_score",
            "range_score",
            "final_score",
            "candidate_score",
            "confidence",
            "technical_score",
            "sentiment_score",
            "fundamental_score",
            "valuation_score",
            "earnings_score",
            "growth_score",
            "risk_score",
        }
        explanation_only_fields = {
            "reason",
            "summary",
            "commentary",
            "explanation",
            "narrative",
            "analysis",
            "notes",
        }
        for row in rows:
            ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
            if ticker:
                affected_candidates.add(ticker)
            for key, value in row.items():
                if key == "ticker" or value is None:
                    continue
                contributor_fields.add(str(key))
        timeout_hits = 0
        fallback_hits = 0
        mock_hits = 0
        success_hits = 0
        for row in rows:
            text = " ".join(
                str(row.get(key) or "")
                for key in ("reason", "source", "error_message", "error_code")
            ).lower()
            is_timeout = "timeout" in text
            is_mock = "mock" in text or str(row.get("source") or "").lower().endswith("_mock")
            is_fallback = bool(row.get("fallback")) or is_mock
            has_scores = any(
                row.get(key) is not None
                for key in (
                    "technical_score",
                    "news_score",
                    "sentiment_score",
                    "fundamental_score",
                    "risk_score",
                    "confidence",
                )
            )
            if is_timeout:
                timeout_hits += 1
            if is_fallback:
                fallback_hits += 1
            if is_mock:
                mock_hits += 1
            if has_scores and not is_fallback:
                success_hits += 1
        if contributor_fields & critical_market_data_fields:
            fallback_scope = "CRITICAL_MARKET_DATA"
            fallback_severity = "CRITICAL"
        elif contributor_fields & non_critical_factor_fields:
            fallback_scope = "NON_CRITICAL_FACTOR"
            fallback_severity = "DEGRADED"
        elif contributor_fields & explanation_only_fields:
            fallback_scope = "EXPLANATION_ONLY"
            fallback_severity = "INFO"
        elif timeout_hits:
            fallback_scope = "RUN_LEVEL"
            fallback_severity = "DEGRADED"
        else:
            fallback_scope = "EXPLANATION_ONLY"
            fallback_severity = "INFO"
        return {
            "provider_name": label,
            "attempted": len(tickers),
            "success": success_hits,
            "failure": max(0, len(tickers) - success_hits),
            "timed_out": timeout_hits,
            "empty_response": int(not rows),
            "fallback_used": fallback_hits,
            "mock_used": mock_hits,
            "elapsed_seconds": round(float(elapsed_seconds), 3),
            "error_code": type(error).__name__ if error is not None else "",
            "error_message": str(error) if error is not None else "",
            "contributed_fields": sorted(contributor_fields),
            "affected_candidates": sorted(affected_candidates),
            "affected_fields": sorted(contributor_fields),
            "fallback_scope": fallback_scope,
            "fallback_severity": fallback_severity,
            "contributor_count": len(contributor_fields),
        }

    def _safe_analyze(self, provider, tickers: list[str], label: str) -> dict:
        started = time.perf_counter()
        try:
            result = dict(provider.analyze(tickers) or {})
            self.last_provider_outputs[label] = result
            self.last_provider_audit[label] = self._build_provider_audit(
                label,
                list(tickers or []),
                result,
                elapsed_seconds=time.perf_counter() - started,
            )
            return result
        except Exception as exc:
            logger.warning("%s provider failed, falling back to neutral data: %s", label, exc)
            self.last_provider_outputs[label] = {}
            self.last_provider_audit[label] = self._build_provider_audit(
                label,
                list(tickers or []),
                {},
                elapsed_seconds=time.perf_counter() - started,
                error=exc,
            )
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
            entry = dict(range_result.get("entry") or {})
            candidate.update(range_result)
            candidate["trade_market_data"] = market_data
            candidate["market_data"] = dict(market_data or {})
            candidate["ai_score"] = round(ai_score, 2)
            candidate["entry"] = entry
            candidate.setdefault("base_score", round(float(candidate.get("score", candidate.get("final_score", ai_score))), 2))
            sufficiency = evaluate_data_sufficiency(
                candidate,
                strict_quote=False,
                strict_history=False,
                strict_benchmark=True,
            )
            candidate["data_mode"] = sufficiency.data_mode
            candidate["data_freshness"] = sufficiency.data_freshness
            candidate["data_status"] = sufficiency.data_status
            candidate["scoring_eligible"] = sufficiency.scoring_eligible
            candidate["scoring_block_reason"] = sufficiency.scoring_block_reason
            candidate["missing_fields"] = list(sufficiency.missing_fields)
            candidate["data_sufficiency"] = sufficiency.to_dict()
            candidate = score_candidate(candidate)
            final_score = float(candidate.get("candidate_score", candidate.get("score", candidate.get("final_score", 50.0))))
            entry_score = _coalesce_float(entry.get("entry_proximity_score"), default=50.0)
            if entry_enabled and entry_weight > 0.0:
                final_score = round(final_score * (1.0 - entry_weight) + entry_score * entry_weight, 2)
            candidate["candidate_score"] = final_score
            candidate["score"] = final_score
            candidate["final_score"] = final_score
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
            return build_candidate_market_snapshot(ticker)
        except Exception as exc:
            logger.debug("range market snapshot failed for %s: %s", ticker, exc)
            return {}

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
