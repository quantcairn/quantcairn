"""Shadow Market Regime Evaluation.

Computes Bull / Bear / Range scores in parallel to the production
selector pipeline.  NEVER modifies selected_symbols, scores, TOP
configs, SelectionBundle, Paper, Live, Broker, or any trading state.

Output: artifacts/research/regime_shadow/<date>/<run_id>/regime_shadow_report.json
Display: 8090 dashboard (read-only research card)
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.openalpha.selection_bundle import load_committed_selection_bundle

PROJECT_DIR = Path(__file__).resolve().parents[2]
SHADOW_REGIME_ROOT = PROJECT_DIR / "artifacts" / "research" / "regime_shadow"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return default
    if result != result or result in {float("inf"), float("-inf")}:
        return default
    return round(result, 4)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize(s: Any) -> str:
    return str(s or "").strip().upper()


def _score_to_label(score: float) -> str:
    if score >= 70:
        return "STRONG"
    if score >= 50:
        return "MODERATE"
    if score >= 30:
        return "WEAK"
    return "NEGLIGIBLE"


# ── Indicator extraction ───────────────────────────────────────────────

@dataclass(slots=True)
class CandidateIndicators:
    symbol: str
    price: float = 0.0
    ma20: float = 0.0
    ma50: float = 0.0
    ma200: float = 0.0
    adx: float = 0.0
    spread_pct: float = 0.0
    gap_pct: float = 0.0
    avg_dollar_volume_20d: float = 0.0
    rsi: float = 50.0
    relative_strength_vs_spy: float = 0.0
    three_day_change_pct: float = 0.0
    volatility_score: float = 50.0
    volume_score: float = 50.0
    trend_score: float = 50.0
    risk_score: float = 50.0
    strategy_fit_score: float = 50.0
    sector: str = ""
    asset_type: str = ""
    is_inverse_etf: bool = False

    @classmethod
    def from_candidate(cls, item: dict[str, Any]) -> "CandidateIndicators":
        sym = _normalize(item.get("ticker") or item.get("symbol") or "")
        md = item.get("market_data") or item.get("trade_market_data") or {}
        if not isinstance(md, dict):
            md = {}

        def _v(*keys: str) -> float:
            for k in keys:
                v = item.get(k) if k in item else md.get(k)
                if v is not None:
                    return _safe_float(v)
            return 0.0

        def _t(*keys: str) -> str:
            for k in keys:
                v = item.get(k) if k in item else md.get(k)
                if v:
                    return _safe_text(v)
            return ""

        at = _t("asset_type")
        is_inv = "inverse" in at.lower() or sym in {"SOXS", "SQQQ", "SPXU", "SDOW", "FAZ", "LABD", "YANG"}

        return cls(
            symbol=sym,
            price=_v("current_price", "price"),
            ma20=_v("ma20"),
            ma50=_v("ma50"),
            ma200=_v("ma200"),
            adx=_v("adx"),
            spread_pct=_v("spread_pct", "spread_pct_live"),
            gap_pct=_v("gap_pct"),
            avg_dollar_volume_20d=_v("average_dollar_volume_20d", "avg_10d_volume"),
            rsi=_v("rsi_14", "rsi"),
            relative_strength_vs_spy=_v("relative_strength_vs_SPY", "relative_strength_vs_spy", "relative_strength_60d"),
            three_day_change_pct=_v("three_day_change_pct"),
            volatility_score=_v("volatility_score"),
            volume_score=_v("volume_score"),
            trend_score=_v("trend_score", "trend_fit_score"),
            risk_score=_v("risk_score"),
            strategy_fit_score=_v("strategy_fit_score"),
            sector=_t("sector", "industry"),
            asset_type=at,
            is_inverse_etf=is_inv,
        )


# ── Regime scoring ─────────────────────────────────────────────────────

def _bull_score(ind: CandidateIndicators) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if ind.price > 0 and ind.ma20 > 0:
        if ind.price > ind.ma20:
            score += 20.0
        else:
            reasons.append("price_below_ma20")
    if ind.ma20 > 0 and ind.ma50 > 0:
        if ind.ma20 > ind.ma50:
            score += 15.0
        else:
            reasons.append("ma20_below_ma50")
    if ind.relative_strength_vs_spy >= 5.0:
        score += 15.0
    elif ind.relative_strength_vs_spy > 0:
        score += 8.0
    else:
        reasons.append("relative_strength_low")
    if ind.adx >= 18.0 and ind.price > ind.ma20:
        score += 15.0
    if ind.three_day_change_pct > 0:
        score += min(15.0, ind.three_day_change_pct)
    if ind.volume_score >= 60.0:
        score += 10.0
    if ind.trend_score >= 60.0:
        score += 10.0
    return round(min(100.0, score), 2), reasons


def _bear_score(ind: CandidateIndicators) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if ind.price > 0 and ind.ma20 > 0:
        if ind.price < ind.ma20:
            score += 20.0
        else:
            reasons.append("price_above_ma20")
    if ind.ma20 > 0 and ind.ma50 > 0:
        if ind.ma20 < ind.ma50:
            score += 15.0
        else:
            reasons.append("ma20_above_ma50")
    if ind.relative_strength_vs_spy <= -5.0:
        score += 15.0
    elif ind.relative_strength_vs_spy < 0:
        score += 8.0
    else:
        reasons.append("relative_strength_not_negative")
    if ind.is_inverse_etf:
        score += 15.0
        reasons.append("inverse_etf_bonus")
    if ind.three_day_change_pct < 0:
        score += min(15.0, abs(ind.three_day_change_pct))
    if ind.adx >= 18.0 and ind.price < ind.ma20:
        score += 15.0
    if ind.volatility_score >= 60.0:
        score += 10.0
    return round(min(100.0, score), 2), reasons


def _range_score(ind: CandidateIndicators) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if ind.price > 0 and ind.ma20 > 0:
        ratio = abs(ind.price - ind.ma20) / max(ind.ma20, 0.01)
        if ratio <= 0.03:
            score += 25.0
        elif ratio <= 0.08:
            score += 15.0
        else:
            reasons.append("far_from_ma20")
    if 30.0 <= ind.rsi <= 70.0:
        score += 20.0
    elif ind.rsi <= 30.0 or ind.rsi >= 70.0:
        score += 10.0
        reasons.append("rsi_extreme")
    if ind.adx <= 25.0:
        score += 20.0
    elif ind.adx <= 30.0:
        score += 10.0
    else:
        reasons.append("adx_trending")
    if abs(ind.three_day_change_pct) <= 5.0:
        score += 15.0
    if abs(ind.gap_pct) <= 2.0:
        score += 10.0
    if ind.spread_pct <= 0.3:
        score += 10.0
    return round(min(100.0, score), 2), reasons


# ── Per-candidate regime classification ────────────────────────────────

def _classify_candidate(ind: CandidateIndicators) -> dict[str, Any]:
    bs, br = _bull_score(ind)
    bes, ber = _bear_score(ind)
    rs, rr = _range_score(ind)

    scores = {"BULL": bs, "BEAR": bes, "RANGE": rs}
    winner = max(scores, key=scores.get)
    winner_score = scores[winner]

    # Determine if the classification is meaningful
    score_gap = sorted(scores.values(), reverse=True)
    gap = score_gap[0] - score_gap[1] if len(score_gap) > 1 else 0.0
    if gap >= 20.0:
        confidence = "HIGH"
    elif gap >= 10.0:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return {
        "symbol": ind.symbol,
        "bull_score": bs,
        "bull_label": _score_to_label(bs),
        "bull_reasons": br,
        "bear_score": bes,
        "bear_label": _score_to_label(bes),
        "bear_reasons": ber,
        "range_score": rs,
        "range_label": _score_to_label(rs),
        "range_reasons": rr,
        "winner": winner,
        "winner_score": winner_score,
        "confidence": confidence,
        "score_gap": round(gap, 2),
        "sector": ind.sector,
        "asset_type": ind.asset_type,
        "is_inverse_etf": ind.is_inverse_etf,
    }


# ── Top-level evaluation ───────────────────────────────────────────────

def evaluate_shadow_regime(
    *,
    selection_bundle: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run shadow regime evaluation against the current SelectionBundle.

    Never modifies the bundle, scores, or any trading state.
    """
    run_id = uuid.uuid4().hex[:12]
    started_at = _utc_now_iso()
    bundle = selection_bundle or load_committed_selection_bundle(PROJECT_DIR)

    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "status": "OK",
        "research_only": True,
        "shadow_mode": True,
    }

    if not isinstance(bundle, dict):
        report["status"] = "NO_BUNDLE"
        report["error"] = "SelectionBundle unavailable"
        return report

    manifest = bundle.get("manifest") or {}
    bundle_report = bundle.get("report") or {}

    report["selection_run_id"] = str(manifest.get("selection_run_id") or "")
    report["selection_date"] = str(manifest.get("selection_date") or bundle_report.get("selection_date") or "")
    report["selection_stage"] = str(manifest.get("selection_stage") or bundle_report.get("selection_stage") or "")

    # Extract all candidates from the bundle
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("top10", "top5", "top3", "tradable_top_candidates", "research_top_candidates"):
        items = bundle_report.get(key)
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                sym = _normalize(item.get("ticker") or item.get("symbol") or "")
                if sym and sym not in seen:
                    seen.add(sym)
                    candidates.append(dict(item))

    if not candidates:
        report["status"] = "NO_CANDIDATES"
        report["error"] = "No candidates found in SelectionBundle"
        return report

    # Evaluate each candidate
    per_candidate: list[dict[str, Any]] = []
    for item in candidates:
        ind = CandidateIndicators.from_candidate(item)
        if ind.price <= 0:
            continue
        result = _classify_candidate(ind)
        per_candidate.append(result)

    # Aggregate
    report["candidate_count"] = len(candidates)
    report["evaluated_count"] = len(per_candidate)
    report["per_candidate"] = per_candidate

    # Aggregate regime distribution
    regime_counts: dict[str, int] = {"BULL": 0, "BEAR": 0, "RANGE": 0}
    for r in per_candidate:
        regime_counts[r["winner"]] += 1

    report["regime_distribution"] = regime_counts

    # Aggregate scores
    if per_candidate:
        bs = [r["bull_score"] for r in per_candidate]
        bes = [r["bear_score"] for r in per_candidate]
        rs = [r["range_score"] for r in per_candidate]
        report["aggregate_scores"] = {
            "bull_avg": round(sum(bs) / len(bs), 2) if bs else 0.0,
            "bear_avg": round(sum(bes) / len(bes), 2) if bes else 0.0,
            "range_avg": round(sum(rs) / len(rs), 2) if rs else 0.0,
        }
        dominant = max(
            ("BULL", report["aggregate_scores"]["bull_avg"]),
            ("BEAR", report["aggregate_scores"]["bear_avg"]),
            ("RANGE", report["aggregate_scores"]["range_avg"]),
            key=lambda x: x[1],
        )
        report["dominant_regime"] = dominant[0]
        report["dominant_score"] = dominant[1]

        # Compare with current production result
        current_top3 = [
            _normalize(item.get("ticker") or item.get("symbol") or "")
            for item in (bundle_report.get("top3") or bundle_report.get("tradable_top_candidates") or [])
            if isinstance(item, dict)
        ]
        report["current_selected_symbols"] = current_top3
        report["current_selected_count"] = len(current_top3)

        # What would the shadow regime select? (same pool, different regime lens)
        shadow_by_regime: dict[str, list[dict[str, Any]]] = {}
        for r in per_candidate:
            w = r["winner"]
            shadow_by_regime.setdefault(w, []).append(r)
        for regime in ("BULL", "BEAR", "RANGE"):
            items = sorted(shadow_by_regime.get(regime, []), key=lambda x: -x["winner_score"])
            report[f"shadow_{regime.lower()}_top"] = [
                {"symbol": it["symbol"], "score": it["winner_score"], "confidence": it["confidence"]}
                for it in items[:5]
            ]

        # Differences from current selector
        if current_top3:
            shadow_top = [
                it["symbol"] for it in sorted(per_candidate, key=lambda x: -x["winner_score"])[:len(current_top3)]
            ]
            diff_added = set(shadow_top) - set(current_top3)
            diff_removed = set(current_top3) - set(shadow_top)
            report["comparison"] = {
                "current": current_top3,
                "shadow_top3": shadow_top,
                "added_by_shadow": sorted(diff_added),
                "removed_by_shadow": sorted(diff_removed),
                "overlap": sorted(set(current_top3) & set(shadow_top)),
                "changed": bool(diff_added or diff_removed),
            }

    if not dry_run:
        _write_report(report)

    return report


def _write_report(report: dict[str, Any]) -> Path:
    run_id = str(report.get("run_id") or "")
    sel_date = str(report.get("selection_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    out_dir = SHADOW_REGIME_ROOT / sel_date / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "regime_shadow_report.json"

    fd, tmp = tempfile.mkstemp(prefix=".regime_shadow.", suffix=".tmp", dir=str(out_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        os.replace(tmp, report_path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass

    # Write a latest symlink-style pointer
    latest_path = SHADOW_REGIME_ROOT / sel_date / "latest.json"
    try:
        latest_path.unlink(missing_ok=True)
        latest_path.symlink_to(report_path.name)
    except OSError:
        # Fallback: copy
        try:
            import shutil
            shutil.copy2(report_path, latest_path)
        except Exception:
            pass

    return report_path
