"""Test fallback profile validation audit.

Verifies that ``validate_fallback_profiles()`` correctly identifies
structural issues, universe-rule violations, and spread problems
without modifying any data.
"""

from __future__ import annotations

import pytest

from src.scoring.scorer import Scorer


# ── Helpers ──────────────────────────────────────────────────────────────────


def _audit(**overrides):
    from src.openalpha.fallback_audit import validate_fallback_profiles
    return validate_fallback_profiles(**overrides)


def _profiles_for(*symbols: str) -> dict:
    """Build a minimal FALLBACK_PROFILES dict for a set of test symbols."""
    from src.scoring.scorer import Scorer
    real = Scorer().FALLBACK_PROFILES
    return {s: real[s] for s in symbols if s in real}


# ── Audit is callable and returns a report ───────────────────────────────────


def test_audit_returns_report():
    """validate_fallback_profiles() returns a FallbackAuditReport."""
    report = _audit()
    assert report.total_profiles >= 60
    assert report.valid + report.invalid == report.total_profiles


def test_report_summary_contains_expected_sections():
    """summary() string includes key metrics."""
    summary = _audit().summary()
    assert "Total:" in summary
    assert "Valid:" in summary
    assert "Invalid:" in summary


def test_report_by_category_returns_grouped_findings():
    """by_category() groups findings by their category label."""
    report = _audit()
    grouped = report.by_category()
    assert isinstance(grouped, dict)
    for cat, items in grouped.items():
        assert isinstance(cat, str)
        assert isinstance(items, list)
        for f in items:
            assert f.category == cat


# ── Known-good profiles pass without findings ────────────────────────────────


def test_nvda_passes_if_in_profile():
    """NVDA profile is valid against current universe rules."""
    report = _audit()
    nvda_findings = [f for f in report.findings if f.symbol == "NVDA"]
    assert not nvda_findings, (
        f"NVDA should pass but had findings: "
        f"{[(f.category, f.detail) for f in nvda_findings]}"
    )


def test_aapl_passes_if_in_profile():
    """AAPL profile is valid against current universe rules."""
    report = _audit()
    aapl_findings = [f for f in report.findings if f.symbol == "AAPL"]
    assert not aapl_findings, (
        f"AAPL should pass but had findings: "
        f"{[(f.category, f.detail) for f in aapl_findings]}"
    )


# ── Invalid profiles are flagged ─────────────────────────────────────────────


def test_invalid_range_is_flagged():
    """A profile with range_high <= range_low is flagged."""
    from src.openalpha.fallback_audit import validate_fallback_profiles
    from unittest.mock import patch
    import copy

    with patch.object(Scorer, "FALLBACK_PROFILES", {
        "BROKEN": {"score": 50, "range_low": 100, "range_high": 50, "volume": 1_000_000},
    }), patch.object(Scorer, "FALLBACK_SECTOR", {"BROKEN": "Technology"}), \
       patch.object(Scorer, "FALLBACK_RANGE_PCT", {"BROKEN": 0.03}), \
       patch.object(Scorer, "FALLBACK_MARKET_CAP", {"BROKEN": 10_000_000_000}):
        report = validate_fallback_profiles()
        broken = [f for f in report.findings if f.symbol == "BROKEN"]
        assert len(broken) >= 1
        assert any(f.category == "invalid_range" for f in broken)


def test_missing_sector_is_flagged():
    """A profile without a FALLBACK_SECTOR entry is flagged."""
    from src.openalpha.fallback_audit import validate_fallback_profiles
    from unittest.mock import patch

    with patch.object(Scorer, "FALLBACK_PROFILES", {
        "NOSEC": {"score": 50, "range_low": 40, "range_high": 50, "volume": 5_000_000},
    }), patch.object(Scorer, "FALLBACK_SECTOR", {}), \
       patch.object(Scorer, "FALLBACK_RANGE_PCT", {"NOSEC": 0.03}), \
       patch.object(Scorer, "FALLBACK_MARKET_CAP", {"NOSEC": 20_000_000_000}):
        report = validate_fallback_profiles()
        nosec = [f for f in report.findings if f.symbol == "NOSEC"]
        assert any(f.category == "missing_sector" for f in nosec)


