from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REGIME_NAMES = (
    "UNKNOWN",
    "INVALID_RANGE",
    "HIGH_VOLATILITY",
    "STRONG_UPTREND",
    "STRONG_DOWNTREND",
    "RANGE",
)

ASSET_TYPES = (
    "common_stock",
    "index_etf",
    "leveraged_etf",
    "inverse_etf",
)

STRATEGY_ALIASES: dict[str, str] = {
    "equity_mean_reversion": "mean_reversion",
    "inverse_etf_range": "inverse_range",
    "leveraged_etf_range": "leveraged_range",
    "range": "range_etf",
}


@dataclass(frozen=True)
class StrategyRule:
    strategy_id: str
    aliases: tuple[str, ...]
    allowed_regimes: tuple[str, ...]
    blocked_regimes: tuple[str, ...]
    allowed_asset_types: tuple[str, ...]
    requires_benchmark: bool = True
    requires_backtest: bool = True
    requires_walk_forward: bool = True
    min_liquidity_score: float = 0.0
    min_confidence: float = 0.55
    expected_edge_bias: float = 0.0
    description: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "aliases": list(self.aliases),
            "allowed_regimes": list(self.allowed_regimes),
            "blocked_regimes": list(self.blocked_regimes),
            "allowed_asset_types": list(self.allowed_asset_types),
            "requires_benchmark": self.requires_benchmark,
            "requires_backtest": self.requires_backtest,
            "requires_walk_forward": self.requires_walk_forward,
            "min_liquidity_score": self.min_liquidity_score,
            "min_confidence": self.min_confidence,
            "expected_edge_bias": self.expected_edge_bias,
            "description": self.description,
            "notes": list(self.notes),
        }


STRATEGY_REGISTRY: dict[str, StrategyRule] = {
    "trend_following": StrategyRule(
        strategy_id="trend_following",
        aliases=("trend", "momentum"),
        allowed_regimes=("STRONG_UPTREND", "RANGE"),
        blocked_regimes=("STRONG_DOWNTREND", "HIGH_VOLATILITY", "INVALID_RANGE"),
        allowed_asset_types=("common_stock", "index_etf"),
        requires_benchmark=True,
        requires_backtest=True,
        requires_walk_forward=True,
        min_liquidity_score=60.0,
        min_confidence=0.60,
        expected_edge_bias=0.08,
        description="Follow persistent upward or orderly range trends.",
        notes=("requires_ma_stack", "requires_relative_strength"),
    ),
    "mean_reversion": StrategyRule(
        strategy_id="mean_reversion",
        aliases=("equity_mean_reversion", "reversion"),
        allowed_regimes=("RANGE", "HIGH_VOLATILITY"),
        blocked_regimes=("STRONG_UPTREND", "STRONG_DOWNTREND", "INVALID_RANGE"),
        allowed_asset_types=("common_stock", "index_etf"),
        requires_benchmark=True,
        requires_backtest=True,
        requires_walk_forward=True,
        min_liquidity_score=55.0,
        min_confidence=0.60,
        expected_edge_bias=0.05,
        description="Trade reversion inside a controlled range.",
        notes=("requires_support_resistance", "requires_spread_control"),
    ),
    "index_follow": StrategyRule(
        strategy_id="index_follow",
        aliases=("benchmark_follow", "index"),
        allowed_regimes=("STRONG_UPTREND", "RANGE"),
        blocked_regimes=("STRONG_DOWNTREND", "HIGH_VOLATILITY", "INVALID_RANGE"),
        allowed_asset_types=("index_etf",),
        requires_benchmark=True,
        requires_backtest=True,
        requires_walk_forward=True,
        min_liquidity_score=50.0,
        min_confidence=0.58,
        expected_edge_bias=0.04,
        description="Track broad index direction with lower friction.",
        notes=("benchmark_driven",),
    ),
    "range_etf": StrategyRule(
        strategy_id="range_etf",
        aliases=("etf_range",),
        allowed_regimes=("RANGE", "HIGH_VOLATILITY"),
        blocked_regimes=("INVALID_RANGE",),
        allowed_asset_types=("index_etf", "leveraged_etf", "inverse_etf"),
        requires_benchmark=True,
        requires_backtest=True,
        requires_walk_forward=True,
        min_liquidity_score=55.0,
        min_confidence=0.55,
        expected_edge_bias=0.03,
        description="Range behavior on ETF baskets with benchmark confirmation.",
        notes=("etf_only",),
    ),
    "inverse_range": StrategyRule(
        strategy_id="inverse_range",
        aliases=("inverse_etf_range", "inverse"),
        allowed_regimes=("STRONG_UPTREND", "HIGH_VOLATILITY"),
        blocked_regimes=("INVALID_RANGE",),
        allowed_asset_types=("inverse_etf",),
        requires_benchmark=True,
        requires_backtest=True,
        requires_walk_forward=True,
        min_liquidity_score=55.0,
        min_confidence=0.60,
        expected_edge_bias=0.06,
        description="Inverse ETF relative-value / range research.",
        notes=("inverse_specific", "requires_strict_risk"),
    ),
    "leveraged_range": StrategyRule(
        strategy_id="leveraged_range",
        aliases=("leveraged_etf_range", "leveraged"),
        allowed_regimes=("RANGE", "HIGH_VOLATILITY"),
        blocked_regimes=("INVALID_RANGE", "STRONG_DOWNTREND"),
        allowed_asset_types=("leveraged_etf",),
        requires_benchmark=True,
        requires_backtest=True,
        requires_walk_forward=True,
        min_liquidity_score=60.0,
        min_confidence=0.62,
        expected_edge_bias=0.05,
        description="Leveraged ETF range research with tighter risk controls.",
        notes=("leveraged_specific", "requires_strict_risk"),
    ),
    "benchmark": StrategyRule(
        strategy_id="benchmark",
        aliases=("benchmark_only",),
        allowed_regimes=("RANGE", "STRONG_UPTREND", "STRONG_DOWNTREND", "HIGH_VOLATILITY"),
        blocked_regimes=("INVALID_RANGE",),
        allowed_asset_types=ASSET_TYPES,
        requires_benchmark=False,
        requires_backtest=False,
        requires_walk_forward=False,
        min_liquidity_score=0.0,
        min_confidence=0.0,
        expected_edge_bias=0.0,
        description="Research-only benchmark anchor or diagnostic path.",
        notes=("diagnostic_only",),
    ),
}


def normalize_strategy_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return STRATEGY_ALIASES.get(text, text)


def strategy_rule_for(value: Any) -> StrategyRule | None:
    strategy_id = normalize_strategy_id(value)
    if not strategy_id:
        return None
    return STRATEGY_REGISTRY.get(strategy_id)


def strategy_registry_snapshot() -> list[dict[str, Any]]:
    return [rule.to_dict() for rule in STRATEGY_REGISTRY.values()]
