from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import BacktestResult, StrategyComparisonResult, WalkForwardResult


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _git_commit_hash() -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        return None
    return None


def make_run_id(strategy: str, symbol: str, data_start: str | None, data_end: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    symbol_part = symbol.replace(".", "_") if symbol else "UNKNOWN"
    span = f"{(data_start or 'start').replace(':', '')}_{(data_end or 'end').replace(':', '')}"
    return f"{strategy}_{symbol_part}_{span}_{stamp}"


def write_backtest_artifacts(result: BacktestResult, output_dir: str | Path) -> Path:
    root = Path(output_dir) / result.run_id
    root.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["git_commit"] = _git_commit_hash()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload = _jsonable(payload)

    (root / "summary.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "metrics.json").write_text(
        json.dumps(_jsonable(result.metrics), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "configuration.json").write_text(
        json.dumps(_jsonable(result.configuration), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "warnings.json").write_text(
        json.dumps(_jsonable(result.warnings), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_csv(root / "trades.csv", result.trades)
    _write_csv(root / "orders.csv", result.orders)
    _write_csv(root / "equity_curve.csv", result.equity_curve)
    _write_csv(root / "drawdown_curve.csv", result.drawdown_curve)
    _write_csv(root / "rejected_signals.csv", result.rejected_signals)
    return root


def write_walk_forward_artifacts(result: WalkForwardResult, output_dir: str | Path) -> Path:
    root = Path(output_dir) / f"walk_forward_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    root.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["git_commit"] = _git_commit_hash()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    (root / "summary.json").write_text(json.dumps(_jsonable(payload), indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "metrics.json").write_text(
        json.dumps(_jsonable(result.aggregate_oos_metrics), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "configuration.json").write_text(
        json.dumps(_jsonable({"strategy": result.strategy, "symbol": result.symbol}), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "warnings.json").write_text(json.dumps(_jsonable(result.warnings), indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "parameter_stability.json").write_text(
        json.dumps(_jsonable(result.parameter_stability), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_csv(root / "parameter_candidates.csv", result.parameter_candidates or [])
    _write_csv(root / "top_candidates.csv", _top_candidates_rows(result.parameter_candidates or []))
    _write_csv(root / "parameter_sensitivity.csv", _parameter_sensitivity_rows(result.parameter_candidates or []))
    _write_csv(root / "stitched_oos_equity.csv", result.stitched_oos_equity)
    _write_markdown_report(root / "report.md", _walk_forward_markdown(result))
    return root


def write_strategy_comparison_artifacts(result: StrategyComparisonResult, output_dir: str | Path) -> Path:
    root = Path(output_dir) / result.run_id
    root.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["git_commit"] = _git_commit_hash()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload = _jsonable(payload)
    (root / "comparison_summary.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(root / "strategy_metrics.csv", result.metrics)
    _write_csv(root / "strategy_ranking.csv", result.ranking)
    _write_csv(root / "parameter_candidates.csv", result.parameter_candidates or [])
    (root / "parameter_stability.json").write_text(
        json.dumps(_jsonable(result.parameter_stability), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "warnings.json").write_text(json.dumps(_jsonable(result.warnings), indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "configuration.json").write_text(
        json.dumps(_jsonable(result.configuration), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    for row in result.comparison:
        version = str(row.get("version") or "unknown")
        _write_csv(root / f"trades_{version}.csv", row.get("trades", []))
        _write_csv(root / f"orders_{version}.csv", row.get("orders", []))
        _write_csv(root / f"equity_{version}.csv", row.get("equity_curve", []))
        _write_csv(root / f"drawdown_{version}.csv", row.get("drawdown_curve", []))
        _write_csv(root / f"rejected_{version}.csv", row.get("rejected_signals", []))
    _write_markdown_report(root / "report.md", _comparison_markdown(result))
    return root


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    rows = rows or []
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    columns = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _jsonable(row.get(key)) for key in columns})


def _top_candidates_rows(parameter_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not parameter_candidates:
        return []
    rows = sorted(
        parameter_candidates,
        key=lambda row: float(row.get("risk_adjusted_score") or 0.0),
        reverse=True,
    )
    top_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(rows[:10], start=1):
        top_rows.append({"rank": rank, **row})
    return top_rows


def _parameter_sensitivity_rows(parameter_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(parameter_candidates, start=1):
        params = candidate.get("parameter_set") or {}
        rows.append(
            {
                "candidate_index": index,
                "parameter_key": "|".join(f"{key}={params[key]}" for key in sorted(params)),
                "risk_adjusted_score": candidate.get("risk_adjusted_score"),
                "trade_count": candidate.get("trade_count"),
                "total_return": candidate.get("total_return"),
                "max_drawdown": candidate.get("max_drawdown"),
                "sharpe": candidate.get("sharpe"),
                "calmar": candidate.get("calmar"),
            }
        )
    return rows


def _write_markdown_report(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _comparison_markdown(result: StrategyComparisonResult) -> str:
    summary = result.summary or {}
    lines = [
        f"# Strategy Comparison Report",
        "",
        f"- Symbol: {result.symbol}",
        f"- Data start: {result.data_start}",
        f"- Data end: {result.data_end}",
        f"- Data frequency: {summary.get('data_frequency')}",
        f"- Benchmark frequency: {summary.get('benchmark_frequency')}",
        f"- Baseline: baseline",
        f"- Benchmark status: {summary.get('benchmark_status')}",
        f"- Ranking status: {summary.get('ranking_status')}",
        "",
        "## Risk-adjusted ranking",
    ]
    for row in result.ranking:
        lines.append(
            f"- {row.get('version')}: score={row.get('risk_adjusted_score')} trade_count={row.get('trade_count')} total_return={row.get('total_return')} max_drawdown={row.get('max_drawdown')}"
        )
    eligible = summary.get("eligible_ranking") or []
    insufficient = summary.get("insufficient_evidence_versions") or []
    lines.extend(
        [
            "",
            "## Eligible ranking",
        ]
    )
    if eligible:
        lines.extend(f"- {version}" for version in eligible)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Insufficient evidence",
        ]
    )
    if insufficient:
        lines.extend(f"- {version}" for version in insufficient)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Warnings",
        ]
    )
    if result.warnings:
        lines.extend(f"- {warning}" for warning in result.warnings)
    else:
        lines.append("- none")
    return "\n".join(lines)


def _walk_forward_markdown(result: WalkForwardResult) -> str:
    stability = result.parameter_stability or {}
    lines = [
        "# Walk-forward Report",
        "",
        f"- Symbol: {result.symbol}",
        f"- Strategy: {result.strategy}",
        f"- Windows: {len(result.windows)}",
        f"- Window failures: {result.window_failure_count}",
        f"- No-trade windows: {result.no_trade_window_count}",
        f"- Active window ratio: {stability.get('active_window_ratio')}",
        f"- No-trade window ratio: {stability.get('no_trade_window_ratio')}",
        f"- Ranking status: {stability.get('ranking_status')}",
        "",
        "## Parameter stability",
    ]
    for key, value in stability.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Warnings"])
    if result.warnings:
        lines.extend(f"- {warning}" for warning in result.warnings)
    else:
        lines.append("- none")
    return "\n".join(lines)
