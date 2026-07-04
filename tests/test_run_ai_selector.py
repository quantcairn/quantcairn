from __future__ import annotations

import importlib.util
import sys
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


def run_test_direct():
    test_run_ai_selector_aborts_when_no_symbols_selected()


if __name__ == "__main__":
    run_test_direct()
