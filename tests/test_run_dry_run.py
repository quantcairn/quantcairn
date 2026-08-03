from __future__ import annotations

import os
import subprocess
import tempfile
import sys
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None

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
            [sys.executable, "run.py", "--dry-run", "--config", str(config_path)],
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
    ny = ZoneInfo("America/New_York") if ZoneInfo is not None else None

    def et(year, month, day, hour, minute):
        if ny is None:
            return datetime(year, month, day, hour, minute)
        return datetime(year, month, day, hour, minute, tzinfo=ny)

    assert is_trading_day(et(2026, 7, 3, 9, 0)) is False
    assert is_trading_day(et(2026, 7, 2, 9, 0)) is True
    assert is_market_time(et(2026, 7, 3, 9, 0)) is False
    assert is_market_time(et(2026, 7, 2, 9, 34)) is False
    assert is_market_time(et(2026, 7, 2, 9, 35)) is True
    assert is_market_time(et(2026, 7, 2, 10, 30)) is True
    assert is_market_time(et(2026, 7, 2, 10, 31)) is False
    assert is_market_time(et(2026, 11, 2, 9, 34)) is False
    assert is_market_time(et(2026, 11, 2, 9, 35)) is True
    assert is_market_time(et(2026, 11, 2, 10, 30)) is True
    assert is_market_time(et(2026, 11, 2, 10, 31)) is False


def test_ai_selector_wrapper_is_quiet_when_not_due(monkeypatch, capsys):
    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime(2026, 7, 18, 9, 0)
            return datetime(2026, 7, 18, 9, 0, tzinfo=tz)

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
