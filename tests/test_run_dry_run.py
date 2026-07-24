from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from scripts import ai_selector_wrapper
from scripts.ai_selector_wrapper import is_market_time, is_trading_day


def test_dry_run_exits_nonzero_for_invalid_config():
    repo_root = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            """
ticker: "SOXS"
mode: "paper"
range:
  mode: "manual"
  support_price: 30.0
  resistance_price: 29.0
""".strip()
            + "\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.pop("SOXS_CONFIG", None)

        result = subprocess.run(
            [str(repo_root / ".venv/bin/python"), "run.py", "--dry-run", "--config", str(config_path)],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "Configuration has errors" in result.stdout
    assert "Configuration is invalid" in result.stdout
    assert "Configuration is valid" not in result.stdout


def test_ai_selector_does_not_run_on_market_holiday():
    assert is_trading_day(datetime(2026, 7, 3, 9, 0)) is False
    assert is_trading_day(datetime(2026, 7, 2, 9, 0)) is True
    assert is_market_time(datetime(2026, 7, 3, 9, 0)) is False
    assert is_market_time(datetime(2026, 7, 2, 9, 0)) is True
    assert is_market_time(datetime(2026, 7, 2, 9, 25)) is False


def test_ai_selector_wrapper_is_quiet_when_not_due(monkeypatch, capsys):
    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 18, 9, 0)

    monkeypatch.setattr(ai_selector_wrapper, "datetime", FakeDateTime)
    monkeypatch.delenv("FORCE_AI_RUN", raising=False)
    monkeypatch.delenv("OPENALPHA_WRAPPER_VERBOSE", raising=False)

    ai_selector_wrapper._run_selection_if_due()

    assert capsys.readouterr().out == ""


def run_test_direct():
    test_dry_run_exits_nonzero_for_invalid_config()
    test_ai_selector_does_not_run_on_market_holiday()


if __name__ == "__main__":
    run_test_direct()
