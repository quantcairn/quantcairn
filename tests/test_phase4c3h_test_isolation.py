"""Regression coverage for fetcher test state leaking across test modules."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNKNOWN_SYMBOL_TEST = (
    "tests/test_market_data_separation.py::"
    "TestMarketDataPassThrough::"
    "test_full_pipeline_with_unknown_symbol_still_handled"
)


def test_fetcher_provider_stub_does_not_leak_into_market_data_pipeline():
    """The historical fetcher -> market-data order must stay isolated."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_fetcher.py",
            UNKNOWN_SYMBOL_TEST,
        ],
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated order regression:\n{result.stdout}\n{result.stderr}"
    )
