from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.ai_selector import selection_state
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


class StubOpenBBProvider:
    def analyze(self, tickers: list) -> dict:
        scores = {
            "NVDA": (91, 88, 90, 86, 84),
            "MSFT": (82, 76, 80, 79, 81),
            "AAPL": (70, 68, 69, 71, 73),
            "PLTR": (55, 52, 56, 54, 58),
        }
        result = {}
        for ticker in tickers:
            fundamental, valuation, growth, profitability, risk = scores.get(ticker, (50, 50, 50, 50, 50))
            result[ticker] = {
                "fundamental_score": fundamental,
                "valuation_score": valuation,
                "growth_score": growth,
                "profitability_score": profitability,
                "risk_score": risk,
                "confidence": 0.65,
                "reason": f"ob:{ticker}",
                "source": "openbb",
                "fallback": False,
            }
        return result


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = []

    def setattr(self, obj, name, value):
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def setenv(self, name, value):
        original = os.environ.get(name)
        self._originals.append(("env", name, original))
        os.environ[name] = value

    def restore(self):
        for obj, name, original in reversed(self._originals):
            if obj == "env":
                if original is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original
            else:
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
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
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


def test_ai_selector_with_openbb_enabled_enhances_scores():
    with tempfile.TemporaryDirectory() as tmpdir:
        top10_path = Path(tmpdir) / "latest_top10.json"
        selector = AISelector(
            config=AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["NVDA", "MSFT", "AAPL", "PLTR"],
                top10_path=top10_path,
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
                openbb_enabled=True,
            ),
            tradingagents_provider=StubTradingAgentsProvider(),
            finrobot_provider=StubFinRobotProvider(),
            openbb_provider=StubOpenBBProvider(),
        )

        signals = selector.get_signals()

        assert [item["ticker"] for item in signals] == ["NVDA", "MSFT", "AAPL"]
        assert "openbb" in signals[0]["source"]
        assert "ob:NVDA" in signals[0]["reason"]
        payload = json.loads(top10_path.read_text(encoding="utf-8"))
        assert payload["top10"][0]["ticker"] == "NVDA"
        assert payload["top10"][0]["score"] >= payload["top10"][1]["score"]


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
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
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


def test_engine_reuses_cached_daily_ai_selection():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            configs_dir = root / "configs"
            configs_dir.mkdir(parents=True, exist_ok=True)
            for idx in range(1, 4):
                payload = {
                    "enabled": False,
                    "slot": idx,
                    "reason": "top_n_not_filled",
                    "selection_run_id": "run-1",
                    "top_sync_run_id": "run-1",
                    "selection_date": "2026-07-06",
                    "generated_at": "2026-07-06T08:30:00-04:00",
                    "result_quality": "COMPLETE",
                    "research_admission": "RESEARCH_READY",
                    "mode": "paper",
                }
                if idx == 1:
                    payload.update({"enabled": True, "ticker": "SOFI", "reason": "selected"})
                (configs_dir / f"TOP{idx}.yaml").write_text(
                    yaml.safe_dump(payload, sort_keys=False),
                    encoding="utf-8",
                )
            monkeypatch.setenv("SOXS_STATE_DIR", str(root / "state"))
            monkeypatch.setattr(selection_state, "PROJECT_DIR", root)
            report_path = root / "ai_selection_latest.json"
            report_path.write_text(
                json.dumps(
                    {
                        "selection_date": "2026-07-06",
                        "selection_stage": "FINALIZED",
                        "result_quality": "COMPLETE",
                        "research_admission": "RESEARCH_READY",
                        "top3": [
                            {"ticker": "SOFI", "score": 0.0, "reason": "cached_sofi", "confidence": 0.5},
                            {"ticker": "NVDA", "score": 80.0, "reason": "cached_nvda", "confidence": 0.8},
                            {"ticker": "AAPL", "score": 78.0, "reason": "cached_aapl", "confidence": 0.78},
                        ],
                        "top10": [
                            {"ticker": "SOFI", "score": 0.0, "reason": "cached_sofi", "confidence": 0.5},
                            {"ticker": "NVDA", "score": 80.0, "reason": "cached_nvda", "confidence": 0.8},
                            {"ticker": "AAPL", "score": 78.0, "reason": "cached_aapl", "confidence": 0.78},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            selection_state.write_selection_state(
                et_date="2026-07-06",
                generated_at="2026-07-06T08:30:00-04:00",
                selected_symbols=["SOFI"],
                report_path=str(report_path),
                selection_stage="FINALIZED",
                result_quality="COMPLETE",
                research_admission="RESEARCH_READY",
            )
            monkeypatch.setattr(
                engine_module,
                "load_ai_selector_runtime_config",
                lambda: AISelectorRuntimeConfig(
                    enabled=True,
                    top_n=3,
                    universe=["SOFI", "NVDA", "AAPL"],
                    top10_path=root / "unused_top10.json",
                    tradingagents_path="",
                    tradingagents_python="python3",
                    tradingagents_analysis_date=None,
                    finrobot_path="",
                    finrobot_python="python3",
                    finrobot_config_file="",
                    finrobot_output_dir="",
                ),
            )
            class FakeDateTime:
                @classmethod
                def now(cls, tz=None):
                    from datetime import datetime
                    return datetime(2026, 7, 6, 10, 0, 0)

                @classmethod
                def utcnow(cls):
                    from datetime import datetime
                    return datetime(2026, 7, 6, 14, 0, 0)

            monkeypatch.setattr(engine_module, "datetime", FakeDateTime)

            class RaisingAISelector:
                def __init__(self, *args, **kwargs):
                    raise AssertionError("cached selection should skip provider execution")

            monkeypatch.setattr(engine_module, "AISelector", RaisingAISelector)

            engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
            engine._initialize_ai_selector()

            assert engine._ai_selection.active is True
            assert [item["ticker"] for item in engine._ai_selection.top3] == ["SOFI", "NVDA", "AAPL"]
            assert engine._ai_selection.signal_for_ticker["ticker"] == "SOFI"
    finally:
        monkeypatch.restore()


def run_test_direct():
    test_ai_selector_returns_top3_and_writes_top10_report()
    test_ai_selector_with_openbb_enabled_enhances_scores()
    test_ai_selector_disabled_does_not_change_engine_behavior()
    test_engine_reuses_cached_daily_ai_selection()
