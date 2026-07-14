from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.shadow.universe import (
    SHADOW_SYMBOL_CATALOG,
    canonical_shadow_symbol,
    default_benchmarks_for,
    shadow_title_for,
    strategy_family_for,
    symbol_class_for,
)


class ValidationStatus(str, Enum):
    AI_CANDIDATE = "AI_CANDIDATE"
    CLASSIFIED = "CLASSIFIED"
    BENCHMARK_ASSIGNED = "BENCHMARK_ASSIGNED"
    STRATEGY_ASSIGNED = "STRATEGY_ASSIGNED"
    PENDING_DATA_VALIDATION = "PENDING_DATA_VALIDATION"
    DATA_VALID = "DATA_VALID"
    DATA_INVALID = "DATA_INVALID"
    PENDING_BACKTEST = "PENDING_BACKTEST"
    BACKTEST_COMPLETE = "BACKTEST_COMPLETE"
    BACKTEST_FAILED = "BACKTEST_FAILED"
    PENDING_WALK_FORWARD = "PENDING_WALK_FORWARD"
    WALK_FORWARD_COMPLETE = "WALK_FORWARD_COMPLETE"
    WALK_FORWARD_FAILED = "WALK_FORWARD_FAILED"
    PENDING_SHADOW = "PENDING_SHADOW"
    SHADOW_OBSERVING = "SHADOW_OBSERVING"
    SHADOW_COMPLETE = "SHADOW_COMPLETE"
    PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
    PAPER_INELIGIBLE = "PAPER_INELIGIBLE"
    LIVE_ELIGIBLE = "LIVE_ELIGIBLE"
    LIVE_INELIGIBLE = "LIVE_INELIGIBLE"
    REJECTED = "REJECTED"


