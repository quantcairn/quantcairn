from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

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


def run_test_direct():
    test_tradingagents_provider_fallback_does_not_crash()
    test_finrobot_provider_fallback_does_not_crash()
