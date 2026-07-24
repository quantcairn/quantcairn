#!/usr/bin/env python3
"""Validate the QuantCairn development environment.

Checks:
  - Python version (≥3.11 required)
  - quantcairn package import
  - Public API imports
  - Demo data provider availability
  - Test framework presence

Exit code 0 → everything OK.  Non-zero → one or more checks failed.

Run with: python scripts/check_dev_environment.py
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def _fail(reason: str) -> None:
    print(f"  ❌  {reason}")
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"  ✅  {msg}")


def main() -> None:
    print("QuantCairn — Developer Environment Check")
    print("=" * 45)
    print()

    # 1. Python version
    version = sys.version_info
    print(f"  Python: {sys.version.split()[0]}")
    if version < (3, 11):
        _fail(f"Python ≥3.11 required, found {sys.version.split()[0]}")
    _ok(f"Python {version.major}.{version.minor} — OK")
    print()

    # 2. quantcairn package
    print("  Package imports:")
    try:
        import quantcairn
        _ok(f"quantcairn v{quantcairn.__version__}")
    except ImportError as e:
        _fail(f"quantcairn not importable: {e}")

    # 3. Public API
    names = ["AIStrategySelector", "DemoDataProvider", "FunnelTracker",
             "PreflightReport", "score_candidate", "check_data_availability",
             "evaluate_universe_candidate", "load_runtime_settings"]
    for name in names:
        try:
            getattr(quantcairn, name)
        except AttributeError:
            _fail(f"quantcairn.{name} missing from public API")
    _ok("All public API symbols accessible")
    print()

    # 4. Demo provider
    print("  Demo data:")
    try:
        provider = quantcairn.DemoDataProvider()
        assert len(provider.symbols) == 5, "expected 5 demo symbols"
        df = provider.get_ohlcv("AAPL")
        assert len(df) == 252, "expected 252 rows"
        assert "Close" in df.columns
        _ok(f"DemoDataProvider OK — {len(provider.symbols)} symbols, "
            f"{len(df)} rows each")
    except Exception as e:
        _fail(f"DemoDataProvider failed: {e}")
    print()

    # 5. Test framework
    print("  Test framework:")
    try:
        import pytest
        _ok(f"pytest v{pytest.__version__}")
    except ImportError:
        _fail("pytest not installed — run: pip install pytest")
    print()

    # ── Summary ──────────────────────────────────────────────────────────
    print("=" * 45)
    print("  All checks passed — environment ready.")
    print()
    print("  Next steps:")
    print("    python scripts/run_demo_selector.py   ← full pipeline demo")
    print("    python examples/basic_demo.py          ← Python API quick-start")
    print("    pytest tests/ -q                       ← run test suite")
    print("=" * 45)


if __name__ == "__main__":
    main()
