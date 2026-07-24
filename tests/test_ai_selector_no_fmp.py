from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.openalpha.config import AISelectorRuntimeConfig
from src.openalpha.integration import AISelector


class NeutralProvider:
    def __init__(self, source: str, payload_keys: tuple[str, ...]) -> None:
        self.source = source
        self.payload_keys = payload_keys

    def analyze(self, tickers: list) -> dict:
        result = {}
        for ticker in tickers:
            row = {
                "ticker": ticker,
                "confidence": 0.5,
                "reason": f"{self.source}:{ticker}",
                "source": self.source,
                "fallback": True,
            }
            for key in self.payload_keys:
                row[key] = 50.0
            result[ticker] = row
        return result


def test_ai_selector_runs_without_fmp_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "latest_top10.json"
        selector = AISelector(
            config=AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["NVDA", "MSFT", "AAPL"],
                top10_path=report_path,
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
                openbb_enabled=False,
                fmp_enabled=False,
                fmp_api_key="",
            ),
            tradingagents_provider=NeutralProvider(
                "tradingagents_mock",
                ("technical_score", "news_score", "sentiment_score", "risk_score"),
            ),
            finrobot_provider=NeutralProvider(
                "finrobot_mock",
                ("fundamental_score", "valuation_score", "earnings_score", "risk_score"),
            ),
        )

        signals = selector.get_signals()

        assert len(signals) == 3
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["fmp_enabled"] is False
        assert "fmp" in payload["providers_disabled"]
