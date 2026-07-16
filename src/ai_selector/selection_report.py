from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai_selector.selection_state import load_selection_state

PROJECT_DIR = Path(os.environ.get("SOXS_PROJECT_DIR", str(Path(__file__).resolve().parents[2])))

_PROVIDER_OUTPUT_SCORE_KEYS = {
    "technical_score",
    "news_score",
    "sentiment_score",
    "fundamental_score",
    "valuation_score",
    "earnings_score",
    "balance_sheet_score",
    "cash_flow_score",
    "growth_score",
    "risk_score",
    "confidence",
    "ai_score",
    "range_score",
    "final_score",
    "score",
    "candidate_score",
}

_META_KEYS = {
    "ticker",
    "symbol",
    "source",
    "reason",
    "error_message",
    "error_code",
    "fallback",
    "mock",
    "mock_used",
    "fallback_used",
    "timed_out",
}

_EXPLANATION_ONLY_FIELDS = {
    "reason",
    "summary",
    "commentary",
    "explanation",
    "narrative",
    "analysis",
    "notes",
}

_NON_CRITICAL_FACTOR_FIELDS = {
    "score",
    "ai_score",
    "range_score",
    "final_score",
    "candidate_score",
    "confidence",
    "technical_score",
    "sentiment_score",
    "fundamental_score",
    "valuation_score",
    "earnings_score",
    "growth_score",
    "risk_score",
}

_CRITICAL_MARKET_DATA_FIELDS = {
    "current_price",
    "open",
    "high",
    "low",
    "close",
    "ohlcv",
    "quote",
    "bid",
    "ask",
    "spread_pct",
    "current_session",
    "previous_completed_session",
    "daily_data_as_of",
    "benchmark_data_as_of",
    "benchmark_alignment_status",
    "market_cap",
    "average_dollar_volume_20d",
    "avg_dollar_volume_20d",
    "avg_10d_volume",
    "volume",
    "atr_20_percentage",
    "atr_pct",
    "gap_pct",
}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _flatten_provider_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    nested_rows = [dict(item) for item in payload.values() if isinstance(item, dict)]
    if nested_rows:
        return nested_rows
    return [dict(payload)]


def _row_text(row: dict[str, Any]) -> str:
    parts = [row.get(key) for key in ("reason", "source", "error_message", "error_code")]
    return " ".join(str(part or "") for part in parts).strip().lower()


def _row_is_timeout(row: dict[str, Any]) -> bool:
    text = _row_text(row)
    error_code = str(row.get("error_code") or "").strip().lower()
    return "timeout" in text or "timeout" in error_code or bool(row.get("timed_out"))


def _row_is_mock(row: dict[str, Any]) -> bool:
    text = _row_text(row)
    source = str(row.get("source") or "").strip().lower()
    return bool(row.get("mock")) or bool(row.get("mock_used")) or "mock" in text or source.endswith("_mock")


def _row_is_fallback(row: dict[str, Any]) -> bool:
    return bool(row.get("fallback")) or bool(row.get("fallback_used")) or _row_is_mock(row)


def _row_has_real_success(row: dict[str, Any]) -> bool:
    if _row_is_timeout(row) or _row_is_mock(row) or _row_is_fallback(row):
        return False
    if bool(row.get("success")):
        return True
    return any(row.get(key) is not None for key in _PROVIDER_OUTPUT_SCORE_KEYS)


