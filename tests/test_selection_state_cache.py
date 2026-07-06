from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.ai_selector.config import AISelectorRuntimeConfig
from src.config.loader import AppConfig, PositionConfig
from src.engine import trading_engine as engine_module
from src.engine.trading_engine import TradingEngine


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = []

    def setattr(self, obj, name, value):
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, original in reversed(self._originals):
            setattr(obj, name, original)


def test_trading_engine_uses_fresh_selection_state_cache():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = root / "ai_selection_latest.json"
            report_path.write_text(
                json.dumps(
                    {
                        "selection_date": "2026-07-06",
                        "top3": [{"ticker": "SOFI", "score": 0.0, "confidence": 0.5, "reason": "protected"}],
                        "top10": [{"ticker": "SOFI", "score": 0.0, "confidence": 0.5, "reason": "protected"}],
                    }
                ),
                encoding="utf-8",
            )
            monkeypatch.setattr(
                engine_module,
                "load_ai_selector_runtime_config",
                lambda: AISelectorRuntimeConfig(
                    enabled=True,
                    top_n=3,
                    universe=["SOFI"],
                    top10_path=root / "unused.json",
                    tradingagents_path="",
                    tradingagents_python="python3",
                    tradingagents_analysis_date=None,
                    finrobot_path="",
                    finrobot_python="python3",
                    finrobot_config_file="",
                    finrobot_output_dir="",
                ),
            )
            monkeypatch.setattr(
                engine_module,
                "load_selection_state",
                lambda: {"et_date": "2026-07-06", "report_path": str(report_path)},
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
            engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
            engine._initialize_ai_selector()

            assert engine._ai_selection.active is True
            assert engine._ai_selection.signal_for_ticker["ticker"] == "SOFI"
    finally:
        monkeypatch.restore()


def test_trading_engine_stale_selection_state_falls_back_without_ai_run():
    monkeypatch = SimpleMonkeyPatch()
    try:
        monkeypatch.setattr(
            engine_module,
            "load_ai_selector_runtime_config",
            lambda: AISelectorRuntimeConfig(
                enabled=True,
                top_n=3,
                universe=["SOFI"],
                top10_path=Path(tempfile.gettempdir()) / "unused.json",
                tradingagents_path="",
                tradingagents_python="python3",
                tradingagents_analysis_date=None,
                finrobot_path="",
                finrobot_python="python3",
                finrobot_config_file="",
                finrobot_output_dir="",
            ),
        )
        monkeypatch.setattr(
            engine_module,
            "load_selection_state",
            lambda: {"et_date": "2026-07-05", "report_path": "/tmp/missing.json"},
        )

        class RaisingAISelector:
            def __init__(self, *args, **kwargs):
                raise AssertionError("stale selection should not invoke AISelector")

        monkeypatch.setattr(engine_module, "AISelector", RaisingAISelector)

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
        engine = TradingEngine(AppConfig(ticker="SOFI"), ignore_trading_hours=True)
        engine._initialize_ai_selector()

        assert engine._ai_selection.active is False
        assert engine._ai_selection.fallback_reason == "ai_selection_stale"
    finally:
        monkeypatch.restore()


def test_live_top_engine_blocks_when_selection_state_invalid():
    monkeypatch = SimpleMonkeyPatch()
    try:
        monkeypatch.setattr(engine_module, "has_live_top_configs", lambda: True)
        monkeypatch.setattr(engine_module, "current_top_config_symbols", lambda: ["SOFI"])
        monkeypatch.setattr(
            engine_module,
            "verify_live_startup_selection",
            lambda required_et_date=None: (False, "selection_state_date_mismatch:2026-07-05", None),
        )
        engine = TradingEngine(
            AppConfig(
                ticker="SOFI",
                mode="live",
                position=PositionConfig(reduce_only=True),
            ),
            ignore_trading_hours=True,
        )

        assert engine._verify_live_startup_safety() is False
        assert "当天选股状态无效" in engine._last_signal_reason
    finally:
        monkeypatch.restore()


def test_orphan_monitor_bypasses_live_top_selection_guard():
    monkeypatch = SimpleMonkeyPatch()
    try:
        monkeypatch.setattr(engine_module, "has_live_top_configs", lambda: True)
        monkeypatch.setattr(engine_module, "current_top_config_symbols", lambda: ["SOFI"])
        monkeypatch.setattr(
            engine_module,
            "verify_live_startup_selection",
            lambda required_et_date=None: (False, "selection_state_date_mismatch:2026-07-05", None),
        )
        engine = TradingEngine(
            AppConfig(
                ticker="SOFI",
                mode="live",
                position=PositionConfig(reduce_only=True),
            ),
            ignore_trading_hours=True,
            startup_role="orphan_monitor",
        )

        assert engine._verify_live_startup_safety() is True
    finally:
        monkeypatch.restore()
