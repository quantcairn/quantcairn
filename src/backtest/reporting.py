from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import BacktestResult, WalkForwardResult


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
    (root / "warnings.json").write_text(json.dumps(_jsonable(result.warnings), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(root / "stitched_oos_equity.csv", result.stitched_oos_equity)
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