def _row_contributed_fields(row: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for key, value in row.items():
        if key in _META_KEYS or value is None:
            continue
        fields.append(str(key))
    return sorted(dict.fromkeys(fields))


def _classify_fallback_scope(row: dict[str, Any]) -> tuple[str, str, list[str]]:
    contributed_fields = _row_contributed_fields(row)
    field_set = set(contributed_fields)
    if field_set & _CRITICAL_MARKET_DATA_FIELDS:
        return "CRITICAL_MARKET_DATA", "CRITICAL", contributed_fields
    if field_set & _NON_CRITICAL_FACTOR_FIELDS:
        return "NON_CRITICAL_FACTOR", "DEGRADED", contributed_fields
    if field_set & _EXPLANATION_ONLY_FIELDS:
        return "EXPLANATION_ONLY", "INFO", contributed_fields
    if _row_is_timeout(row):
        return "RUN_LEVEL", "DEGRADED", contributed_fields
    return "EXPLANATION_ONLY", "INFO", contributed_fields


def _provider_records_from_outputs(provider_outputs: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {}
    for provider_name, payload in sorted((provider_outputs or {}).items()):
        provider_key = str(provider_name or "").strip()
        if not provider_key:
            continue
        rows = _flatten_provider_rows(payload)
        if rows:
            records[provider_key] = rows
    return records


def _provider_records_from_audit(provider_audit: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for provider_name, payload in sorted((provider_audit or {}).items()):
        if not _is_mapping(payload):
            continue
        provider_key = str(provider_name or "").strip()
        if not provider_key:
            continue
        records[provider_key] = dict(payload)
    return records


def normalize_provider_audit(
    provider_audit: dict[str, Any] | None,
    provider_outputs: dict[str, Any] | None,
) -> dict[str, Any]:
    provider_audit = provider_audit if _is_mapping(provider_audit) else {}
    provider_outputs = provider_outputs if _is_mapping(provider_outputs) else {}

    output_records = _provider_records_from_outputs(provider_outputs)
    audit_records = _provider_records_from_audit(provider_audit)
    provider_names = sorted(set(output_records) | set(audit_records))

    attempted: list[str] = []
    successful: list[str] = []
    failed: list[str] = []
    timed_out: list[str] = []
    fallback: list[str] = []
    mock: list[str] = []
    contributors: list[str] = []
    details: list[dict[str, Any]] = []

    for provider_name in provider_names:
        audit_record = dict(audit_records.get(provider_name) or {})
        rows = list(output_records.get(provider_name) or [])

        attempted_count = _safe_int(audit_record.get("attempted"))
        successful_count = _safe_int(audit_record.get("success"))
        failed_count = _safe_int(audit_record.get("failure"))
        timed_out_count = _safe_int(audit_record.get("timed_out"))
        fallback_count = _safe_int(audit_record.get("fallback_used"))
        mock_count = _safe_int(audit_record.get("mock_used"))
        contributor_fields = list(audit_record.get("contributed_fields") or [])

        row_timeout = any(_row_is_timeout(row) for row in rows)
        row_mock = any(_row_is_mock(row) for row in rows)
        row_fallback = any(_row_is_fallback(row) for row in rows)
        row_success = any(_row_has_real_success(row) for row in rows)
        row_contributors = sorted({field for row in rows for field in _row_contributed_fields(row)})
        row_scopes = sorted({scope for row in rows for scope, _, _ in [_classify_fallback_scope(row)]})
        row_severities = sorted({severity for row in rows for _, severity, _ in [_classify_fallback_scope(row)]})
        affected_candidates = sorted({
            str(row.get("ticker") or row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("ticker") or row.get("symbol") or "").strip()
        })

        if not attempted_count:
            attempted_count = len(rows) if rows else (1 if provider_name in provider_outputs else 0)
        if not successful_count and row_success:
            successful_count = 1
        if not timed_out_count and row_timeout:
            timed_out_count = 1
        if not fallback_count and row_fallback:
            fallback_count = 1
        if not mock_count and row_mock:
            mock_count = 1
        if not contributor_fields:
            contributor_fields = row_contributors

        if attempted_count > 0:
            attempted.append(provider_name)
        if successful_count > 0:
            successful.append(provider_name)
        elif attempted_count > 0 and timed_out_count <= 0 and fallback_count <= 0 and mock_count <= 0:
            failed.append(provider_name)
        if timed_out_count > 0:
            timed_out.append(provider_name)
        if fallback_count > 0:
            fallback.append(provider_name)
        if mock_count > 0:
            mock.append(provider_name)
        if contributor_fields:
            suffix = " (mock)" if mock_count > 0 else " (fallback)" if fallback_count > 0 else ""
            contributors.append(f"{provider_name}{suffix}")

        details.append(
            {
                "provider_name": provider_name,
                "attempted": attempted_count,
                "successful": successful_count,
                "failed": failed_count if failed_count else (1 if provider_name in failed else 0),
                "timed_out": timed_out_count,
                "fallback": fallback_count,
                "mock": mock_count,
                "contributors": list(dict.fromkeys(contributor_fields)),
                "fallback_scope": row_scopes[0] if row_scopes else "EXPLANATION_ONLY",
                "fallback_severity": row_severities[0] if row_severities else "INFO",
                "affected_candidates": affected_candidates,
                "affected_fields": row_contributors,
                "has_real_success": row_success or bool(successful_count),
                "has_timeout": row_timeout or bool(timed_out_count),
                "has_fallback": row_fallback or bool(fallback_count),
                "has_mock": row_mock or bool(mock_count),
            }
        )

    def _unique_join(values: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in values if item))

    normalized = {
        "attempted": _unique_join(attempted),
        "successful": _unique_join(successful),
        "failed": _unique_join(failed),
        "timed_out": _unique_join(timed_out),
        "fallback": _unique_join(fallback),
        "mock": _unique_join(mock),
        "contributors": _unique_join(contributors),
        "counts": {
            "attempted": len(_unique_join(attempted)),
            "successful": len(_unique_join(successful)),
            "failed": len(_unique_join(failed)),
            "timed_out": len(_unique_join(timed_out)),
            "fallback": len(_unique_join(fallback)),
            "mock": len(_unique_join(mock)),
            "contributors": len(_unique_join(contributors)),
        },
        "details": details,
    }
    return normalized


def provider_audit_sections(
    provider_audit: dict[str, Any] | None,
    provider_outputs: dict[str, Any] | None,
) -> dict[str, str]:
    normalized = normalize_provider_audit(provider_audit, provider_outputs)

    def _join(values: list[str]) -> str:
        return " / ".join(values) if values else "无"

    return {
        "attempted": _join(list(normalized.get("attempted") or [])),
        "success": _join(list(normalized.get("successful") or [])),
        "failure": _join(list(normalized.get("failed") or [])),
        "timeout": _join(list(normalized.get("timed_out") or [])),
        "fallback": _join(list(normalized.get("fallback") or [])),
        "mock": _join(list(normalized.get("mock") or [])),
        "contributor": _join(list(normalized.get("contributors") or [])),
    }


def _report_from_data(data: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    report = dict(data or {})
    if "execution_status" not in report and report.get("selection_execution_status") is not None:
        report["execution_status"] = report.get("selection_execution_status")
    if "result_quality" not in report and report.get("selection_result_quality") is not None:
        report["result_quality"] = report.get("selection_result_quality")
    if "research_admission" not in report and report.get("selection_research_admission") is not None:
        report["research_admission"] = report.get("selection_research_admission")
    if "selection_stage" not in report and report.get("refinement_selection_stage") is not None:
        report["selection_stage"] = report.get("refinement_selection_stage")

    timestamp = report.get("generated_at") or report.get("timestamp") or report.get("selection_date")
    report["generated_at"] = str(timestamp or "").strip() or None
    if not report["generated_at"]:
        try:
            report["generated_at"] = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).isoformat()
        except Exception:
            report["generated_at"] = None
    report["source_path"] = str(source_path)
    try:
        report["source_mtime"] = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        report["source_mtime"] = None
    provider_audit = dict(report.get("provider_audit") or {})
    provider_outputs = dict(report.get("provider_outputs") or {})
    report["provider_audit_sections"] = provider_audit_sections(provider_audit, provider_outputs)
    report["provider_audit_normalized"] = normalize_provider_audit(provider_audit, provider_outputs)
    report.setdefault("top3", list(report.get("top3") or []))
    report.setdefault("top10", list(report.get("top10") or []))
    report.setdefault("settings", dict(report.get("settings") or {}))
    report.setdefault("warnings_structured", list(report.get("warnings_structured") or []))
    report.setdefault("warnings", list(report.get("warnings") or []))
    return report


def load_latest_ai_selection_state(project_dir: Path | None = None) -> dict[str, Any]:
    root = Path(project_dir) if project_dir is not None else PROJECT_DIR
    candidates: list[Path] = []
    latest_path = root / "reports" / "ai_selection_latest.json"
    if latest_path.exists():
        candidates.append(latest_path)

    try:
        state = load_selection_state()
    except Exception:
        state = None
    if isinstance(state, dict):
        report_path = str(state.get("report_path") or "").strip()
        if report_path:
            candidate = Path(report_path)
            if not candidate.is_absolute():
                candidate = root / report_path
            if candidate.exists():
                candidates.append(candidate)

    reports_dir = root / "reports"
    if reports_dir.exists():
        for path in reports_dir.glob("ai_selection_*.json"):
            if path.name != "ai_selection_latest.json":
                candidates.append(path)

    best_report: dict[str, Any] | None = None
    best_score: tuple[datetime | None, float] = (None, -1.0)
    for path in candidates:
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        report = _report_from_data(data, source_path=path)
        generated_at = _parse_timestamp(report.get("generated_at"))
        score = (generated_at, float(path.stat().st_mtime if path.exists() else 0.0))
        if best_report is None or (
            (score[0] is not None and best_score[0] is None)
            or (score[0] is not None and best_score[0] is not None and score[0] > best_score[0])
            or (score[0] == best_score[0] and score[1] > best_score[1])
        ):
            best_report = report
            best_score = score

    if best_report is not None:
        return best_report

    if isinstance(state, dict):
        report_path = str(state.get("report_path") or "").strip()
        if report_path:
            candidate = Path(report_path)
            if not candidate.is_absolute():
                candidate = root / report_path
            if candidate.exists():
                data = _load_json(candidate)
                if isinstance(data, dict):
                    return _report_from_data(data, source_path=candidate)

    return {
        "generated_at": None,
        "source_path": None,
        "source_mtime": None,
        "execution_status": "COMPLETED",
        "result_quality": "COMPLETE",
        "research_admission": "RESEARCH_READY",
        "selection_stage": "UNKNOWN",
        "fallback_used": False,
        "provider_fallback_used": False,
        "top_n_complete": False,
        "top_n_missing_count": 0,
        "warnings_structured": [],
        "warnings": [],
        "provider_audit": {},
        "provider_outputs": {},
        "provider_audit_sections": {
            "attempted": "无",
            "success": "无",
            "failure": "无",
            "timeout": "无",
            "fallback": "无",
            "mock": "无",
            "contributor": "无",
        },
        "provider_audit_normalized": {
            "attempted": [],
            "successful": [],
            "failed": [],
            "timed_out": [],
            "fallback": [],
            "mock": [],
            "contributors": [],
            "counts": {
                "attempted": 0,
                "successful": 0,
                "failed": 0,
                "timed_out": 0,
                "fallback": 0,
                "mock": 0,
                "contributors": 0,
            },
            "details": [],
        },
    }
