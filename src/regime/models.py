"""Market Regime data models — advisory only, never touches trading state."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.runtime_paths import resolve_artifacts_dir


PROJECT_DIR = Path(__file__).resolve().parents[2]
REGIME_ARTIFACT_DIR = resolve_artifacts_dir(PROJECT_DIR) / "regime"

REGIME_TYPES = ("BULL", "SIDEWAYS", "BEAR", "RISK_OFF")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class MarketSnapshot:
    """A single point-in-time snapshot of a market index."""
    symbol: str
    price: float
    ma20: float = 0.0
    ma50: float = 0.0
    ma200: float = 0.0
    rsi: float = 50.0
    atr_pct: float = 0.0
    return_5d_pct: float = 0.0
    return_20d_pct: float = 0.0
    volume_sma20_ratio: float = 1.0  # current vol / avg_vol_20
    fetched_at: str = field(default_factory=_utc_now_iso)

    @property
    def price_vs_ma20(self) -> float:
        return round(self.price / self.ma20, 4) if self.ma20 > 0 else 1.0

    @property
    def price_vs_ma50(self) -> float:
        return round(self.price / self.ma50, 4) if self.ma50 > 0 else 1.0

    @property
    def price_vs_ma200(self) -> float:
        return round(self.price / self.ma200, 4) if self.ma200 > 0 else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "price": self.price,
            "ma20": self.ma20, "ma50": self.ma50, "ma200": self.ma200,
            "rsi": self.rsi, "atr_pct": self.atr_pct,
            "return_5d_pct": self.return_5d_pct,
            "return_20d_pct": self.return_20d_pct,
            "volume_sma20_ratio": self.volume_sma20_ratio,
            "price_vs_ma20": self.price_vs_ma20,
            "price_vs_ma50": self.price_vs_ma50,
            "price_vs_ma200": self.price_vs_ma200,
            "fetched_at": self.fetched_at,
        }


@dataclass(slots=True)
class RegimeState:
    """The current market regime classification."""
    date: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    regime: str = "SIDEWAYS"  # BULL | SIDEWAYS | BEAR | RISK_OFF
    confidence: float = 0.0
    bull_score: float = 0.0
    sideways_score: float = 0.0
    bear_score: float = 0.0
    risk_off_score: float = 0.0
    signals: list[str] = field(default_factory=list)
    indices: dict[str, dict[str, Any]] = field(default_factory=dict)
    vix: float = 0.0
    vix_change_pct: float = 0.0
    advance_decline_ratio: float = 1.0
    new_high_low_ratio: float = 1.0
    market_return_5d: float = 0.0
    market_return_20d: float = 0.0
    generated_at: str = field(default_factory=_utc_now_iso)
    version: str = "v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date, "regime": self.regime,
            "confidence": self.confidence,
            "bull_score": self.bull_score,
            "sideways_score": self.sideways_score,
            "bear_score": self.bear_score,
            "risk_off_score": self.risk_off_score,
            "signals": list(self.signals),
            "indices": {
                k: v if isinstance(v, dict) else v.to_dict()
                for k, v in self.indices.items()
            },
            "vix": self.vix, "vix_change_pct": self.vix_change_pct,
            "advance_decline_ratio": self.advance_decline_ratio,
            "new_high_low_ratio": self.new_high_low_ratio,
            "market_return_5d": self.market_return_5d,
            "market_return_20d": self.market_return_20d,
            "generated_at": self.generated_at, "version": self.version,
        }


@dataclass(slots=True)
class RegimeHistory:
    """Historical record of regime states for analysis."""
    records: list[RegimeState] = field(default_factory=list)

    def add(self, state: RegimeState) -> None:
        self.records.append(state)
        # Keep last 365 days
        if len(self.records) > 365:
            self.records = self.records[-365:]

    def regime_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.records:
            counts[r.regime] = counts.get(r.regime, 0) + 1
        return counts

    def last_n(self, n: int = 30) -> list[RegimeState]:
        return self.records[-n:]
