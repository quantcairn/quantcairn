"""Managed Universe data models.  Controls candidate availability only — never touches trading state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[2]
UNIVERSE_ARTIFACT_DIR = PROJECT_DIR / "artifacts" / "universe"

ASSET_TYPES = (
    "index_etf", "common_stock", "sector_etf",
    "leveraged_etf", "inverse_etf", "mega_cap",
)
SECTORS = (
    "technology", "financial", "energy", "healthcare",
    "consumer", "industrial", "semiconductor",
)
LEVERAGE_TYPES = ("1x", "2x", "3x", "-1x", "-2x", "-3x")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class UniverseSymbol:
    """A single symbol in the managed universe with its profile."""

    symbol: str
    name: str = ""
    asset_type: str = "common_stock"
    sector: str = ""
    benchmark: str = "SPY"
    leverage_type: str = "1x"
    min_price: float = 4.0
    min_avg_volume: int = 500_000
    min_market_cap: float = 500_000_000
    enabled: bool = True
    disabled_reason: str = ""
    liquidity_score: float = 0.0
    risk_score: float = 50.0
    volatility_score: float = 50.0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name,
            "asset_type": self.asset_type, "sector": self.sector,
            "benchmark": self.benchmark, "leverage_type": self.leverage_type,
            "min_price": self.min_price, "min_avg_volume": self.min_avg_volume,
            "min_market_cap": self.min_market_cap,
            "enabled": self.enabled, "disabled_reason": self.disabled_reason,
            "liquidity_score": self.liquidity_score,
            "risk_score": self.risk_score,
            "volatility_score": self.volatility_score,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UniverseSymbol":
        def _f(v: Any, default: float = 0.0) -> float:
            try: return float(v or default)
            except (TypeError, ValueError): return default
        def _i(v: Any, default: int = 0) -> int:
            try: return int(float(v or default))
            except (TypeError, ValueError): return default
        def _b(v: Any, default: bool = True) -> bool:
            if v is None: return default
            if isinstance(v, bool): return v
            if isinstance(v, str): return v.strip().lower() in {"true", "1", "yes", "on"}
            return bool(v)

        return cls(
            symbol=str(d.get("symbol") or "").strip().upper(),
            name=str(d.get("name") or ""),
            asset_type=str(d.get("asset_type") or "common_stock"),
            sector=str(d.get("sector") or ""),
            benchmark=str(d.get("benchmark") or "SPY"),
            leverage_type=str(d.get("leverage_type") or "1x"),
            min_price=_f(d.get("min_price"), 4.0),
            min_avg_volume=_i(d.get("min_avg_volume"), 500_000),
            min_market_cap=_f(d.get("min_market_cap"), 500_000_000),
            enabled=_b(d.get("enabled"), True),
            disabled_reason=str(d.get("disabled_reason") or ""),
            liquidity_score=_f(d.get("liquidity_score")),
            risk_score=_f(d.get("risk_score"), 50.0),
            volatility_score=_f(d.get("volatility_score"), 50.0),
            tags=list(d.get("tags") or []),
        )

    @property
    def is_leveraged(self) -> bool:
        return self.leverage_type not in {"1x", ""}

    @property
    def is_inverse(self) -> bool:
        return self.asset_type == "inverse_etf" or self.leverage_type.startswith("-")

    def disable(self, reason: str = "") -> None:
        self.enabled = False
        self.disabled_reason = reason

    def enable(self) -> None:
        self.enabled = True
        self.disabled_reason = ""


@dataclass(slots=True)
class UniverseSnapshot:
    """Complete universe state snapshot for persistence."""

    version: str = "v1"
    generated_at: str = field(default_factory=_utc_now_iso)
    total_symbols: int = 0
    enabled_symbols: int = 0
    symbols: list[UniverseSymbol] = field(default_factory=list)
    filter_results: dict[str, Any] = field(default_factory=dict)
    composition: dict[str, int] = field(default_factory=dict)
    max_leveraged_inverse: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "total_symbols": self.total_symbols,
            "enabled_symbols": self.enabled_symbols,
            "symbols": [s.to_dict() for s in self.symbols],
            "filter_results": dict(self.filter_results),
            "composition": dict(self.composition),
            "max_leveraged_inverse": self.max_leveraged_inverse,
        }

    def compute_composition(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.symbols:
            if s.enabled:
                at = s.asset_type
                counts[at] = counts.get(at, 0) + 1
        self.composition = counts
        return counts