def test_missing_market_cap_for_common_stock_is_flagged():
    """A common_stock without FALLBACK_MARKET_CAP is flagged."""
    from src.openalpha.fallback_audit import validate_fallback_profiles
    from unittest.mock import patch

    with patch.object(Scorer, "FALLBACK_PROFILES", {
        "NOMC": {"score": 50, "range_low": 40, "range_high": 50, "volume": 5_000_000},
    }), patch.object(Scorer, "FALLBACK_SECTOR", {"NOMC": "Technology"}), \
       patch.object(Scorer, "FALLBACK_RANGE_PCT", {"NOMC": 0.03}), \
       patch.object(Scorer, "FALLBACK_MARKET_CAP", {}):
        report = validate_fallback_profiles()
        nomc = [f for f in report.findings if f.symbol == "NOMC"]
        assert any(f.category == "missing_market_cap" for f in nomc)


def test_price_out_of_range_is_flagged():
    """A profile whose midpoint exceeds the $200 common_stock cap is flagged."""
    from src.openalpha.fallback_audit import validate_fallback_profiles
    from unittest.mock import patch

    with patch.object(Scorer, "FALLBACK_PROFILES", {
        "EXPENSIVE": {"score": 50, "range_low": 400, "range_high": 500, "volume": 5_000_000},
    }), patch.object(Scorer, "FALLBACK_SECTOR", {"EXPENSIVE": "Technology"}), \
       patch.object(Scorer, "FALLBACK_RANGE_PCT", {"EXPENSIVE": 0.03}), \
       patch.object(Scorer, "FALLBACK_MARKET_CAP", {"EXPENSIVE": 800_000_000_000}):
        report = validate_fallback_profiles()
        expensive = [f for f in report.findings if f.symbol == "EXPENSIVE"]
        assert any(
            f.category == "universe_validation_failure"
            and "price_out_of_range" in f.detail
            for f in expensive
        )


def test_spread_too_narrow_is_flagged():
    """A profile with spread < 3% is flagged."""
    from src.openalpha.fallback_audit import validate_fallback_profiles
    from unittest.mock import patch

    with patch.object(Scorer, "FALLBACK_PROFILES", {
        "NARROW": {"score": 50, "range_low": 48, "range_high": 49, "volume": 5_000_000},
    }), patch.object(Scorer, "FALLBACK_SECTOR", {"NARROW": "Technology"}), \
       patch.object(Scorer, "FALLBACK_RANGE_PCT", {"NARROW": 0.01}), \
       patch.object(Scorer, "FALLBACK_MARKET_CAP", {"NARROW": 50_000_000_000}):
        report = validate_fallback_profiles()
        narrow = [f for f in report.findings if f.symbol == "NARROW"]
        assert any(f.category == "spread_too_narrow" for f in narrow), (
            f"Expected spread_too_narrow, got: {[(f.category, f.detail) for f in narrow]}"
        )


# ── ETF symbols are not flagged for missing market cap ──────────────────────


def test_spy_not_flagged_for_market_cap():
    """SPY is an ETF — should never be flagged for missing_market_cap."""
    report = _audit()
    spy_findings = [f for f in report.findings if f.symbol == "SPY"]
    assert not any(f.category == "missing_market_cap" for f in spy_findings), (
        f"SPY incorrectly flagged for missing_market_cap: {spy_findings}"
    )


def test_soxl_not_flagged_for_market_cap():
    """SOXL is a leveraged ETF — should never be flagged for missing_market_cap."""
    report = _audit()
    soxl_findings = [f for f in report.findings if f.symbol == "SOXL"]
    assert not any(f.category == "missing_market_cap" for f in soxl_findings), (
        f"SOXL incorrectly flagged: {soxl_findings}"
    )


# ── Score behavior untouched ─────────────────────────────────────────────────


def test_audit_does_not_mutate_profiles():
    """Running the audit must not modify FALLBACK_PROFILES."""
    scorer = Scorer()
    before = dict(scorer.FALLBACK_PROFILES)
    _audit()
    after = dict(scorer.FALLBACK_PROFILES)
    assert before == after, "validate_fallback_profiles() mutated FALLBACK_PROFILES"


def test_audit_does_not_mutate_sector():
    """Running the audit must not modify FALLBACK_SECTOR."""
    scorer = Scorer()
    before = dict(scorer.FALLBACK_SECTOR)
    _audit()
    after = dict(scorer.FALLBACK_SECTOR)
    assert before == after, "validate_fallback_profiles() mutated FALLBACK_SECTOR"
