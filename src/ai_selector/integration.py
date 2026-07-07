from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import AISelectorRuntimeConfig, load_runtime_config
from .providers.finrobot_provider import FinRobotProvider
from .providers.openbb_provider import OpenBBProvider
from .providers.tradingagents_provider import TradingAgentsProvider
from .scoring import combine_scores


logger = logging.getLogger(__name__)


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
            ta_result = self.tradingagents_provider.analyze(analyzed_universe)
            fr_result = self.finrobot_provider.analyze(analyzed_universe)
            ob_result = self.openbb_provider.analyze(analyzed_universe) if self.config.openbb_enabled else {}
            ranked = combine_scores(ta_result, fr_result, ob_result)
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

        self.last_top10 = ranked[:10]
        provider_fallback_used = any(
            bool(item.get("fallback"))
            for item in [*ta_result.values(), *fr_result.values(), *ob_result.values()]
        )
        self.last_run_metadata = {
            "providers_used": providers_used,
            "providers_disabled": providers_disabled,
            "fmp_enabled": bool(self.config.fmp_enabled),
            "provider_fallback_used": provider_fallback_used,
            "fallback_used": provider_fallback_used,
            "analysis_universe": analyzed_universe,
            "analysis_universe_limit": analysis_limit,
        }
        self._write_report(self.last_top10)
        top_n = min(max(1, self.config.top_n), len(self.last_top10))
        return self.last_top10[:top_n]

    def _write_report(self, ranked: list[dict]) -> None:
        top3 = ranked[: min(3, len(ranked))]
        payload = {
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "selection_date": datetime.utcnow().date().isoformat(),
            "providers_used": list(self.last_run_metadata.get("providers_used") or []),
            "providers_disabled": list(self.last_run_metadata.get("providers_disabled") or []),
            "fmp_enabled": bool(self.last_run_metadata.get("fmp_enabled", False)),
            "fallback_used": bool(self.last_run_metadata.get("fallback_used", False)),
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
