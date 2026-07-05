from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.ai_selector.config import AISelectorRuntimeConfig
from src.ai_selector.providers.finrobot_provider import FinRobotProvider
from src.ai_selector.providers.tradingagents_provider import TradingAgentsProvider


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = []

    def setattr(self, obj, name, value):
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, original in reversed(self._originals):
            setattr(obj, name, original)


def test_tradingagents_provider_fallback_does_not_crash():
    monkeypatch = SimpleMonkeyPatch()
    try:
        provider = TradingAgentsProvider()
        monkeypatch.setattr(provider, "_is_available", lambda: False)

        result = provider.analyze(["NVDA"])

        assert "NVDA" in result
        assert result["NVDA"]["fallback"] is True
        assert "technical_score" in result["NVDA"]
    finally:
        monkeypatch.restore()


def test_finrobot_provider_fallback_does_not_crash():
    monkeypatch = SimpleMonkeyPatch()
    try:
        provider = FinRobotProvider()
        monkeypatch.setattr(provider, "_is_available", lambda: False)

        result = provider.analyze(["MSFT"])

        assert "MSFT" in result
        assert result["MSFT"]["fallback"] is True
        assert "fundamental_score" in result["MSFT"]
    finally:
        monkeypatch.restore()


def test_tradingagents_provider_detects_local_source_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / "tradingagents").mkdir()
        provider = TradingAgentsProvider(
            config=AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["NVDA"],
                top10_path=repo / "latest_top10.json",
                tradingagents_path=str(repo),
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
            )
        )

        assert provider._resolve_source_path() == repo
        assert provider._is_available() is True


def test_finrobot_provider_parses_research_artifacts():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        analysis_csv = root / "financial_metrics_and_forecasts.csv"
        analysis_csv.write_text(
            "metric,value\nRevenue Growth,28\nGross Margin,74\nEPS Growth,18\n",
            encoding="utf-8",
        )
        ratios_csv = root / "ratios_raw_data.csv"
        ratios_csv.write_text(
            "metric,value\nDCF Upside,15\nNet Debt,-2\nEV/EBITDA,22\n",
            encoding="utf-8",
        )
        risks_file = root / "risks.txt"
        risks_file.write_text(
            "Risk remains manageable with strong balance sheet and resilient demand.",
            encoding="utf-8",
        )
        provider = FinRobotProvider()

        result = provider._normalize_result(
            "NVDA",
            {
                "analysis_csv": str(analysis_csv),
                "ratios_csv": str(ratios_csv),
                "risks_file": str(risks_file),
            },
        )

        assert result["fallback"] is False
        assert result["fundamental_score"] > 61.0
        assert result["valuation_score"] > 59.0
        assert result["risk_score"] >= 59.0


def run_test_direct():
    test_tradingagents_provider_fallback_does_not_crash()
    test_finrobot_provider_fallback_does_not_crash()
    test_tradingagents_provider_detects_local_source_path()
    test_finrobot_provider_parses_research_artifacts()
