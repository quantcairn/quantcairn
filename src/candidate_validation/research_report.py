from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .performance_tracker import CandidatePerformanceTracker
from .store import CandidateValidationStore

PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))
RESEARCH_ROOT = PROJECT_DIR / "artifacts" / "research" / "daily"


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
                    "backtest_status": item.get("metadata", {}).get("backtest_status") if isinstance(item.get("metadata"), dict) else None,
                    "walk_forward_status": item.get("metadata", {}).get("walk_forward_status") if isinstance(item.get("metadata"), dict) else None,
                    "shadow_status": item.get("metadata", {}).get("shadow_status") if isinstance(item.get("metadata"), dict) else None,
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
        report = {
            "title": "AI Candidate Daily Research Report",
            "generated_at": _utc_now_iso(),
            "candidate_count": candidate_count,
            "average_score": average_score,
            "score_distribution": score_distribution,
            "top_candidates": top_candidates,
            "performance": performance,
            "failure_analysis": failure_analysis,
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

        lines = [
            "# AI Candidate Daily Research Report",
            "",
            f"- Generated At: {report.get('generated_at') or 'unavailable'}",
            f"- Candidate Count: {report.get('candidate_count') or 0}",
            f"- Average Score: {report.get('average_score') if report.get('average_score') is not None else 'unavailable'}",
            "",
            "## 今日候选统计",
            "",
            _markdown_table(
                ["metric", "value"],
                [
                    ["candidate_count", report.get("candidate_count")],
                    ["average_score", report.get("average_score")],
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
                ["candidate_id", "symbol", "candidate_score", "recommended_strategy", "validation_status", "backtest_status", "walk_forward_status", "shadow_status", "score_reason"],
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
                "## 说明",
                "",
                "- 本报告只读生成，不会触发交易、回测、Shadow 或 Paper/Live。",
                "- Top candidates 仅代表当前候选评分与验证状态，不构成交易建议。",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def write(self) -> dict[str, Any]:
        report = self.build()
        _atomic_write_text(self.json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        _atomic_write_text(self.markdown_path, self._render_markdown(report))
        return report
