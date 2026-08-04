"""Semantic tests for the demo banner — modern typography edition.

The banner uses clean letter-spaced text, not heavy FIGlet blocks.
The Q is the literal letter Q — no Q/O ambiguity.
"""

import importlib.util


def _load_banner() -> str:
    spec = importlib.util.spec_from_file_location(
        "run_demo_selector", "scripts/run_demo_selector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "DEMO_BANNER", "")


# ── Brand identity ──────────────────────────────────────────────────────────


def test_brand_name_present():
    """Brand name must appear as clean, letter-spaced text."""
    banner = _load_banner()
    assert "Q U A N T C A I R N" in banner, (
        "Brand name 'Q U A N T C A I R N' not found."
    )


def test_tagline_present():
    """Tagline must appear."""
    banner = _load_banner()
    assert "AI Research Platform" in banner, (
        "Tagline 'AI Research Platform' not found."
    )


def test_horizontal_rule_present():
    """A heavy double-line rule (══) must separate brand from tagline."""
    banner = _load_banner()
    # Find interior lines (║ ... ║) whose content is entirely ═ chars
    rule_rows = [
        ln for ln in banner.split("\n")
        if ln.startswith("║") and ln.endswith("║")
        and ln[1:-1].strip()  # non-empty content
        and all(c in "═ " for c in ln[1:-1])
    ]
    assert len(rule_rows) >= 1, (
        f"Horizontal double-line rule not found. "
        f"Expected a row of ═ inside the box."
    )


def test_no_figlet_blocks_remaining():
    """The old FIGlet art used heavy block chars that are gone now.
    Neither the heavy Q/O blocks nor the doubled-C pattern may exist."""
    banner = _load_banner()
    assert "████████╗ ██████╗" not in banner, (
        "Old FIGlet doubled-C pattern still present."
    )
    # No heavy filled blocks >= 6 chars in a row (was FIGlet "standard" font)
    for ln in banner.split("\n"):
        block_runs = ln.count("█")
        assert block_runs < 5, (
            f"Line still has heavy FIGlet blocks ({block_runs}): {ln.strip()[:40]}..."
        )


# ── Box integrity ────────────────────────────────────────────────────────────


def test_top_and_bottom_borders_equal_length():
    """Top and bottom borders must have identical width."""
    banner = _load_banner()
    lines = banner.split("\n")
    top_line, bot_line = lines[0], lines[-1]
    assert top_line.startswith("╔") and top_line.endswith("╗")
    assert bot_line.startswith("╚") and bot_line.endswith("╝")
    assert len(top_line) == len(bot_line), (
        f"border width mismatch: top={len(top_line)} bottom={len(bot_line)}"
    )


def test_every_row_has_both_border_chars():
    """Every interior row starts and ends with ║."""
    banner = _load_banner()
    for i, ln in enumerate(banner.split("\n")[1:-1], start=2):
        stripped = ln.rstrip()
        assert stripped.startswith("║"), f"row {i}: missing left border"
        assert stripped.endswith("║"), f"row {i}: missing right border"


def test_all_rows_same_width():
    """All banner rows must have identical character width."""
    banner = _load_banner()
    widths = {len(ln) for ln in banner.split("\n")}
    assert len(widths) == 1, (
        f"Inconsistent row widths: {sorted(widths)}"
    )


def test_demo_mode_mentioned():
    """The informational line must mention demo mode."""
    banner = _load_banner()
    assert "demo" in banner.lower(), "Demo mode indicator missing"
