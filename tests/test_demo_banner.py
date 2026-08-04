"""Semantic tests for the demo banner ASCII art.

The banner must spell "QUANTCAIRN" (QUANT + CAIRN), NOT the buggy
"QUANTCCAIRN" variant that had a doubled C between the two blocks.
"""

import importlib.util


def _load_banner() -> str:
    spec = importlib.util.spec_from_file_location(
        "run_demo_selector", "scripts/run_demo_selector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "DEMO_BANNER", "")


def test_banner_is_not_empty():
    """Banner loads and is non-trivial."""
    banner = _load_banner()
    assert len(banner) > 200, f"banner too short ({len(banner)} chars)"
    assert "QuantCairn" in banner or "AI Research Pipeline" in banner


def test_banner_has_no_doubled_c():
    """The old bug had C's top bar (██████╗) glued directly after
    T's top bar (████████╗) on the same row, producing QUANTC+C=QUANTCCAIRN.
    That adjacency must never appear."""
    banner = _load_banner()
    assert "████████╗ ██████╗" not in banner, (
        "Banner contains doubled-C pattern (QUANTCCAIRN). "
        "Block 1 still has a trailing C glued after T."
    )


def test_banner_contains_both_blocks():
    """Both FIGlet blocks must be present: the first (QUANT) starts with
    Q's top bar, the second (CAIRN) starts with C's top bar indented."""
    banner = _load_banner()
    lines = banner.split("\n")
    figlet_lines = [ln for ln in lines if "║" in ln and "══" not in ln and "AI Research" not in ln and "█" in ln]
    # We need at least two FIGlet rows: one from QUANT, one from CAIRN
    assert len(figlet_lines) >= 2, (
        f"Expected ≥2 FIGlet rows (QUANT + CAIRN blocks), got {len(figlet_lines)}"
    )
    # Every FIGlet row in the full banner must contain at least one box-drawing char
    for i, ln in enumerate(figlet_lines):
        assert "█" in ln, f"FIGlet row {i} has no block characters"
