"""Market Data Preflight Check.

Runs before the AI Selector to determine market state, data availability,
and the recommended run mode.  Advisory only — never modifies trading state.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

from src.utils.market_calendar import market_session_context, required_selection_date
from src.data.fetcher import PriceFetcher
from src.config.runtime_paths import resolve_artifacts_dir

PROJECT_DIR = Path(__file__).resolve().parents[2]
PREFLIGHT_ARTIFACT_DIR = resolve_artifacts_dir(PROJECT_DIR) / "selection"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return round(result, 4)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split(".")[0]


# ═══════════════════════════════════════════════════════════════════════════════
# Preflight report
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class PreflightReport:
    """Complete market data readiness assessment before selector execution."""

    selection_run_id: str = ""
    diagnostic_preflight: bool = False
    market_state: str = "UNKNOWN"  # PRE_MARKET | MARKET_OPEN | AFTER_HOURS | CLOSED
    is_trading_day: bool = False
    session_label: str = ""
    session_reason: str = ""
    current_session_date: str = ""
    previous_session_date: str = ""

    run_mode: str = "FULL"          # FULL | DEGRADED | AFTER_MARKET | EOD_ONLY
    data_mode: str = "LIVE"         # LIVE | MIXED | EOD_ONLY | UNAVAILABLE

    symbols_checked: int = 0
    quotes_available: int = 0
    ohlcv_available: int = 0
    quote_coverage_pct: float = 0.0
    ohlcv_coverage_pct: float = 0.0

    generated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_run_id": self.selection_run_id,
            "diagnostic_preflight": self.diagnostic_preflight,
            "market_state": self.market_state,
            "is_trading_day": self.is_trading_day,
            "session_label": self.session_label,
            "session_reason": self.session_reason,
            "current_session_date": self.current_session_date,
            "previous_session_date": self.previous_session_date,
            "run_mode": self.run_mode,
            "data_mode": self.data_mode,
            "symbols_checked": self.symbols_checked,
            "quotes_available": self.quotes_available,
            "ohlcv_available": self.ohlcv_available,
            "quote_coverage_pct": self.quote_coverage_pct,
            "ohlcv_coverage_pct": self.ohlcv_coverage_pct,
            "generated_at": self.generated_at,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Data availability scan
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_data_availability(
    symbols: list[str],
    *,
    max_symbols: int = 20,
) -> dict[str, int]:
    """Check quote and OHLCV availability for a sample of symbols.

    Returns {"quotes": N, "ohlcv": M, "checked": len(sample)}
    """
    sample = symbols[:max(1, min(max_symbols, len(symbols)))]
    quotes_ok = 0
    ohlcv_ok = 0

    for sym in sample:
        fetcher = PriceFetcher(sym, poll_interval=0)
        try:
            quote = fetcher.get_quote()
            if quote is not None and getattr(quote, "price", 0) > 0:
                quotes_ok += 1
        except Exception:
            pass
        try:
            candles = fetcher.get_ohlcv(period="5d", interval="1d")
            if len(candles) >= 3:
                closes = [float(getattr(c, "close", 0) or 0) for c in candles]
                if any(c > 0 for c in closes):
                    ohlcv_ok += 1
        except Exception:
            pass
        finally:
            try:
                fetcher.close()
            except Exception:
                pass

    return {"quotes": quotes_ok, "ohlcv": ohlcv_ok, "checked": len(sample)}


# ═══════════════════════════════════════════════════════════════════════════════
# Main preflight entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run_preflight(
    *,
    symbols: list[str] | None = None,
    max_scan_symbols: int = 20,
    dry_run: bool = False,
    selection_run_id: str | None = None,
    diagnostic_preflight: bool = False,
) -> PreflightReport:
    """Assess market state and data availability before selector execution.

    Returns a PreflightReport with market_state, run_mode, data_mode,
    and coverage statistics.  Never modifies trading state.
    """
    now_et = _et_now()
    session = market_session_context(now_et)
    req_date = required_selection_date(now_et)

    report = PreflightReport(
        selection_run_id=str(selection_run_id or "").strip(),
        diagnostic_preflight=bool(diagnostic_preflight),
        is_trading_day=(not session.is_market_holiday) and session.session_label != "CLOSED",
        session_label=session.session_label or "",
        session_reason=session.current_session_reason or "",
        current_session_date=session.current_session.isoformat() if session.current_session else "",
        previous_session_date=session.previous_completed_session.isoformat() if session.previous_completed_session else "",
    )

    # ── Market state ─────────────────────────────────────────────────────
    if session.is_premarket:
        report.market_state = "PRE_MARKET"
    elif session.is_regular_session:
        report.market_state = "MARKET_OPEN"
    elif session.is_after_hours:
        report.market_state = "AFTER_HOURS"
    else:
        report.market_state = "CLOSED"

    # ── Data availability scan ───────────────────────────────────────────
    if symbols:
        availability = _scan_data_availability(symbols, max_symbols=max_scan_symbols)
        report.symbols_checked = availability["checked"]
        report.quotes_available = availability["quotes"]
        report.ohlcv_available = availability["ohlcv"]
        if availability["checked"] > 0:
            report.quote_coverage_pct = round(
                availability["quotes"] / availability["checked"] * 100.0, 1
            )
            report.ohlcv_coverage_pct = round(
                availability["ohlcv"] / availability["checked"] * 100.0, 1
            )

    # ── Run mode recommendation ─────────────────────────────────────────
    if report.market_state == "MARKET_OPEN":
        if report.quote_coverage_pct >= 50.0:
            report.run_mode = "FULL"
            report.data_mode = "LIVE"
        elif report.quote_coverage_pct >= 20.0:
            report.run_mode = "DEGRADED"
            report.data_mode = "MIXED"
        else:
            report.run_mode = "DEGRADED"
            report.data_mode = "EOD_ONLY"
    elif report.market_state == "PRE_MARKET":
        report.run_mode = "DEGRADED"
        report.data_mode = "MIXED" if report.quote_coverage_pct >= 30.0 else "EOD_ONLY"
    elif report.market_state == "AFTER_HOURS":
        report.run_mode = "AFTER_MARKET"
        report.data_mode = "EOD_ONLY"
    else:  # CLOSED
        report.run_mode = "AFTER_MARKET"
        report.data_mode = "EOD_ONLY"

    # ── Persist ──────────────────────────────────────────────────────────
    if not dry_run:
        _write_report(report)

    return report


def _write_report(report: PreflightReport) -> Path | None:
    try:
        PREFLIGHT_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = PREFLIGHT_ARTIFACT_DIR / "preflight.json"
        fd, tmp = tempfile.mkstemp(
            prefix=".preflight.", suffix=".tmp", dir=str(PREFLIGHT_ARTIFACT_DIR)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def print_preflight(report: PreflightReport) -> None:
    """Print a human-readable preflight summary."""
    print()
    print("=" * 50)
    print("  Market Data Preflight Check")
    print("=" * 50)
    print(f"  Market State:       {report.market_state}")
    print(f"  Trading Day:        {'Yes' if report.is_trading_day else 'No'}")
    print(f"  Session:            {report.session_label or 'N/A'}")
    if report.session_reason:
        print(f"  Session Reason:     {report.session_reason}")
    if report.current_session_date:
        print(f"  Current Session:    {report.current_session_date}")
    if report.previous_session_date:
        print(f"  Previous Session:   {report.previous_session_date}")
    print(f"  Run Mode:           {report.run_mode}")
    print(f"  Data Mode:          {report.data_mode}")
    print("-" * 50)
    print(f"  Symbols Checked:    {report.symbols_checked}")
    print(f"  Quotes Available:   {report.quotes_available} ({report.quote_coverage_pct}%)")
    print(f"  OHLCV Available:    {report.ohlcv_available} ({report.ohlcv_coverage_pct}%)")
    print("=" * 50)
    print()

    if report.run_mode in {"AFTER_MARKET", "EOD_ONLY"}:
        print("  ⚠️  RUN_MODE={} — DATA_MODE={}".format(report.run_mode, report.data_mode))
        print("  Live quotes are not available. Selector will use EOD data only.")
        print("  Preview candidates may be generated. Formal TOP requires market-open run.")
        print()
