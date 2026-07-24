from __future__ import annotations

from src.openalpha.providers.finrobot_provider import FinRobotProvider
from src.openalpha.providers.openbb_provider import OpenBBProvider
from src.openalpha.providers.tradingagents_provider import TradingAgentsProvider


def test_tradingagents_mock_result_is_neutral():
    provider = TradingAgentsProvider()
    payload = provider._mock_result("NVDA", reason="disabled")

    assert payload["technical_score"] == 50.0
    assert payload["news_score"] == 50.0
    assert payload["sentiment_score"] == 50.0
    assert payload["risk_score"] == 50.0


def test_finrobot_mock_result_is_neutral():
    provider = FinRobotProvider()
    payload = provider._mock_result("NVDA", reason="disabled")

    assert payload["fundamental_score"] == 50.0
    assert payload["valuation_score"] == 50.0
    assert payload["earnings_score"] == 50.0
    assert payload["risk_score"] == 50.0


def test_openbb_fallback_result_is_neutral():
    provider = OpenBBProvider()
    payload = provider._fallback_result("NVDA", "disabled")

    assert payload["fundamental_score"] == 50.0
    assert payload["valuation_score"] == 50.0
    assert payload["growth_score"] == 50.0
    assert payload["profitability_score"] == 50.0
    assert payload["risk_score"] == 50.0
