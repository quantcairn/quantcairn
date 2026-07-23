#!/usr/bin/env python3
"""Detect current market regime from index-level indicators.

Reads SPY/QQQ/IWM OHLCV + VIX quote via PriceFetcher.
Classifies into BULL / SIDEWAYS / BEAR / RISK_OFF.

Usage:
    python scripts/run_regime_detector.py           # detect + print
    python scripts/run_regime_detector.py --apply   # write current_regime.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.regime.detector import detect_regime
from src.regime.models import REGIME_ARTIFACT_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect current market regime")
    parser.add_argument("--apply", action="store_true", help="Write current_regime.json (default: detect only)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON")
    args = parser.parse_args()

    dry_run = not args.apply
    state = detect_regime(dry_run=dry_run)

    if args.json_output:
        print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False, default=str))
        return 0

    icons = {"BULL": "🟢", "SIDEWAYS": "🟡", "BEAR": "🔴", "RISK_OFF": "⚪"}
    icon = icons.get(state.regime, "⚪")

    print()
    print("=" * 50)
    print(f"  {icon}  Market Regime: {state.regime}  ({state.confidence:.0%} confidence)")
    print("=" * 50)
    print(f"  Date:        {state.date}")
    print(f"  VIX:         {state.vix:.1f} ({state.vix_change_pct:+.1f}%)")
    print(f"  5d Return:   {state.market_return_5d:+.2f}%")
    print(f"  20d Return:  {state.market_return_20d:+.2f}%")
    print()
    print(f"  Regime scores:")
    print(f"    🟢 BULL:      {state.bull_score:.1f}")
    print(f"    🟡 SIDEWAYS:  {state.sideways_score:.1f}")
    print(f"    🔴 BEAR:      {state.bear_score:.1f}")
    print(f"    ⚪ RISK_OFF:  {state.risk_off_score:.1f}")
    print()
    if state.signals:
        print("  Signals:")
        for s in state.signals:
            print(f"    • {s}")
    print()

    if state.indices:
        print("  Index snapshots:")
        for sym, snap in state.indices.items():
            p20 = snap.get("price_vs_ma20", 1.0)
            arrow = ">" if p20 > 1.0 else ("<" if p20 < 1.0 else "≈")
            print(f"    {sym:4s}  ${snap.get('price', 0):.2f}  "
                  f"MA20={snap.get('ma20', 0):.2f} (price{arrow}MA20)  "
                  f"RSI={snap.get('rsi', 50):.0f}")

    if not dry_run:
        REGIME_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        import os, tempfile
        path = REGIME_ARTIFACT_DIR / "current_regime.json"
        fd, tmp = tempfile.mkstemp(prefix=".regime.", suffix=".tmp", dir=str(REGIME_ARTIFACT_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            os.replace(tmp, path)
            print(f"  Written: {path}")
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            raise

    print("=" * 50)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
