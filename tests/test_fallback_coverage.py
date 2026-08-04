"""Verify FALLBACK_PROFILES coverage against the managed universe snapshot.

The fallback layer is used when Yahoo Finance is unavailable — every
enabled universe symbol should ideally have a fallback profile so the
pipeline can produce results even during data-provider outages.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = PROJECT_DIR / "artifacts" / "universe" / "universe_snapshot.json"
SCORER_PATH = PROJECT_DIR / "src" / "scoring" / "scorer.py"


def _load_universe_symbols() -> set[str]:
    """Return the set of upper-case enabled symbols from the managed snapshot."""
    if not SNAPSHOT_PATH.exists():
        return set()

    with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
        data = json.load(fh)

    symbols: set[str] = set()
    for item in data.get("symbols", []):
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", False):
            continue
        sym = str(item.get("symbol", "")).strip().upper()
        if sym:
            symbols.add(sym)
    return symbols


def _load_fallback_symbols() -> set[str]:
    """Return the set of symbols in FALLBACK_PROFILES (excluding value keys)."""
    text = SCORER_PATH.read_text(encoding="utf-8")

    start = text.index("FALLBACK_PROFILES = {")
    end = text.index("\n    }", start)
    block = text[start:end]

    # The block uses lines like  "SYM": { ... }  — extract top-level keys only.
    raw = set(re.findall(r'"([A-Za-z0-9.-]+)"\s*:', block))

    # Filter out profile value keys that look like symbol names (e.g. "score", "volume")
    VALUE_KEYS = {"score", "volume"}
    return {s.upper() for s in raw if s.lower() not in VALUE_KEYS}


def _load_fallback_range_pct_symbols() -> set[str]:
    """Symbols present in FALLBACK_RANGE_PCT."""
    text = SCORER_PATH.read_text(encoding="utf-8")
    start = text.index("FALLBACK_RANGE_PCT = {")
    end = text.index("\n    }", start)
    block = text[start:end]
    return {s.upper() for s in re.findall(r'"([A-Za-z0-9.-]+)"\s*:', block)}


def _load_fallback_sector_symbols() -> set[str]:
    """Symbols present in FALLBACK_SECTOR."""
    text = SCORER_PATH.read_text(encoding="utf-8")
    start = text.index("FALLBACK_SECTOR = {")
    end = text.index("\n    }", start)
    block = text[start:end]
    return {s.upper() for s in re.findall(r'"([A-Za-z0-9.-]+)"\s*:', block)}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_fallback_profiles_not_empty():
    """FALLBACK_PROFILES must contain at least the original set of symbols."""
    profiles = _load_fallback_symbols()
    assert len(profiles) >= 60, (
        f"FALLBACK_PROFILES unexpectedly small: {len(profiles)} symbols"
    )


def test_fallback_dicts_are_consistent():
    """Every FALLBACK_PROFILES entry must have RANGE_PCT and SECTOR entries."""
    profiles = _load_fallback_symbols()
    rpct = _load_fallback_range_pct_symbols()
    sector = _load_fallback_sector_symbols()

    missing_rpct = profiles - rpct
    missing_sector = profiles - sector

    assert not missing_rpct, (
        f"FALLBACK_RANGE_PCT missing: {sorted(missing_rpct)}"
    )
    assert not missing_sector, (
        f"FALLBACK_SECTOR missing: {sorted(missing_sector)}"
    )


def test_managed_universe_coverage_report():
    """Print a coverage report and assert minimum acceptable coverage.

    If the snapshot is missing (CI, clean checkout), the test passes
    with a warning.  When present, coverage must be >= 30%.
    """
    universe = _load_universe_symbols()
    if not universe:
        # No snapshot available — CI or fresh checkout
        return

    profiles = _load_fallback_symbols()
    covered = sorted(universe & profiles)
    missing = sorted(universe - profiles)

    coverage = len(covered) / len(universe) * 100

    # Print coverage report (visible in pytest -v)
    print(f"\n  Universe:  {len(universe):>4} symbols")
    print(f"  Fallback:  {len(profiles):>4} profiles")
    print(f"  Covered:   {len(covered):>4}  ({coverage:.1f}%)")
    print(f"  Missing:   {len(missing):>4}")

    assert coverage >= 30.0, (
        f"Fallback coverage {coverage:.1f}% is below 30% minimum. "
        f"Add profiles for the {len(missing)} missing symbols."
    )


def test_no_orphan_range_pct_entries():
    """Every FALLBACK_RANGE_PCT entry must belong to a FALLBACK_PROFILES symbol."""
    profiles = _load_fallback_symbols()
    rpct = _load_fallback_range_pct_symbols()
    orphans = rpct - profiles
    assert not orphans, f"Orphan RANGE_PCT entries: {sorted(orphans)}"


def test_no_orphan_sector_entries():
    """Every FALLBACK_SECTOR entry must belong to a FALLBACK_PROFILES symbol."""
    profiles = _load_fallback_symbols()
    sector = _load_fallback_sector_symbols()
    orphans = sector - profiles
    assert not orphans, f"Orphan SECTOR entries: {sorted(orphans)}"
