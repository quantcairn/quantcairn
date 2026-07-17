from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import yaml

from src.ai_selector import selection_state
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


def test_trading_engine_uses_fresh_selection_state_cache():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = root / "ai_selection_latest.json"
            report_path.write_text(
                json.dumps(
                    {
                        "selection_date": "2026-07-17",
                        "top3": [{"ticker": "SOFI", "score": 0.0, "confidence": 0.5, "reason": "protected"}],
                        "top10": [{"ticker": "SOFI", "score": 0.0, "confidence": 0.5, "reason": "protected"}],
                    }
                ),
                encoding="utf-8",
            )
            configs_dir = root / "configs"
            configs_dir.mkdir(parents=True, exist_ok=True)
            for idx in range(1, 4):
                payload = {
                    "enabled": False,
                    "slot": idx,
                    "reason": "top_n_not_filled",
                    "selection_run_id": "run-1",
                    "top_sync_run_id": "run-1",
                    "selection_date": "2026-07-17",
                    "generated_at": "2026-07-17T08:30:00-04:00",
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
            selection_state.write_selection_state(
                et_date="2026-07-17",
                generated_at="2026-07-17T08:30:00-04:00",
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
                "load_committed_selection_bundle",
                lambda project_dir: {
                    "bundle_root": root,
                    "state": {"et_date": "2026-07-17", "selected_symbols": ["SOFI"], "top_config_symbols": ["SOFI"]},
                },
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

            assert engine._ai_selection.active is False
            assert engine._ai_selection.selection_mode == "STALE"
            assert "selection_state_date_mismatch" in engine._ai_selection.fallback_reason
            assert engine._ai_selection.signal_for_ticker is None
    finally:
        monkeypatch.restore()


def test_trading_engine_stale_selection_state_falls_back_without_ai_run():
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
                    "selection_date": "2026-07-05",
                    "generated_at": "2026-07-05T08:30:00-04:00",
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
            selection_state.write_selection_state(
                et_date="2026-07-05",
                generated_at="2026-07-05T08:30:00-04:00",
                selected_symbols=["SOFI"],
                report_path="/tmp/missing.json",
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
                "load_committed_selection_bundle",
                lambda project_dir: {
                    "bundle_root": root,
                    "state": {"et_date": "2026-07-05", "selected_symbols": ["SOFI"], "top_config_symbols": ["SOFI"]},
                },
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
            assert engine._ai_selection.selection_mode == "STALE"
            assert engine._ai_selection.fallback_reason == "selection_state_date_mismatch:2026-07-05"
            assert engine._ai_entry_allowed() is False
    finally:
        monkeypatch.restore()


def test_trading_engine_refreshes_ai_selection_and_blocks_new_entries_when_bundle_becomes_blocked():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = root / "ai_selection_latest.json"
            report_path.write_text(
                json.dumps(
                    {
                        "selection_date": "2026-07-17",
                        "selection_stage": "FINALIZED",
                        "result_quality": "COMPLETE",
                        "research_admission": "RESEARCH_READY",
                        "top3": [{"ticker": "SOFI", "score": 0.0, "confidence": 0.5, "reason": "selected"}],
                        "top10": [{"ticker": "SOFI", "score": 0.0, "confidence": 0.5, "reason": "selected"}],
                    }
                ),
                encoding="utf-8",
            )
            configs_dir = root / "configs"
            configs_dir.mkdir(parents=True, exist_ok=True)
            for idx in range(1, 4):
                payload = {
                    "enabled": False,
                    "slot": idx,
                    "reason": "top_n_not_filled",
                    "selection_run_id": "run-1",
                    "top_sync_run_id": "run-1",
                    "selection_date": "2026-07-17",
                    "generated_at": "2026-07-17T08:30:00-04:00",
                    "result_quality": "COMPLETE",
                    "research_admission": "RESEARCH_READY",
                    "mode": "paper",
                }
                if idx == 1:
                    payload.update({"enabled": True, "ticker": "SOFI", "reason": "selected"})
                (configs_dir / f"TOP{idx}.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

            monkeypatch.setenv("SOXS_STATE_DIR", str(root / "state"))
            monkeypatch.setattr(selection_state, "PROJECT_DIR", root)
            selection_state.write_selection_state(
                et_date="2026-07-17",
                generated_at="2026-07-17T08:30:00-04:00",
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
                    universe=["SOFI"],
                    top10_path=report_path,
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
                "load_committed_selection_bundle",
                lambda project_dir: {
                    "bundle_root": root,
                    "state": {"et_date": "2026-07-17", "selected_symbols": ["SOFI"], "top_config_symbols": ["SOFI"]},
                },
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
            assert engine._ai_selection.active is False
            assert engine._ai_selection.selection_mode == "STALE"
            assert engine._ai_entry_allowed() is False

            report_path.write_text(
                json.dumps(
                    {
                        "selection_date": "2026-07-17",
                        "selection_stage": "FINALIZED",
                        "result_quality": "INVALID",
                        "research_admission": "BLOCKED",
                        "top3": [],
                        "top10": [],
                    }
                ),
                encoding="utf-8",
            )

            engine._initialize_ai_selector()
            assert engine._ai_selection.active is False
            assert engine._ai_selection.selection_mode == "STALE"
            assert engine._ai_entry_allowed() is False
            assert "禁止新开仓" in engine._blocked_ai_reason()
    finally:
        monkeypatch.restore()


def test_trading_engine_blocks_ai_buy_when_cached_selection_used_fallback():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = root / "ai_selection_latest.json"
            report_path.write_text(
                json.dumps(
                    {
                        "selection_date": "2026-07-17",
                        "fallback_used": True,
                        "top3": [{"ticker": "SOFI", "score": 55.0, "confidence": 0.58, "reason": "fallback"}],
                        "top10": [{"ticker": "SOFI", "score": 55.0, "confidence": 0.58, "reason": "fallback"}],
                    }
                ),
                encoding="utf-8",
            )
            configs_dir = root / "configs"
            configs_dir.mkdir(parents=True, exist_ok=True)
            for idx in range(1, 4):
                payload = {
                    "enabled": False,
                    "slot": idx,
                    "reason": "top_n_not_filled",
                    "selection_run_id": "run-1",
                    "top_sync_run_id": "run-1",
                    "selection_date": "2026-07-17",
                    "generated_at": "2026-07-17T08:30:00-04:00",
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
            selection_state.write_selection_state(
                et_date="2026-07-17",
                generated_at="2026-07-17T08:30:00-04:00",
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
                "load_committed_selection_bundle",
                lambda project_dir: {
                    "bundle_root": root,
                    "state": {"et_date": "2026-07-17", "selected_symbols": ["SOFI"], "top_config_symbols": ["SOFI"]},
                },
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

            assert engine._ai_selection.active is False
            assert engine._ai_selection.selection_mode == "STALE"
            assert engine._ai_selection.fallback_used is False
            assert engine._ai_entry_allowed() is False
            assert "selection_state_date_mismatch" in engine._blocked_ai_reason()
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
