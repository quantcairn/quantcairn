"""
Instrument profile: provides risk metadata per instrument type.

Leveraged / inverse ETFs get tighter limits than ordinary equities.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Known leveraged and inverse ETFs with their characteristics.
# Additional symbols can be added at runtime via environment variable
# SOXS_LEVERAGED_ETF_SYMBOLS (comma-separated).
LEVERAGED_ETF_REGISTRY: dict[str, dict[str, Any]] = {
    # ---- 3x Semiconductor ----
    "SOXL": {"leverage": 3, "inverse": False, "sector": "semiconductor"},
    "SOXS": {"leverage": 3, "inverse": True,  "sector": "semiconductor"},
    # ---- 3x Biotechnology ----
    "LABU": {"leverage": 3, "inverse": False, "sector": "biotechnology"},
    "LABD": {"leverage": 3, "inverse": True,  "sector": "biotechnology"},
    # ---- 3x Nasdaq ----
    "TQQQ": {"leverage": 3, "inverse": False, "sector": "nasdaq"},
    "SQQQ": {"leverage": 3, "inverse": True,  "sector": "nasdaq"},
    # ---- 3x Small Cap ----
    "TNA":  {"leverage": 3, "inverse": False, "sector": "small_cap"},
    "TZA":  {"leverage": 3, "inverse": True,  "sector": "small_cap"},
    # ---- 3x Financial ----
    "FAS":  {"leverage": 3, "inverse": False, "sector": "financial"},
    "FAZ":  {"leverage": 3, "inverse": True,  "sector": "financial"},
    # ---- 3x Energy ----
    "GUSH": {"leverage": 3, "inverse": False, "sector": "energy"},
    "DRIP": {"leverage": 3, "inverse": True,  "sector": "energy"},
    # ---- 2x China ----
    "YINN": {"leverage": 2, "inverse": False, "sector": "china"},
    "YANG": {"leverage": 2, "inverse": True,  "sector": "china"},
    # ---- 3x Homebuilders ----
    "NAIL": {"leverage": 3, "inverse": False, "sector": "homebuilders"},
    # ---- 3x Regional Banks ----
    "DPST": {"leverage": 3, "inverse": False, "sector": "regional_banks"},
    # ---- 1.5x/2x Volatility ----
    "UVXY": {"leverage": 1.5, "inverse": False, "sector": "volatility"},
    # ---- 3x S&P 500 ----
    "SPXL": {"leverage": 3, "inverse": False, "sector": "sp500"},
    "SPXS": {"leverage": 3, "inverse": True,  "sector": "sp500"},
    # ---- 2x Natural Gas ----
    "BOIL": {"leverage": 2, "inverse": False, "sector": "natural_gas"},
    "KOLD": {"leverage": 2, "inverse": True,  "sector": "natural_gas"},
}

DEFAULT_PROFILE: dict[str, Any] = {
    "instrument_type": "equity",
    "leverage_factor": 1,
    "inverse": False,
    "overnight_allowed": True,
    "max_position_pct": 0.30,        # max 30% of equity in a single position
    "max_total_group_exposure": 0.80, # max 80% of equity in total positions
    "max_daily_loss_pct": 0.06,       # max 6% daily loss
    "reduce_only_allowed": False,     # reduce_only is not forced
}

LEVERAGED_ETF_PROFILE: dict[str, Any] = {
    "instrument_type": "leveraged_etf",
    "leverage_factor": 3,
    "inverse": False,        # overridden per symbol
    "overnight_allowed": False,
    "max_position_pct": 0.15,           # max 15% of equity single position
    "max_total_group_exposure": 0.50,   # max 50% in all leveraged ETFs combined
    "max_daily_loss_pct": 0.03,         # max 3% daily loss per ETF
    "reduce_only_allowed": True,
}


def _extra_leveraged_symbols() -> set[str]:
    """Read additional leveraged ETF symbols from env var."""
    raw = os.environ.get("SOXS_LEVERAGED_ETF_SYMBOLS", "").strip()
    if not raw:
        return set()
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def get_profile(ticker: str) -> dict[str, Any]:
    """Return the risk profile for *ticker*.

    Leveraged/inverse ETFs listed in LEVERAGED_ETF_REGISTRY (or the env
    var override) receive a tightened profile.  Everything else gets the
    default equity profile.
    """
    import os
    symbol = str(ticker or "").strip().upper().split(".")[0]
    if not symbol:
        return dict(DEFAULT_PROFILE)

    # Check registry first, then env override
    registry = LEVERAGED_ETF_REGISTRY
    extra = _extra_leveraged_symbols()
    known = symbol in registry or symbol in extra

    if not known:
        return dict(DEFAULT_PROFILE)

    meta = registry.get(symbol, {"leverage": 3, "inverse": False, "sector": "other"})
    profile = dict(LEVERAGED_ETF_PROFILE)
    profile["inverse"] = bool(meta.get("inverse", False))
    # Use a lower max-position cap for higher-leverage names.
    lev = float(meta.get("leverage", 3) or 3)
    profile["leverage_factor"] = lev
    if lev >= 3:
        profile["max_position_pct"] = 0.15
    elif lev >= 2:
        profile["max_position_pct"] = 0.20
    else:
        profile["max_position_pct"] = 0.25
    return profile


def is_leveraged_etf(ticker: str) -> bool:
    """Quick check — is *ticker* a known leveraged/inverse ETF?"""
    symbol = str(ticker or "").strip().upper().split(".")[0]
    if not symbol:
        return False
    return symbol in LEVERAGED_ETF_REGISTRY or symbol in _extra_leveraged_symbols()
