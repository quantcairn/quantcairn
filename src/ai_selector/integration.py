from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import AISelectorRuntimeConfig, load_runtime_config
from .providers.finrobot_provider import FinRobotProvider
from .providers.tradingagents_provider import TradingAgentsProvider
from .scoring import combine_scores


logger = logging.getLogger(__name__)


class AISelector:
    def __init__(
        self,
        config: Optional[AISelectorRuntimeConfig] = None,
        tradingagents_provider: Optional[TradingAgentsProvider] = None,
        finrobot_provider: Optional[FinRobotProvider] = None,
    ) -> None:
        self.config = config or load_runtime_config()
        self.tradingagents_provider = tradingagents_provider or TradingAgentsProvider()
        self.finrobot_provider = finrobot_provider or FinRobotProvider()
        self.last_top10: list[dict] = []

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

        try:
            ta_result = self.tradingagents_provider.analyze(universe)
            fr_result = self.finrobot_provider.analyze(universe)
            ranked = combine_scores(ta_result, fr_result)
        except Exception as exc:
            logger.exception("AI selector failed, fallback to original config: %s", exc)
            self.last_top10 = []
            return []

        self.last_top10 = ranked[:10]
        self._write_report(self.last_top10)
        top_n = min(max(1, self.config.top_n), len(self.last_top10))
        return self.last_top10[:top_n]

    def _write_report(self, ranked: list[dict]) -> None:
        top3 = ranked[: min(3, len(ranked))]
        payload = {
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
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
