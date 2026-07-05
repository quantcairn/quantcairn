from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.ai_selector.config import AISelectorRuntimeConfig
from src.ai_selector.integration import AISelector
from src.config.loader import AppConfig
from src.engine import trading_engine as engine_module
from src.engine.trading_engine import TradingEngine


class StubTradingAgentsProvider:
    def analyze(self, tickers: list) -> dict:
        scores = {
            "NVDA": (92, 86, 84, 80),
            "MSFT": (88, 82, 80, 78),
            "AAPL": (85, 79, 78, 77),
            "PLTR": (82, 70, 74, 68),
        }
        result = {}
        for ticker in tickers:
            technical, news, sentiment, risk = scores.get(ticker, (60, 60, 60, 60))
            result[ticker] = {
                "technical_score": technical,
                "news_score": news,
                "sentiment_score": sentiment,
                "risk_score": risk,
                "confidence": 0.8,
                "reason": f"ta:{ticker}",
                "source": "tradingagents",
            }
        return result


class StubFinRobotProvider:
    def analyze(self, tickers: list) -> dict:
        scores = {
            "NVDA": (82, 80, 84, 79),
            "MSFT": (78, 79, 77, 80),
            "AAPL": (76, 75, 78, 79),
            "PLTR": (68, 67, 69, 66),
        }
        result = {}
        for ticker in tickers:
            fundamental, valuation, earnings, risk = scores.get(ticker, (60, 60, 60, 60))
            result[ticker] = {
                "fundamental_score": fundamental,
                "valuation_score": valuation,
                "earnings_score": earnings,
                "risk_score": risk,
                "confidence": 0.75,
                "reason": f"fr:{ticker}",
                "source": "finrobot",
            }
        return result


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = []

    def setattr(self, obj, name, value):
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, original in reversed(self._originals):
            setattr(obj, name, original)


def test_ai_selector_returns_top3_and_writes_top10_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        top10_path = Path(tmpdir) / "latest_top10.json"
        selector = AISelector(
            config=AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["NVDA", "MSFT", "AAPL", "PLTR"],
                top10_path=top10_path,
            ),
            tradingagents_provider=StubTradingAgentsProvider(),
            finrobot_provider=StubFinRobotProvider(),
        )

        signals = selector.get_signals()

        assert [item["ticker"] for item in signals] == ["NVDA", "MSFT", "AAPL"]
        assert top10_path.exists()
        payload = json.loads(top10_path.read_text(encoding="utf-8"))
        assert [item["ticker"] for item in payload["top10"][:3]] == ["NVDA", "MSFT", "AAPL"]
        assert len(payload["top3"]) == 3


def test_ai_selector_disabled_does_not_change_engine_behavior():
    monkeypatch = SimpleMonkeyPatch()
    try:
        engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
        monkeypatch.setattr(
            engine_module,
            "load_ai_selector_runtime_config",
            lambda: AISelectorRuntimeConfig(
                enabled=False,
                top_n=3,
                universe=["NVDA", "MSFT", "AAPL"],
                top10_path=Path(tempfile.gettempdir()) / "unused_top10.json",
            ),
        )

        class RaisingAISelector:
            def __init__(self, *args, **kwargs):
                raise AssertionError("AISelector should not be constructed when disabled")

        monkeypatch.setattr(engine_module, "AISelector", RaisingAISelector)

        engine._initialize_ai_selector()

        assert engine._ai_selection.enabled is False
        assert engine._ai_entry_allowed() is True
    finally:
        monkeypatch.restore()


def run_test_direct():
    test_ai_selector_returns_top3_and_writes_top10_report()
    test_ai_selector_disabled_does_not_change_engine_behavior()
