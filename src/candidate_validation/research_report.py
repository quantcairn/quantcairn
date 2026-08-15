from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .performance_tracker import CandidatePerformanceTracker
from .outcome_dataset import CandidateOutcomeDatasetBuilder
from .model_evaluation import load_candidate_model_evaluation_snapshot
from src.openalpha.selection_report import load_latest_ai_selection_state
from .store import CandidateValidationStore
from src.dashboard.snapshots import write_dashboard_snapshot
from src.config.runtime_paths import resolve_artifacts_dir

PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))
RESEARCH_ROOT = resolve_artifacts_dir(PROJECT_DIR) / "research" / "daily"
LOGGER = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
        raise
    return path


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in {float("inf"), float("-inf")}:
        return default
    return number


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        if value is None:
            return "unavailable"
        if isinstance(value, float):
            return f"{value:.2f}"
        if isinstance(value, (list, tuple)):
            return " / ".join(str(item) for item in value)
        return str(value)

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(lines)


def _load_ai_selection_report(project_dir: Path | None = None) -> dict[str, Any]:
    return load_latest_ai_selection_state(Path(project_dir or PROJECT_DIR))


def candidate_research_dashboard_payload(report: dict[str, Any]) -> dict[str, object]:
    """Return the Dashboard-facing candidate research payload.

    This is display metadata only. It intentionally mirrors the shape used by
    the Dashboard fallback path so snapshots do not become a new authority.
    """
    performance = report.get("performance") or {}
    return {
        "available": True,
        "state": "SAFE",
        "status_label": "SAFE",
        "detail": "daily research report ready",
        "title": report.get("title") or "AI Candidate Daily Research Report",
        "display_title": "AI Research Report",
        "generated_at": report.get("generated_at"),
        "candidate_count": report.get("candidate_count", 0),
        "average_score": report.get("average_score"),
        "score_distribution": report.get("score_distribution") or [],
        "top_candidates": report.get("top_candidates") or [],
        "failure_analysis": report.get("failure_analysis") or {"statuses": {}},
        "market_regime": report.get("market_regime") or {},
        "strategy_selection": report.get("strategy_selection") or {},
        "candidate_strategy_matrix": list(report.get("candidate_strategy_matrix") or []),
        "portfolio_composition": dict(report.get("portfolio_composition") or {}),
        "final_selected": list(report.get("final_selected") or []),
        "final_selected_count": int(report.get("final_selected_count") or 0),
        "selection_outcome": report.get("selection_outcome") or "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        "actionable_candidate_status": report.get("actionable_candidate_status") or "NO_ACTIONABLE_RESEARCH_CANDIDATE",
        "selection_execution_status": report.get("selection_execution_status") or "COMPLETED",
        "selection_result_quality": report.get("selection_result_quality") or "COMPLETE",
        "selection_research_admission": report.get("selection_research_admission") or "RESEARCH_READY",
        "selection_stage": report.get("selection_stage") or "FINALIZED",
        "selection_top_n_complete": bool(report.get("selection_top_n_complete", False)),
        "selection_top_n_missing_count": int(report.get("selection_top_n_missing_count") or 0),
        "selection_fallback_used": bool(report.get("selection_fallback_used", False)),
        "selection_provider_audit": report.get("selection_provider_audit") or {},
        "selection_provider_outputs": report.get("selection_provider_outputs") or {},
        "selection_warnings_structured": list(report.get("selection_warnings_structured") or []),
        "selection_warnings": list(report.get("selection_warnings") or []),
        "high_score_success_rate": performance.get("high_score_success_rate"),
        "high_score_threshold": performance.get("high_score_threshold", 80.0),
        "performance": performance,
        "run_id": str(report.get("selection_run_id") or ""),
        "source_run_id": str(report.get("selection_run_id") or ""),
        "source": "candidate_daily_research_report",
        "snapshot_name": "candidate_research_report",
    }