class EvidenceStatus(str, Enum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    PENDING = "PENDING"


class ProfitabilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    PENDING = "PENDING"


class DeploymentStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    PENDING = "PENDING"


ALLOWED_SYMBOL_CLASSES = {"common_stock", "index_etf", "leveraged_etf", "inverse_etf"}
ALLOWED_RISK_PROFILES = {"balanced", "strict", "very_strict"}
ALLOWED_TIMEFRAMES = {"5m", "15m", "30m", "1h", "daily"}

_ALLOWED_TRANSITIONS: dict[ValidationStatus, set[ValidationStatus]] = {
    ValidationStatus.AI_CANDIDATE: {ValidationStatus.CLASSIFIED, ValidationStatus.REJECTED},
    ValidationStatus.CLASSIFIED: {ValidationStatus.BENCHMARK_ASSIGNED, ValidationStatus.REJECTED},
    ValidationStatus.BENCHMARK_ASSIGNED: {ValidationStatus.STRATEGY_ASSIGNED, ValidationStatus.REJECTED},
    ValidationStatus.STRATEGY_ASSIGNED: {ValidationStatus.PENDING_DATA_VALIDATION, ValidationStatus.REJECTED},
    ValidationStatus.PENDING_DATA_VALIDATION: {
        ValidationStatus.DATA_VALID,
        ValidationStatus.DATA_INVALID,
        ValidationStatus.REJECTED,
    },
    ValidationStatus.DATA_VALID: {ValidationStatus.PENDING_BACKTEST, ValidationStatus.REJECTED},
    ValidationStatus.DATA_INVALID: {ValidationStatus.REJECTED},
    ValidationStatus.PENDING_BACKTEST: {
        ValidationStatus.BACKTEST_COMPLETE,
        ValidationStatus.BACKTEST_FAILED,
        ValidationStatus.REJECTED,
    },
    ValidationStatus.BACKTEST_COMPLETE: {ValidationStatus.PENDING_WALK_FORWARD, ValidationStatus.REJECTED},
    ValidationStatus.BACKTEST_FAILED: {ValidationStatus.REJECTED},
    ValidationStatus.PENDING_WALK_FORWARD: {
        ValidationStatus.WALK_FORWARD_COMPLETE,
        ValidationStatus.WALK_FORWARD_FAILED,
        ValidationStatus.REJECTED,
    },
    ValidationStatus.WALK_FORWARD_COMPLETE: {ValidationStatus.PENDING_SHADOW, ValidationStatus.REJECTED},
    ValidationStatus.WALK_FORWARD_FAILED: {ValidationStatus.REJECTED},
    ValidationStatus.PENDING_SHADOW: {ValidationStatus.SHADOW_OBSERVING, ValidationStatus.REJECTED},
    ValidationStatus.SHADOW_OBSERVING: {ValidationStatus.SHADOW_COMPLETE, ValidationStatus.REJECTED},
    ValidationStatus.SHADOW_COMPLETE: {
        ValidationStatus.PAPER_ELIGIBLE,
        ValidationStatus.PAPER_INELIGIBLE,
        ValidationStatus.REJECTED,
    },
    ValidationStatus.PAPER_ELIGIBLE: {ValidationStatus.LIVE_ELIGIBLE, ValidationStatus.LIVE_INELIGIBLE, ValidationStatus.REJECTED},
    ValidationStatus.PAPER_INELIGIBLE: {ValidationStatus.REJECTED},
    ValidationStatus.LIVE_ELIGIBLE: {ValidationStatus.REJECTED},
    ValidationStatus.LIVE_INELIGIBLE: {ValidationStatus.REJECTED},
    ValidationStatus.REJECTED: set(),
}


PROJECT_DIR = Path(__file__).resolve().parents[2]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_symbol(value: Any) -> str:
    return canonical_shadow_symbol(_normalize_text(value)).upper()


def _normalize_benchmarks(values: Any) -> tuple[str, ...]:
    items: list[str] = []
    if isinstance(values, (list, tuple, set, frozenset)):
        for item in values:
            symbol = _normalize_symbol(item)
            if symbol:
                items.append(symbol)
    elif values:
        symbol = _normalize_symbol(values)
        if symbol:
            items.append(symbol)
    seen: set[str] = set()
    normalized: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return tuple(normalized)


def _symbol_market(symbol: str) -> str:
    cleaned = _normalize_symbol(symbol)
    if "." in cleaned:
        return cleaned.split(".")[-1]
    return "US"


def _default_asset_type(symbol: str) -> str:
    key = _normalize_symbol(symbol)
    if key in SHADOW_SYMBOL_CATALOG:
        return str(symbol_class_for(key) or "").strip()
    return ""


def _default_strategy_family(symbol: str) -> str:
    key = _normalize_symbol(symbol)
    if key in SHADOW_SYMBOL_CATALOG:
        return str(strategy_family_for(key) or "").strip()
    return ""


def _default_risk_profile(symbol: str, asset_type: str | None = None) -> str:
    asset = str(asset_type or _default_asset_type(symbol) or "").strip().lower()
    if asset in {"leveraged_etf", "inverse_etf"}:
        key = _normalize_symbol(symbol)
        if key in SHADOW_SYMBOL_CATALOG:
            entry = SHADOW_SYMBOL_CATALOG.get(key, {})
            return str(entry.get("risk_profile") or "strict").strip().lower()
        return "strict"
    key = _normalize_symbol(symbol)
    if key in SHADOW_SYMBOL_CATALOG:
        entry = SHADOW_SYMBOL_CATALOG.get(key, {})
        return str(entry.get("risk_profile") or "balanced").strip().lower()
    return ""


def _trade_admission_status(validation_status: str) -> str:
    status = _normalize_text(validation_status).upper()
    if status in {ValidationStatus.PAPER_ELIGIBLE.value, ValidationStatus.LIVE_ELIGIBLE.value}:
        return status
    if status in {
        ValidationStatus.PAPER_INELIGIBLE.value,
        ValidationStatus.LIVE_INELIGIBLE.value,
        ValidationStatus.REJECTED.value,
    }:
        return "NOT_TRADABLE"
    return "NOT_TRADABLE"


@dataclass(slots=True)
class CandidateRecord:
    candidate_id: str
    symbol: str
    market: str = "US"
    selected_at: str = ""
    source: str = "ai_selector"
    ai_score: float | None = None
    candidate_score: float | None = None
    liquidity_score: float | None = None
    trend_score: float | None = None
    volatility_score: float | None = None
    risk_score: float | None = None
    strategy_fit_score: float | None = None
    recommended_strategy: str = ""
    score_reason: str = ""
    data_mode: str = ""
    data_freshness: str = ""
    data_status: str = ""
    scoring_eligible: bool = False
    scoring_block_reason: str = ""
    missing_fields: tuple[str, ...] = ()
    candidate_fallback: bool = False
    fallback_sources: tuple[str, ...] = ()
    mock_used: bool = False
    mock_sources: tuple[str, ...] = ()
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()
    ai_reason: str = ""
    asset_type: str = ""
    benchmarks: tuple[str, ...] = ()
    strategy_family: str = ""
    risk_profile: str = ""
    timeframe: str = "15m"
    validation_status: ValidationStatus = ValidationStatus.AI_CANDIDATE
    evidence_status: EvidenceStatus = EvidenceStatus.INSUFFICIENT_EVIDENCE
    profitability_status: ProfitabilityStatus = ProfitabilityStatus.INELIGIBLE
    deployment_status: DeploymentStatus = DeploymentStatus.INELIGIBLE
    trading_enabled: bool = False
    shadow_enabled: bool = False
    paper_enabled: bool = False
    live_enabled: bool = False
    rejection_reason: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.candidate_id = _normalize_text(self.candidate_id) or self._build_candidate_id()
        self.symbol = _normalize_symbol(self.symbol)
        self.market = _normalize_text(self.market).upper() or _symbol_market(self.symbol)
        self.selected_at = _normalize_text(self.selected_at)
        self.source = _normalize_text(self.source) or "ai_selector"
        self.candidate_score = self._coerce_float(self.candidate_score)
        self.liquidity_score = self._coerce_float(self.liquidity_score)
        self.trend_score = self._coerce_float(self.trend_score)
        self.volatility_score = self._coerce_float(self.volatility_score)
        self.risk_score = self._coerce_float(self.risk_score)
        self.strategy_fit_score = self._coerce_float(self.strategy_fit_score)
        self.recommended_strategy = _normalize_text(self.recommended_strategy)
        self.score_reason = _normalize_text(self.score_reason)
        self.data_mode = _normalize_text(self.data_mode).lower()
        self.data_freshness = _normalize_text(self.data_freshness).lower()
        self.data_status = _normalize_text(self.data_status).upper()
        self.scoring_eligible = bool(self.scoring_eligible)
        self.scoring_block_reason = _normalize_text(self.scoring_block_reason)
        if isinstance(self.missing_fields, (list, tuple, set, frozenset)):
            self.missing_fields = tuple(_normalize_text(item) for item in self.missing_fields if _normalize_text(item))
        else:
            text = _normalize_text(self.missing_fields)
            self.missing_fields = (text,) if text else ()
        self.candidate_fallback = bool(self.candidate_fallback)
        self.fallback_sources = tuple(
            _normalize_text(item)
            for item in self.fallback_sources
            if _normalize_text(item)
        ) if isinstance(self.fallback_sources, (list, tuple, set, frozenset)) else ((_normalize_text(self.fallback_sources),) if _normalize_text(self.fallback_sources) else ())
        self.mock_used = bool(self.mock_used)
        self.mock_sources = tuple(
            _normalize_text(item)
            for item in self.mock_sources
            if _normalize_text(item)
        ) if isinstance(self.mock_sources, (list, tuple, set, frozenset)) else ((_normalize_text(self.mock_sources),) if _normalize_text(self.mock_sources) else ())
        self.degraded = bool(self.degraded)
        self.degradation_reasons = tuple(
            _normalize_text(item)
            for item in self.degradation_reasons
            if _normalize_text(item)
        ) if isinstance(self.degradation_reasons, (list, tuple, set, frozenset)) else ((_normalize_text(self.degradation_reasons),) if _normalize_text(self.degradation_reasons) else ())
        self.ai_reason = _normalize_text(self.ai_reason)
        self.asset_type = _normalize_text(self.asset_type).lower()
        self.benchmarks = _normalize_benchmarks(self.benchmarks)
        self.strategy_family = _normalize_text(self.strategy_family)
        self.risk_profile = _normalize_text(self.risk_profile).lower()
        self.timeframe = _normalize_text(self.timeframe).lower() or "15m"
        self.validation_status = self._coerce_status(self.validation_status, ValidationStatus.AI_CANDIDATE)
        self.evidence_status = self._coerce_status(self.evidence_status, EvidenceStatus.INSUFFICIENT_EVIDENCE)
        self.profitability_status = self._coerce_status(self.profitability_status, ProfitabilityStatus.INELIGIBLE)
        self.deployment_status = self._coerce_status(self.deployment_status, DeploymentStatus.INELIGIBLE)
        self.trading_enabled = bool(self.trading_enabled)
        self.shadow_enabled = bool(self.shadow_enabled)
        self.paper_enabled = bool(self.paper_enabled)
        self.live_enabled = bool(self.live_enabled)
        self.rejection_reason = _normalize_text(self.rejection_reason)
        self.created_at = _normalize_text(self.created_at) or _utc_now_iso()
        self.updated_at = _normalize_text(self.updated_at) or self.created_at
        if not isinstance(self.metadata, dict):
            self.metadata = {}

    def _build_candidate_id(self) -> str:
        symbol = self.symbol or "CANDIDATE"
        stamp = (self.selected_at or self.created_at or _utc_now_iso()).replace(":", "").replace("-", "").replace("+", "")
        return f"cand_{symbol.replace('.', '_')}_{stamp}_{uuid4().hex[:8]}"

    @staticmethod
    def _coerce_status(value: Any, default: Enum) -> str:
        if isinstance(value, Enum):
            return str(value.value)
        text = _normalize_text(value)
        return text or str(default.value)

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or number in {float("inf"), float("-inf")}:
            return None
        return round(number, 2)

    @classmethod
    def from_ai_candidate(
        cls,
        *,
        symbol: str,
        selected_at: str,
        source: str = "ai_selector",
        ai_score: float | None = None,
        candidate_score: float | None = None,
        liquidity_score: float | None = None,
        trend_score: float | None = None,
        volatility_score: float | None = None,
        risk_score: float | None = None,
        strategy_fit_score: float | None = None,
        recommended_strategy: str = "",
        score_reason: str = "",
        data_mode: str = "",
        data_freshness: str = "",
        data_status: str = "",
        scoring_eligible: bool = False,
        scoring_block_reason: str = "",
        missing_fields: tuple[str, ...] | list[str] | None = None,
        candidate_fallback: bool = False,
        fallback_sources: tuple[str, ...] | list[str] | None = None,
        mock_used: bool = False,
        mock_sources: tuple[str, ...] | list[str] | None = None,
        degraded: bool = False,
        degradation_reasons: tuple[str, ...] | list[str] | None = None,
        ai_reason: str = "",
        asset_type: str | None = None,
        benchmarks: tuple[str, ...] | list[str] | None = None,
        strategy_family: str | None = None,
        risk_profile: str | None = None,
        timeframe: str = "15m",
        market: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "CandidateRecord":
        normalized_symbol = _normalize_symbol(symbol)
        normalized_asset = _normalize_text(asset_type).lower() if asset_type else _default_asset_type(normalized_symbol)
        normalized_benchmarks = _normalize_benchmarks(benchmarks or default_benchmarks_for(normalized_symbol))
        normalized_strategy_family = _normalize_text(strategy_family) if strategy_family is not None else _default_strategy_family(normalized_symbol)
        normalized_risk_profile = _normalize_text(risk_profile).lower() if risk_profile else _default_risk_profile(normalized_symbol, normalized_asset)
        return cls(
            candidate_id="",
            symbol=normalized_symbol,
            market=_normalize_text(market).upper() if market else _symbol_market(normalized_symbol),
            selected_at=_normalize_text(selected_at),
            source=source,
            ai_score=ai_score,
            candidate_score=candidate_score,
            liquidity_score=liquidity_score,
            trend_score=trend_score,
            volatility_score=volatility_score,
            risk_score=risk_score,
            strategy_fit_score=strategy_fit_score,
            recommended_strategy=recommended_strategy,
            score_reason=score_reason,
            data_mode=data_mode,
            data_freshness=data_freshness,
            data_status=data_status,
            scoring_eligible=scoring_eligible,
            scoring_block_reason=scoring_block_reason,
            missing_fields=tuple(missing_fields or ()),
            candidate_fallback=candidate_fallback,
            fallback_sources=tuple(fallback_sources or ()),
            mock_used=mock_used,
            mock_sources=tuple(mock_sources or ()),
            degraded=degraded,
            degradation_reasons=tuple(degradation_reasons or ()),
            ai_reason=ai_reason,
            asset_type=normalized_asset,
            benchmarks=normalized_benchmarks,
            strategy_family=normalized_strategy_family,
            risk_profile=normalized_risk_profile,
            timeframe=timeframe,
            validation_status=ValidationStatus.AI_CANDIDATE,
            evidence_status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
            profitability_status=ProfitabilityStatus.INELIGIBLE,
            deployment_status=DeploymentStatus.INELIGIBLE,
            trading_enabled=False,
            shadow_enabled=False,
            paper_enabled=False,
            live_enabled=False,
            rejection_reason="",
            metadata=dict(metadata or {}),
        )

    def is_leveraged_or_inverse(self) -> bool:
        return self.asset_type in {"leveraged_etf", "inverse_etf"}

    def validate_initial_state(self) -> list[str]:
        errors: list[str] = []
        if self.validation_status != ValidationStatus.AI_CANDIDATE.value:
            errors.append("validation_status must start at AI_CANDIDATE")
        if self.trading_enabled:
            errors.append("trading_enabled must be false")
        if self.shadow_enabled:
            errors.append("shadow_enabled must be false")
        if self.paper_enabled:
            errors.append("paper_enabled must be false")
        if self.live_enabled:
            errors.append("live_enabled must be false")
        if self.deployment_status != DeploymentStatus.INELIGIBLE.value:
            errors.append("deployment_status must be INELIGIBLE")
        if self.evidence_status not in {EvidenceStatus.INSUFFICIENT_EVIDENCE.value, EvidenceStatus.PENDING.value}:
            errors.append("evidence_status must start unavailable or pending")
        if self.profitability_status not in {ProfitabilityStatus.INELIGIBLE.value, ProfitabilityStatus.PENDING.value}:
            errors.append("profitability_status must start ineligible or pending")
        if self.timeframe and self.timeframe not in ALLOWED_TIMEFRAMES:
            errors.append("invalid_timeframe")
        if self.asset_type and self.asset_type not in ALLOWED_SYMBOL_CLASSES:
            errors.append("invalid_asset_type")
        if self.is_leveraged_or_inverse() and self.risk_profile not in {"strict", "very_strict"}:
            errors.append("missing_or_weak_risk_profile")
        return errors

    def clone(self, **changes: Any) -> "CandidateRecord":
        payload = self.to_dict()
        payload.update(changes)
        return CandidateRecord.from_dict(payload)

    def touch(self) -> None:
        self.updated_at = _utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "market": self.market,
            "selected_at": self.selected_at,
            "source": self.source,
            "ai_score": self.ai_score,
            "candidate_score": self.candidate_score,
            "liquidity_score": self.liquidity_score,
            "trend_score": self.trend_score,
            "volatility_score": self.volatility_score,
            "risk_score": self.risk_score,
            "strategy_fit_score": self.strategy_fit_score,
            "recommended_strategy": self.recommended_strategy,
            "score_reason": self.score_reason,
            "data_mode": self.data_mode,
            "data_freshness": self.data_freshness,
            "data_status": self.data_status,
            "scoring_eligible": self.scoring_eligible,
            "scoring_block_reason": self.scoring_block_reason,
            "missing_fields": list(self.missing_fields),
            "candidate_fallback": self.candidate_fallback,
            "fallback_sources": list(self.fallback_sources),
            "mock_used": self.mock_used,
            "mock_sources": list(self.mock_sources),
            "degraded": self.degraded,
            "degradation_reasons": list(self.degradation_reasons),
            "ai_reason": self.ai_reason,
            "asset_type": self.asset_type,
            "benchmarks": list(self.benchmarks),
            "strategy_family": self.strategy_family,
            "risk_profile": self.risk_profile,
            "timeframe": self.timeframe,
            "validation_status": self.validation_status,
            "current_validation_status": self.validation_status,
            "trade_admission_status": _trade_admission_status(self.validation_status),
            "evidence_status": self.evidence_status,
            "profitability_status": self.profitability_status,
            "deployment_status": self.deployment_status,
            "trading_enabled": self.trading_enabled,
            "shadow_enabled": self.shadow_enabled,
            "paper_enabled": self.paper_enabled,
            "live_enabled": self.live_enabled,
            "rejection_reason": self.rejection_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateRecord":
        return cls(
            candidate_id=payload.get("candidate_id") or "",
            symbol=payload.get("symbol") or "",
            market=payload.get("market") or "US",
            selected_at=payload.get("selected_at") or "",
            source=payload.get("source") or "ai_selector",
            ai_score=payload.get("ai_score"),
            candidate_score=payload.get("candidate_score"),
            liquidity_score=payload.get("liquidity_score"),
            trend_score=payload.get("trend_score"),
            volatility_score=payload.get("volatility_score"),
            risk_score=payload.get("risk_score"),
            strategy_fit_score=payload.get("strategy_fit_score"),
            recommended_strategy=payload.get("recommended_strategy") or "",
            score_reason=payload.get("score_reason") or "",
            data_mode=payload.get("data_mode") or "",
            data_freshness=payload.get("data_freshness") or "",
            data_status=payload.get("data_status") or "",
            scoring_eligible=bool(payload.get("scoring_eligible", False)),
            scoring_block_reason=payload.get("scoring_block_reason") or "",
            missing_fields=tuple(payload.get("missing_fields") or ()),
            candidate_fallback=bool(payload.get("candidate_fallback", False)),
            fallback_sources=tuple(payload.get("fallback_sources") or ()),
            mock_used=bool(payload.get("mock_used", False)),
            mock_sources=tuple(payload.get("mock_sources") or ()),
            degraded=bool(payload.get("degraded", False)),
            degradation_reasons=tuple(payload.get("degradation_reasons") or ()),
            ai_reason=payload.get("ai_reason") or "",
            asset_type=payload.get("asset_type") or "",
            benchmarks=tuple(payload.get("benchmarks") or ()),
            strategy_family=payload.get("strategy_family") or "",
            risk_profile=payload.get("risk_profile") or "",
            timeframe=payload.get("timeframe") or "15m",
            validation_status=payload.get("validation_status") or ValidationStatus.AI_CANDIDATE.value,
            evidence_status=payload.get("evidence_status") or EvidenceStatus.INSUFFICIENT_EVIDENCE.value,
            profitability_status=payload.get("profitability_status") or ProfitabilityStatus.INELIGIBLE.value,
            deployment_status=payload.get("deployment_status") or DeploymentStatus.INELIGIBLE.value,
            trading_enabled=bool(payload.get("trading_enabled", False)),
            shadow_enabled=bool(payload.get("shadow_enabled", False)),
            paper_enabled=bool(payload.get("paper_enabled", False)),
            live_enabled=bool(payload.get("live_enabled", False)),
            rejection_reason=payload.get("rejection_reason") or "",
            created_at=payload.get("created_at") or _utc_now_iso(),
            updated_at=payload.get("updated_at") or payload.get("created_at") or _utc_now_iso(),
            metadata=dict(payload.get("metadata") or {}),
        )

    def display_title(self) -> str:
        return shadow_title_for(self.symbol, self.timeframe)

    def summary_row(self) -> dict[str, Any]:
        metadata = dict(self.metadata or {})
        current_validation_status = self.validation_status
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "market": self.market,
            "ai_score": self.ai_score,
            "candidate_score": self.candidate_score,
            "liquidity_score": self.liquidity_score,
            "trend_score": self.trend_score,
            "volatility_score": self.volatility_score,
            "risk_score": self.risk_score,
            "strategy_fit_score": self.strategy_fit_score,
            "recommended_strategy": self.recommended_strategy,
            "score_reason": self.score_reason,
            "data_mode": self.data_mode or metadata.get("data_mode") or "",
            "data_freshness": self.data_freshness or metadata.get("data_freshness") or "",
            "data_status": self.data_status or metadata.get("data_status") or "",
            "scoring_eligible": bool(self.scoring_eligible if self.data_status or self.data_mode or self.scoring_block_reason else metadata.get("scoring_eligible", False)),
            "scoring_block_reason": self.scoring_block_reason or metadata.get("scoring_block_reason") or "",
            "missing_fields": list(self.missing_fields or metadata.get("missing_fields") or []),
            "candidate_fallback": bool(self.candidate_fallback or metadata.get("candidate_fallback", False)),
            "fallback_sources": list(self.fallback_sources or metadata.get("fallback_sources") or []),
            "mock_used": bool(self.mock_used or metadata.get("mock_used", False)),
            "mock_sources": list(self.mock_sources or metadata.get("mock_sources") or []),
            "degraded": bool(self.degraded or metadata.get("degraded", False)),
            "degradation_reasons": list(self.degradation_reasons or metadata.get("degradation_reasons") or []),
            "ai_reason": self.ai_reason,
            "asset_type": self.asset_type,
            "benchmarks": "|".join(self.benchmarks),
            "strategy_family": self.strategy_family,
            "risk_profile": self.risk_profile,
            "timeframe": self.timeframe,
            "validation_status": self.validation_status,
            "current_validation_status": current_validation_status,
            "trade_admission_status": _trade_admission_status(current_validation_status),
            "evidence_status": self.evidence_status,
            "profitability_status": self.profitability_status,
            "deployment_status": self.deployment_status,
            "rejection_reason": self.rejection_reason,
            "selected_at": self.selected_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "selection_stage": metadata.get("selection_stage") or metadata.get("market_selection_stage") or "",
            "last_completed_session": metadata.get("last_completed_session") or "",
            "daily_data_as_of": metadata.get("daily_data_as_of") or "",
            "premarket_snapshot_at": metadata.get("premarket_snapshot_at") or "",
            "freshness_status": metadata.get("freshness_status") or "",
            "stale_reason": metadata.get("stale_reason") or "",
            "trading_eligible": metadata.get("trading_eligible"),
            "current_session": metadata.get("current_session") or "",
            "previous_completed_session": metadata.get("previous_completed_session") or "",
            "next_session": metadata.get("next_session") or "",
            "is_market_holiday": metadata.get("is_market_holiday"),
            "is_premarket": metadata.get("is_premarket"),
            "is_regular_session": metadata.get("is_regular_session"),
            "is_after_hours": metadata.get("is_after_hours"),
            "quote_age_seconds": metadata.get("quote_age_seconds"),
            "benchmark_data_as_of": metadata.get("benchmark_data_as_of") or {},
            "premarket_change_pct": metadata.get("premarket_change_pct"),
            "gap_pct": metadata.get("gap_pct"),
            "premarket_volume": metadata.get("premarket_volume"),
            "spread_pct": metadata.get("spread_pct"),
            "daily_data_status": metadata.get("daily_data_status") or "",
            "data_mode": metadata.get("data_mode") or "",
            "data_freshness": metadata.get("data_freshness") or "",
            "data_status": metadata.get("data_status") or "",
            "scoring_eligible": metadata.get("scoring_eligible"),
            "scoring_block_reason": metadata.get("scoring_block_reason") or "",
            "missing_fields": metadata.get("missing_fields") or [],
            "candidate_fallback": metadata.get("candidate_fallback", False),
            "fallback_sources": metadata.get("fallback_sources") or [],
            "mock_used": metadata.get("mock_used", False),
            "mock_sources": metadata.get("mock_sources") or [],
            "degraded": metadata.get("degraded", False),
            "degradation_reasons": metadata.get("degradation_reasons") or [],
        }


class CandidateTransitionError(RuntimeError):
    pass


def is_valid_transition(current: str, new: str) -> bool:
    try:
        current_status = ValidationStatus(current)
        new_status = ValidationStatus(new)
    except Exception:
        return False
    return new_status in _ALLOWED_TRANSITIONS.get(current_status, set())


def assert_transition_allowed(current: str, new: str) -> None:
    if not is_valid_transition(current, new):
        raise CandidateTransitionError(f"invalid_validation_transition:{current}->{new}")


def default_candidate_for_symbol(symbol: str, *, selected_at: str, source: str = "ai_selector", ai_score: float | None = None, ai_reason: str = "", timeframe: str = "15m") -> CandidateRecord:
    return CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at=selected_at,
        source=source,
        ai_score=ai_score,
        ai_reason=ai_reason,
        timeframe=timeframe,
    )
