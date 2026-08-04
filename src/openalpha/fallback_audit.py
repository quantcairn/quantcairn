"""Fallback profile validation audit.

Reads ``Scorer.FALLBACK_PROFILES`` and the supporting lookup dicts,
then simulates the same universe-validation and spread checks that
``_fallback_scored_item()`` performs at runtime.

Returns a structured report so operators can fix stale or misaligned
profile entries before they cause silent scoring drops.

This module is read-only — it never modifies the profile dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .universe_filter import (
    DEFAULT_UNIVERSE_RULES,
    UniverseRule,
    evaluate_universe_candidate,
    infer_asset_type,
    load_universe_rules,
)


# ── Default spread threshold (mirrors Scorer.DEFAULT_MIN_SPREAD_PCT) ────────
DEFAULT_MIN_SPREAD_PCT = 3.0


def _resolve_asset_type(symbol: str, sector_label: str | None) -> str:
    """Return the most-accurate asset type for a fallback profile symbol.

    ``infer_asset_type()`` uses ``symbol_class_for()`` which has
    incomplete coverage of leveraged / inverse ETFs.  We fall back to
    the FALLBACK_SECTOR label when it contains an explicit ETF keyword.
    """
    raw = infer_asset_type(symbol)
    if raw != "common_stock":
        return raw   # already specific enough

    if sector_label:
        sl = sector_label.lower()
        if "leveraged" in sl and "etf" in sl:
            return "leveraged_etf"
        if "inverse" in sl and "etf" in sl:
            return "inverse_etf"
        if "etf" in sl:
            return "etf"
    return raw


@dataclass
class ProfileFinding:
    """One audited issue for a single symbol."""
    symbol: str
    category: str               # e.g. "price_out_of_range", "missing_sector"
    detail: str = ""            # human-readable explanation
    asset_type: str = "unknown"


@dataclass
class FallbackAuditReport:
    """Results of running ``validate_fallback_profiles()``."""
    total_profiles: int = 0
    valid: int = 0
    findings: list[ProfileFinding] = field(default_factory=list)

    @property
    def invalid(self) -> int:
        return self.total_profiles - self.valid

    def by_category(self) -> dict[str, list[ProfileFinding]]:
        grouped: dict[str, list[ProfileFinding]] = {}
        for f in self.findings:
            grouped.setdefault(f.category, []).append(f)
        return dict(sorted(grouped.items()))

    def summary(self) -> str:
        lines: list[str] = []
        lines.append("Fallback Profile Audit")
        lines.append(f"====")
        lines.append(f"Total:   {self.total_profiles}")
        lines.append(f"Valid:   {self.valid}")
        lines.append(f"Invalid: {self.invalid}")
        if not self.findings:
            lines.append("All profiles pass ✅")
            return "\n".join(lines)
        for cat, items in self.by_category().items():
            lines.append(f"\n  [{cat}] — {len(items)} profile(s)")
            for item in items[:10]:
                lines.append(f"    {item.symbol:<8s} ({item.asset_type}): {item.detail}")
            if len(items) > 10:
                lines.append(f"    ... and {len(items) - 10} more")
        return "\n".join(lines)


def validate_fallback_profiles(
    rules: dict[str, UniverseRule] | None = None,
    min_spread_pct: float = DEFAULT_MIN_SPREAD_PCT,
) -> FallbackAuditReport:
    """Audit every entry in ``Scorer.FALLBACK_PROFILES`` against the
    current universe filter rules and the three supporting lookup dicts
    (FALLBACK_SECTOR, FALLBACK_RANGE_PCT, FALLBACK_MARKET_CAP).

    Returns a ``FallbackAuditReport``.  No filesystem or network access.
    """
    from src.scoring.scorer import Scorer

    scorer = Scorer()
    profiles = scorer.FALLBACK_PROFILES
    sectors = scorer.FALLBACK_SECTOR
    range_pcts = scorer.FALLBACK_RANGE_PCT
    market_caps = scorer.FALLBACK_MARKET_CAP
    active_rules = rules or load_universe_rules()

    report = FallbackAuditReport(total_profiles=len(profiles))

    for symbol_raw, profile in profiles.items():
        symbol = str(symbol_raw).strip().upper()
        findings: list[ProfileFinding] = []

        sector_label = sectors.get(symbol)
        asset_type = _resolve_asset_type(symbol, sector_label)

        support = float(profile.get("range_low") or 0)
        resistance = float(profile.get("range_high") or 0)
        volume = float(profile.get("volume") or 0)
        price_mid = (support + resistance) / 2.0 if resistance > support > 0 else 0.0

        # ── 1. Structural: required lookup-table entries ──────────────────
        if sector_label is None:
            findings.append(ProfileFinding(symbol, "missing_sector",
                "No FALLBACK_SECTOR entry", asset_type))
        if symbol not in range_pcts:
            findings.append(ProfileFinding(symbol, "missing_range_pct",
                "No FALLBACK_RANGE_PCT entry", asset_type))
        if asset_type == "common_stock" and symbol not in market_caps:
            findings.append(ProfileFinding(symbol, "missing_market_cap",
                "No FALLBACK_MARKET_CAP entry for common_stock", asset_type))

        # ── 2. Data sanity: range must be positive ────────────────────────
        if resistance <= support:
            findings.append(ProfileFinding(symbol, "invalid_range",
                f"range_low={support} range_high={resistance}", asset_type))
            report.findings.extend(findings)
            if not findings:
                report.valid += 1
            continue

        if price_mid <= 0:
            findings.append(ProfileFinding(symbol, "invalid_price",
                f"price_mid={price_mid} from [{support}, {resistance}]", asset_type))
            report.findings.extend(findings)
            if not findings:
                report.valid += 1
            continue

        # ── 3. Simulate universe validation (same path as _fallback_scored_item) ──
        candidate = {
            "ticker": symbol,
            "current_price": price_mid,
            "asset_type": asset_type,
            "market_cap": market_caps.get(symbol),
            "average_dollar_volume_20d": volume * price_mid,
            "atr_20_percentage": ((resistance - support) / price_mid * 100.0) / 2.0,
            "data_source": "fallback",
        }
        eval_result = evaluate_universe_candidate(candidate, rules=active_rules, skip_atr_validation=True)

        if eval_result.rejected:
            findings.append(ProfileFinding(
                symbol, "universe_validation_failure",
                ",".join(eval_result.rejection_reason), asset_type,
            ))

        # ── 4. Spread check (same as _fallback_scored_item) ───────────────
        spread_pct = ((resistance - support) / support * 100.0) if support > 0 else 0.0
        if spread_pct < min_spread_pct:
            findings.append(ProfileFinding(
                symbol, "spread_too_narrow",
                f"spread={spread_pct:.2f}% (min={min_spread_pct}%)", asset_type,
            ))

        report.findings.extend(findings)
        if not findings:
            report.valid += 1

    return report