@dataclass(slots=True)
class CandidateDailyResearchReportGenerator:
    root_dir: Path = RESEARCH_ROOT
    candidate_root: Path | None = None

    def __post_init__(self) -> None:
        self.root_dir = Path(self.root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        if self.candidate_root is not None:
            self.candidate_root = Path(self.candidate_root)

    @property
    def json_path(self) -> Path:
        return self.root_dir / "daily_candidate_report.json"

    @property
    def markdown_path(self) -> Path:
        return self.root_dir / "daily_candidate_report.md"

    def _load_candidates(self) -> list[dict[str, Any]]:
        store = CandidateValidationStore(self.candidate_root) if self.candidate_root is not None else CandidateValidationStore()
        return store.load_latest_candidates()

    def _load_performance(self) -> dict[str, Any]:
        root = self.candidate_root if self.candidate_root is not None else None
        tracker = CandidatePerformanceTracker(root) if root is not None else CandidatePerformanceTracker()
        return tracker.analyze(self._load_candidates())

    def _load_outcome_dataset(self):
        return CandidateOutcomeDatasetBuilder(
            candidate_root=self.candidate_root if self.candidate_root is not None else None,
        ).build()

    def _top_candidates(self, candidates: list[Any], limit: int = 10) -> list[dict[str, Any]]:
        rows = sorted(
            [candidate.to_dict() for candidate in candidates],
            key=lambda item: (
                -(_safe_float(item.get("candidate_score"), 0.0) or 0.0),
                str(item.get("updated_at") or ""),
                str(item.get("candidate_id") or ""),
            ),
        )
        result: list[dict[str, Any]] = []
        for item in rows[:limit]:
            result.append(
                {
                    "candidate_id": item.get("candidate_id"),
                    "symbol": item.get("symbol"),
                    "candidate_score": item.get("candidate_score"),
                    "factor_scores": {
                        "candidate_score": item.get("candidate_score"),
                        "liquidity_score": item.get("liquidity_score"),
                        "trend_score": item.get("trend_score"),
                        "volatility_score": item.get("volatility_score"),
                        "risk_score": item.get("risk_score"),
                        "strategy_fit_score": item.get("strategy_fit_score"),
                    },
                    "recommended_strategy": item.get("recommended_strategy"),
                    "score_reason": item.get("score_reason"),
                    "validation_status": item.get("validation_status"),
                    "current_validation_status": item.get("current_validation_status") or item.get("validation_status"),
                    "trade_admission_status": item.get("trade_admission_status"),
                    "backtest_status": item.get("metadata", {}).get("backtest_status") if isinstance(item.get("metadata"), dict) else None,
                    "walk_forward_status": item.get("metadata", {}).get("walk_forward_status") if isinstance(item.get("metadata"), dict) else None,
                    "shadow_status": item.get("metadata", {}).get("shadow_status") if isinstance(item.get("metadata"), dict) else None,
                    "candidate_fallback": bool(item.get("candidate_fallback", False)),
                    "fallback_sources": list(item.get("fallback_sources") or []),
                    "mock_used": bool(item.get("mock_used", False)),
                    "mock_sources": list(item.get("mock_sources") or []),
                    "degraded": bool(item.get("degraded", False)),
                    "degradation_reasons": list(item.get("degradation_reasons") or []),
                    "data_mode": item.get("data_mode"),
                    "data_freshness": item.get("data_freshness"),
                    "data_status": item.get("data_status"),
                    "scoring_eligible": bool(item.get("scoring_eligible", False)),
                    "scoring_block_reason": item.get("scoring_block_reason") or "",
                }
            )
        return result

    def _failure_analysis(self, candidates: list[Any]) -> dict[str, Any]:
        statuses = ["DATA_INVALID", "BACKTEST_FAILED", "WALK_FORWARD_FAILED"]
        counts = {status: 0 for status in statuses}
        for candidate in candidates:
            status = _safe_text(getattr(candidate, "validation_status", "")).upper()
            if status in counts:
                counts[status] += 1
        return {
            "statuses": counts,
            "total": sum(counts.values()),
        }

    def build(self) -> dict[str, Any]:
        candidates = self._load_candidates()
        performance = self._load_performance()
        outcome_dataset = self._load_outcome_dataset()
        candidate_model_evaluation = load_candidate_model_evaluation_snapshot(
            candidate_root=self.candidate_root if self.candidate_root is not None else None,
        )
        ai_report = _load_ai_selection_report()
        top_candidates = self._top_candidates(candidates)
        failure_analysis = self._failure_analysis(candidates)
        score_distribution = performance.get("score_bucket_distribution") or []
        if not candidates:
            score_distribution = []
        score_distribution = [
            {
                "score_bucket": row.get("score_bucket"),
                "candidate_count": row.get("candidate_count"),
                "data_valid_rate": row.get("data_valid_rate"),
                "backtest_complete_rate": row.get("backtest_complete_rate"),
                "walk_forward_complete_rate": row.get("walk_forward_complete_rate"),
            }
            for row in score_distribution
        ]
        candidate_count = len(candidates)
        average_score = performance.get("average_score")
        fallback_used = bool(ai_report.get("fallback_used", False))
        execution_status = str(
            ai_report.get("execution_status") or ("COMPLETED" if fallback_used else "COMPLETED")
        ).strip().upper()
        result_quality = str(
            ai_report.get("result_quality") or ("DEGRADED" if fallback_used else "COMPLETE")
        ).strip().upper()
        research_admission = str(
            ai_report.get("research_admission") or ("RESEARCH_ONLY" if fallback_used else "RESEARCH_READY")
        ).strip().upper()
        report = {
            "title": "AI Candidate Daily Research Report",
            "generated_at": _utc_now_iso(),
            "candidate_count": candidate_count,
            "average_score": average_score,
            "score_distribution": score_distribution,
            "top_candidates": top_candidates,
            "performance": performance,
            "candidate_model_evaluation": candidate_model_evaluation,
            "failure_analysis": failure_analysis,
            "selection_execution_status": execution_status,
            "selection_result_quality": result_quality,
            "selection_research_admission": research_admission,
            "selection_stage": str(ai_report.get("selection_stage") or "").strip().upper(),
            "selection_top_n_complete": bool(ai_report.get("top_n_complete", False)),
            "selection_top_n_missing_count": int(ai_report.get("top_n_missing_count") or 0),
            "selection_fallback_used": bool(ai_report.get("fallback_used", False)),
            "selection_provider_audit": ai_report.get("provider_audit") or {},
            "selection_provider_outputs": ai_report.get("provider_outputs") or {},
            "selection_warnings_structured": list(ai_report.get("warnings_structured") or []),
            "selection_warnings": list(ai_report.get("warnings") or []),
            "selection_run_id": str(ai_report.get("selection_run_id") or ""),
            "selection_date": str(ai_report.get("selection_date") or ai_report.get("date") or ""),
            "source": {
                "candidate_store": str((CandidateValidationStore(self.candidate_root).candidates_path) if self.candidate_root is not None else CandidateValidationStore().candidates_path),
                "performance_store": str((CandidatePerformanceTracker(self.candidate_root).performance_path) if self.candidate_root is not None else CandidatePerformanceTracker().performance_path),
            },
        }
        return report

    def _render_markdown(self, report: dict[str, Any]) -> str:
        top_candidates = report.get("top_candidates") or []
        score_distribution = report.get("score_distribution") or []
        failure_analysis = report.get("failure_analysis") or {}
        failure_statuses = failure_analysis.get("statuses") or {}
        model = report.get("candidate_model_evaluation") or {}
        market_regime = report.get("market_regime") or {}
        strategy_selection = report.get("strategy_selection") or {}
        portfolio_composition = report.get("portfolio_composition") or {}
        strategy_candidates = report.get("strategy_candidates") or []
        baseline_metrics = model.get("baseline_metrics") or {}
        challenger_metrics = model.get("challenger_metrics") or {}
        baseline_weights = model.get("baseline_weights") or {}
        proposed_weights = model.get("proposed_weights") or {}

        lines = [
            "# AI Candidate Daily Research Report",
            "",
            f"- Generated At: {report.get('generated_at') or 'unavailable'}",
            f"- Candidate Count: {report.get('candidate_count') or 0}",
            f"- Average Score: {report.get('average_score') if report.get('average_score') is not None else 'unavailable'}",
            f"- Selection Execution: {report.get('selection_execution_status') or 'COMPLETED'}",
            f"- Result Quality: {report.get('selection_result_quality') or 'COMPLETE'}",
            f"- Research Admission: {report.get('selection_research_admission') or 'RESEARCH_READY'}",
            "",
            "## 今日候选统计",
            "",
            _markdown_table(
                ["metric", "value"],
                [
                    ["candidate_count", report.get("candidate_count")],
                    ["average_score", report.get("average_score")],
                    ["selection_execution_status", report.get("selection_execution_status")],
                    ["selection_result_quality", report.get("selection_result_quality")],
                    ["selection_research_admission", report.get("selection_research_admission")],
                    ["score_distribution", "see below"],
                ],
            ),
            "",
            "### Score Distribution",
            "",
            _markdown_table(
                ["score_bucket", "candidate_count", "data_valid_rate", "backtest_complete_rate", "walk_forward_complete_rate"],
                [
                    [
                        row.get("score_bucket"),
                        row.get("candidate_count"),
                        row.get("data_valid_rate"),
                        row.get("backtest_complete_rate"),
                        row.get("walk_forward_complete_rate"),
                    ]
                    for row in score_distribution
                ],
            ),
            "",
            "## Top Candidates",
            "",
            _markdown_table(
                ["candidate_id", "symbol", "candidate_score", "recommended_strategy", "validation_status", "backtest_status", "walk_forward_status", "shadow_status", "score_reason", "trade_admission_status", "fallback/mock"],
                [
                    [
                        item.get("candidate_id"),
                        item.get("symbol"),
                        item.get("candidate_score"),
                        item.get("recommended_strategy"),
                        item.get("validation_status"),
                        item.get("backtest_status"),
                        item.get("walk_forward_status"),
                        item.get("shadow_status"),
                        item.get("score_reason"),
                        item.get("trade_admission_status"),
                        f"{'fallback' if item.get('candidate_fallback') else 'direct'} / {'mock' if item.get('mock_used') else 'real'}",
                    ]
                    for item in top_candidates
                ],
            ),
            "",
            "## Factor Scores",
        ]

        for item in top_candidates:
            factor_scores = item.get("factor_scores") or {}
            lines.extend(
                [
                    "",
                    f"### {item.get('symbol') or item.get('candidate_id')}",
                    _markdown_table(
                        ["factor", "value"],
                        [[key, factor_scores.get(key)] for key in ("candidate_score", "liquidity_score", "trend_score", "volatility_score", "risk_score", "strategy_fit_score")],
                    ),
                    f"- Recommended Strategy: {item.get('recommended_strategy') or 'unavailable'}",
                    f"- Score Reason: {item.get('score_reason') or 'unavailable'}",
                ]
            )

        lines.extend(
            [
                "",
                "## 历史评分效果",
                "",
                "Score Distribution 已在上方展示。",
                "",
                "## Failure Analysis",
                "",
            _markdown_table(
                ["status", "count"],
                [[status, failure_statuses.get(status, 0)] for status in ("DATA_INVALID", "BACKTEST_FAILED", "WALK_FORWARD_FAILED")],
            ),
            "",
            "## Candidate Model Evaluation",
            "",
            _markdown_table(
                ["metric", "value"],
                [
                    ["active_model_version", model.get("active_model_version") or "baseline_v1"],
                    ["challenger_version", model.get("challenger_version") or "unavailable"],
                    ["training_sample_count", model.get("training_sample_count") or 0],
                    ["training_period", f"{(model.get('training_period') or {}).get('start') or 'unavailable'} -> {(model.get('training_period') or {}).get('end') or 'unavailable'}"],
                    ["approval_status", model.get("approval_status") or "DRAFT"],
                    ["recommended_action", model.get("recommended_action") or "keep_baseline"],
                    ["sample_size_warning", model.get("sample_size_warning")],
                    ["overfitting_warning", model.get("overfitting_warning")],
                ],
            ),
            "",
            "### Baseline Metrics",
            "",
            _markdown_table(
                ["metric", "value"],
                [[key, baseline_metrics.get(key)] for key in ("precision_at_3", "precision_at_5", "backtest_pass_rate", "walk_forward_pass_rate", "average_forward_return", "max_drawdown", "calibration_error", "candidate_turnover", "score_rank_correlation")],
            ),
            "",
            "### Challenger Metrics",
            "",
            _markdown_table(
                ["metric", "value"],
                [[key, challenger_metrics.get(key)] for key in ("precision_at_3", "precision_at_5", "backtest_pass_rate", "walk_forward_pass_rate", "average_forward_return", "max_drawdown", "calibration_error", "candidate_turnover", "score_rank_correlation")],
            ),
            "",
            "### Candidate Weights",
            "",
            _markdown_table(
                ["factor", "baseline", "proposed"],
                [[name, baseline_weights.get(name), proposed_weights.get(name)] for name in ("liquidity_score", "trend_score", "volatility_score", "risk_score", "strategy_fit_score")],
            ),
            "",
            "## Selection Execution",
            "",
            _markdown_table(
                ["metric", "value"],
                    [
                        ["execution_status", report.get("selection_execution_status")],
                        ["result_quality", report.get("selection_result_quality")],
                        ["research_admission", report.get("selection_research_admission")],
                        ["selection_stage", report.get("selection_stage")],
                        ["fallback_used", report.get("selection_fallback_used")],
                        ["top_n_complete", report.get("selection_top_n_complete")],
                        ["top_n_missing_count", report.get("selection_top_n_missing_count")],
                    ],
                ),
            "",
            "## 说明",
            "",
            "- 本报告只读生成，不会触发交易、回测、Shadow 或 Paper/Live。",
            "- Top candidates 仅代表当前候选评分与验证状态，不构成交易建议。",
            "- Candidate Model Evaluation 只表示离线校准和权重建议，不会自动更新正式模型。",
            "- 当 final_selected_count 为 0 时，输出 NO_ACTIONABLE_RESEARCH_CANDIDATE。",
        ]
        )
        return "\n".join(lines).strip() + "\n"

    def write(self) -> dict[str, Any]:
        report = self.build()
        _atomic_write_text(self.json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        _atomic_write_text(self.markdown_path, self._render_markdown(report))
        try:
            write_dashboard_snapshot(
                "candidate_research_report",
                candidate_research_dashboard_payload(report),
                source_run_id=str(report.get("selection_run_id") or ""),
                generated_at=str(report.get("generated_at") or ""),
            )
        except Exception as exc:
            LOGGER.warning("dashboard_candidate_research_snapshot_write_failed: %s", exc)
        return report
