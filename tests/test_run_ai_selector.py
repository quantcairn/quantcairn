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

        def run_selection(self, write_configs: bool = True, symbols_override=None):
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
    original_run_integrated = module._run_integrated_ai_selector
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5}
        module.write_selection_filter_log = lambda payload: None
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [],
            "preferred_symbols": [],
            "signal_map": {},
            "providers_used": [],
            "providers_disabled": ["openbb", "fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }

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
        module._run_integrated_ai_selector = original_run_integrated

    assert raised is not None
    assert raised.code == 1


def test_run_ai_selector_succeeds_with_openbb_flag_enabled():
    module = _load_module()
    config_writer_name = "src.ai_selector.config_writer"
    original_config_writer = sys.modules.get(config_writer_name)

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
                "top10": [
                    {"ticker": "NVDA", "score": 91.5, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0},
                    {"ticker": "MSFT", "score": 88.2, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0},
                ],
                "top5": [
                    {"ticker": "NVDA", "score": 91.5, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0},
                    {"ticker": "MSFT", "score": 88.2, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0},
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
    original_run_integrated = module._run_integrated_ai_selector
    original_split_selected = module._split_selected_and_protected_positions
    original_annotate = module._annotate_with_ai_signals
    original_apply_range_scores = module._apply_range_scores
    original_build_report_top10 = module._build_report_top10
    original_finalize_price_band = module._finalize_price_band
    original_apply_trade_filter = module._apply_trade_filter
    original_apply_composition_filter = module._apply_composition_filter
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
        module._annotate_with_ai_signals = lambda rows, signal_map: [dict(item) for item in rows]
        module._apply_range_scores = lambda rows: [dict(item) for item in rows]
        module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: [dict(item) for item in (selector_top10 or selected or [])]
        module._finalize_price_band = lambda candidates, min_price, max_price: ([dict(item) for item in candidates], [])
        module._apply_trade_filter = lambda rows: ([dict(item) for item in rows], {"rejected": [], "fallback_used": False})
        module._apply_composition_filter = lambda rows, top_n=3: ([dict(item) for item in rows], {"rejected": [], "warnings": []})
        module._split_selected_and_protected_positions = lambda candidates, positions, limit=5: (list(candidates)[:limit], [])
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [
                {"ticker": "NVDA", "score": 91.5, "confidence": 0.8, "reason": "stub", "source": "stub"},
                {"ticker": "MSFT", "score": 88.2, "confidence": 0.75, "reason": "stub", "source": "stub"},
            ],
            "preferred_symbols": ["NVDA", "MSFT"],
            "signal_map": {
                "NVDA": {"ticker": "NVDA", "score": 91.5, "confidence": 0.8, "reason": "stub", "source": "stub"},
                "MSFT": {"ticker": "MSFT", "score": 88.2, "confidence": 0.75, "reason": "stub", "source": "stub"},
            },
            "providers_used": ["openbb"],
            "providers_disabled": ["fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }
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
        module._run_integrated_ai_selector = original_run_integrated
        module._split_selected_and_protected_positions = original_split_selected
        module._annotate_with_ai_signals = original_annotate
        module._apply_range_scores = original_apply_range_scores
        module._build_report_top10 = original_build_report_top10
        module._finalize_price_band = original_finalize_price_band
        module._apply_trade_filter = original_apply_trade_filter
        module._apply_composition_filter = original_apply_composition_filter
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
    top_item = written_reports[0]["top3"][0]
    assert isinstance(top_item.get("entry"), dict)
    for key in [
        "entry_proximity_score",
        "good_for_entry_now",
        "entry_quality",
        "entry_reason",
        "range_position",
        "dist_to_support",
        "dist_to_resistance",
    ]:
        assert key in top_item["entry"]
        assert key in top_item
    assert "selector_core" in written_reports[0]["providers_used"]
    assert "yfinance" in written_reports[0]["providers_used"]
    assert "openbb" in written_reports[0]["providers_used"]
    assert "fmp" in written_reports[0]["providers_disabled"]
    assert written_reports[0]["fmp_enabled"] is False
    assert spawned_refinement


def test_run_ai_selector_backfills_top10_when_selector_top10_empty():
    module = _load_module()
    config_writer_name = "src.ai_selector.config_writer"
    original_config_writer = sys.modules.get(config_writer_name)

    class FakeSelector:
        selection_size = 5

        def run_selection(self, write_configs: bool = True, symbols_override=None):
            return {
                "top10": [],
                "top5": [
                    {"ticker": "NVDA", "score": 91.5, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0},
                    {"ticker": "MSFT", "score": 88.2, "reduce_only": False, "current_price": 100.0, "range_low": 95.0, "range_high": 105.0},
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
    fake_config_writer.write_top_configs = lambda selected: None

    written_reports: list[dict] = []
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
    original_run_integrated = module._run_integrated_ai_selector
    original_split_selected = module._split_selected_and_protected_positions
    original_annotate = module._annotate_with_ai_signals
    original_apply_range_scores = module._apply_range_scores
    original_build_report_top10 = module._build_report_top10
    original_finalize_price_band = module._finalize_price_band
    original_apply_trade_filter = module._apply_trade_filter
    original_apply_composition_filter = module._apply_composition_filter
    original_env = os.environ.copy()
    try:
        module.AIStrategySelector = FakeSelector
        module._live_equity_positions = lambda: []
        module._has_live_top_configs = lambda: False
        module.load_runtime_settings = lambda: {"min_price": 10.0, "max_price": 200.0, "auto_refresh_minutes": 5, "max_symbols": 20}
        module.write_selection_filter_log = lambda payload: None
        module.write_selection_state = lambda **payload: None
        module._write_reports = lambda summary: (written_reports.append(dict(summary)) or True) and (Path("/tmp/latest.json"), Path("/tmp/dated.json"))
        module._restart_top_engines = lambda: 0
        module._spawn_background_refinement = lambda timestamp: None
        module.load_local_ai_env = lambda: None
        module._annotate_with_ai_signals = lambda rows, signal_map: [dict(item) for item in rows]
        module._apply_range_scores = lambda rows: [dict(item) for item in rows]
        module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: [dict(item) for item in (selector_top10 or selected or [])]
        module._finalize_price_band = lambda candidates, min_price, max_price: ([dict(item) for item in candidates], [])
        module._apply_trade_filter = lambda rows: ([dict(item) for item in rows], {"rejected": [], "fallback_used": False})
        module._apply_composition_filter = lambda rows, top_n=3: ([dict(item) for item in rows], {"rejected": [], "warnings": []})
        module._split_selected_and_protected_positions = lambda candidates, positions, limit=5: (list(candidates)[:limit], [])
        module._run_integrated_ai_selector = lambda: {
            "enabled": True,
            "top3": [],
            "top10": [],
            "preferred_symbols": [],
            "signal_map": {},
            "providers_used": [],
            "providers_disabled": ["openbb", "fmp"],
            "fmp_enabled": False,
            "fallback_used": False,
        }
        sys.modules[config_writer_name] = fake_config_writer

        os.environ["AI_SELECTOR_RESTART_TOP"] = "0"
        os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] = "0"

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
        module._run_integrated_ai_selector = original_run_integrated
        module._split_selected_and_protected_positions = original_split_selected
        module._annotate_with_ai_signals = original_annotate
        module._apply_range_scores = original_apply_range_scores
        module._build_report_top10 = original_build_report_top10
        module._finalize_price_band = original_finalize_price_band
        module._apply_trade_filter = original_apply_trade_filter
        module._apply_composition_filter = original_apply_composition_filter
        os.environ.clear()
        os.environ.update(original_env)
        if original_config_writer is None:
            sys.modules.pop(config_writer_name, None)
        else:
            sys.modules[config_writer_name] = original_config_writer

    assert written_reports
    assert [item["ticker"] for item in written_reports[0]["top10"]] == ["NVDA", "MSFT"]


def run_test_direct():
    test_run_ai_selector_aborts_when_no_symbols_selected()
    test_run_ai_selector_succeeds_with_openbb_flag_enabled()
    test_run_ai_selector_backfills_top10_when_selector_top10_empty()


if __name__ == "__main__":
    run_test_direct()
