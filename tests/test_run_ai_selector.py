from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "run_ai_selector.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_run_ai_selector_module", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_run_ai_selector_aborts_when_no_symbols_selected():
    module = _load_module()

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True):
            return {
                "top10": [],
                "top5": [],
                "top3": [],
                "report": [],
                "settings": {},
                "quality_filter_report": {},
            }

    original_selector = module.AIStrategySelector
    original_live_positions = module._live_equity_positions
    original_has_live = module._has_live_top_configs
    original_load_settings = module.load_runtime_settings
    original_write_log = module.write_selection_filter_log
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5}
        module.write_selection_filter_log = lambda payload: None

        try:
            module.main()
            raised = None
        except SystemExit as exc:
            raised = exc
    finally:
        module.AIStrategySelector = original_selector
        module._live_equity_positions = original_live_positions
        module._has_live_top_configs = original_has_live
        module.load_runtime_settings = original_load_settings
        module.write_selection_filter_log = original_write_log

    assert raised is not None
    assert raised.code == 1


def test_run_ai_selector_succeeds_with_openbb_flag_enabled():
    module = _load_module()
    config_writer_name = "src.ai_selector.config_writer"
    original_config_writer = sys.modules.get(config_writer_name)

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True):
            return {
                "top10": [
                    {"ticker": "NVDA", "score": 91.5},
                    {"ticker": "MSFT", "score": 88.2},
                ],
                "top5": [
                    {"ticker": "NVDA", "score": 91.5, "reduce_only": False},
                    {"ticker": "MSFT", "score": 88.2, "reduce_only": False},
                ],
                "top3": [],
                "report": [],
                "settings": {"selection_stage": "fast_preliminary"},
                "quality_filter_report": {},
            }

        def _format_report_rows(self, selected: list[dict]):
            return [
                {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                for idx, row in enumerate(selected)
            ]

    fake_config_writer = types.ModuleType(config_writer_name)
    written_configs: list[list[dict]] = []
    written_logs: list[dict] = []
    written_states: list[dict] = []
    written_reports: list[dict] = []
    spawned_refinement: list[str] = []

    def _fake_write_top_configs(selected: list[dict]) -> None:
        written_configs.append([dict(item) for item in selected])

    fake_config_writer.write_top_configs = _fake_write_top_configs

    original_selector = module.AIStrategySelector
    original_live_positions = module._live_equity_positions
    original_has_live = module._has_live_top_configs
    original_load_settings = module.load_runtime_settings
    original_write_log = module.write_selection_filter_log
    original_write_state = module.write_selection_state
    original_write_reports = module._write_reports
    original_restart = module._restart_top_engines
    original_spawn = module._spawn_background_refinement
    original_load_local_env = module.load_local_ai_env
    original_env = os.environ.copy()
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5, "max_symbols": 20}
        module.write_selection_filter_log = lambda payload: written_logs.append(dict(payload))
        module.write_selection_state = lambda **payload: written_states.append(dict(payload))
        module._write_reports = lambda summary: (written_reports.append(dict(summary)) or True) and (Path("/tmp/latest.json"), Path("/tmp/dated.json"))
        module._restart_top_engines = lambda: 0
        module._spawn_background_refinement = lambda timestamp: spawned_refinement.append(timestamp)
        module.load_local_ai_env = lambda: None
        sys.modules[config_writer_name] = fake_config_writer

        os.environ["SOXS_OPENBB_ENABLED"] = "1"
        os.environ["AI_SELECTOR_RESTART_TOP"] = "0"
        os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] = "1"

        module.main()
    finally:
        module.AIStrategySelector = original_selector
        module._live_equity_positions = original_live_positions
        module._has_live_top_configs = original_has_live
        module.load_runtime_settings = original_load_settings
        module.write_selection_filter_log = original_write_log
        module.write_selection_state = original_write_state
        module._write_reports = original_write_reports
        module._restart_top_engines = original_restart
        module._spawn_background_refinement = original_spawn
        module.load_local_ai_env = original_load_local_env
        os.environ.clear()
        os.environ.update(original_env)
        if original_config_writer is None:
            sys.modules.pop(config_writer_name, None)
        else:
            sys.modules[config_writer_name] = original_config_writer

    assert written_configs
    assert [item["ticker"] for item in written_configs[0]] == ["NVDA", "MSFT"]
    assert written_logs
    assert written_logs[0]["final_selected_symbols"] == ["NVDA", "MSFT"]
    assert written_states
    assert written_states[0]["selected_symbols"] == ["NVDA", "MSFT"]
    assert written_reports
    assert written_reports[0]["top3"][0]["ticker"] == "NVDA"
    assert spawned_refinement


def run_test_direct():
    test_run_ai_selector_aborts_when_no_symbols_selected()
    test_run_ai_selector_succeeds_with_openbb_flag_enabled()


if __name__ == "__main__":
    run_test_direct()
