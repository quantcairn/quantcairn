from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.config.local_env import load_local_ai_env


def test_load_local_ai_env_populates_missing_values_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = Path(tmpdir) / ".env.ai_selector.local"
        env_path.write_text(
            "SOXS_TRADINGAGENTS_PATH=/tmp/tradingagents\n"
            "SOXS_AI_SELECTOR_ENABLED=1\n",
            encoding="utf-8",
        )

        previous_path = os.environ.get("SOXS_TRADINGAGENTS_PATH")
        previous_enabled = os.environ.get("SOXS_AI_SELECTOR_ENABLED")
        os.environ["SOXS_AI_SELECTOR_ENABLED"] = "0"
        os.environ.pop("SOXS_TRADINGAGENTS_PATH", None)
        try:
            loaded = load_local_ai_env(env_path)
            assert loaded is True
            assert os.environ["SOXS_TRADINGAGENTS_PATH"] == "/tmp/tradingagents"
            assert os.environ["SOXS_AI_SELECTOR_ENABLED"] == "0"
        finally:
            if previous_path is None:
                os.environ.pop("SOXS_TRADINGAGENTS_PATH", None)
            else:
                os.environ["SOXS_TRADINGAGENTS_PATH"] = previous_path
            if previous_enabled is None:
                os.environ.pop("SOXS_AI_SELECTOR_ENABLED", None)
            else:
                os.environ["SOXS_AI_SELECTOR_ENABLED"] = previous_enabled


def run_test_direct():
    test_load_local_ai_env_populates_missing_values_only()
