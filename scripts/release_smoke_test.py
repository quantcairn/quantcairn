#!/usr/bin/env python3
"""Unified release smoke test for QuantCairn.

This script exercises the minimum release gate path in an isolated workspace:

- AI Selector
- Selection Bundle + TOP Configs
- Candidate validation / research / model-evaluation / shadow snapshots
- Dashboard startup + GET /api/status
- Telegram delivery mock
- PaperBroker initialization
- Scheduler / system health
- Log traceback scan

The script never mutates the caller's working tree. It copies the repository to
an isolated temporary workspace, runs all checks there, and exits non-zero on
the first failure.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SELECTOR_MODE = "fast_preliminary"
DEFAULT_UNIVERSE_SOURCE = "sample"


class ReleaseSmokeError(RuntimeError):
    pass


@dataclass(slots=True)
class SmokeStepResult:
    name: str
    ok: bool
    detail: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return dict(data) if isinstance(data, dict) else None


def _read_dashboard_snapshot(workspace_root: Path, name: str) -> dict[str, Any]:
    path = workspace_root / "state" / "dashboard_snapshots" / f"{name}.json"
    payload = _read_json(path)
    if not payload or not isinstance(payload.get("data"), dict):
        raise ReleaseSmokeError(f"dashboard snapshot invalid or missing: {name}")
    return payload


def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    patterns = (
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "build",
        "dist",
        "state",
        "logs",
        "reports",
        "site",
        ".coverage",
        "config.local.yaml",
        "config.local.json",
        ".env",
        ".env.local",
        ".env.ai_selector.local",
    )
    for name in names:
        if name in patterns:
            ignored.add(name)
            continue
        if fnmatch.fnmatch(name, "*.local.yaml") or fnmatch.fnmatch(name, "*.local.json"):
            ignored.add(name)
            continue
        if fnmatch.fnmatch(name, "*.pyc") or name == ".DS_Store":
            ignored.add(name)
            continue
        if name.startswith(".env"):
            ignored.add(name)
            continue
    return ignored


@contextmanager
def _smoke_workspace(repo_root: Path) -> Iterator[Path]:
    temp_root = Path(tempfile.mkdtemp(prefix="quantcairn-release-smoke-"))
    workspace_root = temp_root / "workspace"
    try:
        shutil.copytree(repo_root, workspace_root, ignore=_copy_ignore)
        yield workspace_root
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _base_env(workspace_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace_root)
    env["SOXS_STATE_DIR"] = str(workspace_root / "state")
    env["YF_DISABLE_CURL_CFFI"] = "1"
    env["OPENALPHA_SKIP_YFINANCE_HISTORY"] = "1"
    env["OPENALPHA_RESTART_TOP"] = "0"
    env["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
    env["OPENALPHA_HTTP_TIMEOUT_SECONDS"] = "1"
    # Prevent accidental use of developer machine secrets in the smoke path.
    for name in (
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
        "FMP_API_KEY",
        "SOXS_FMP_ENABLED",
        "SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN",
        "SOXS_OPENALPHA_TELEGRAM_CHAT_ID",
        "SOXS_TELEGRAM_BOT_TOKEN",
        "SOXS_TELEGRAM_CHAT_ID",
        "QUANTCAIRN_ADMIN_CHAT_ID",
        "SOXS_OPENALPHA_ADMIN_CHAT_ID",
    ):
        env.pop(name, None)
    return env


def _run_command(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 240,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    combined_output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise ReleaseSmokeError(
            "command failed: "
            f"{' '.join(cmd)}\n"
            f"exit={proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    if "Traceback (most recent call last)" in combined_output or "Traceback:" in combined_output:
        raise ReleaseSmokeError(
            "traceback detected in command output: "
            f"{' '.join(cmd)}\n"
            f"{combined_output}"
        )
    return proc


def _run_python_json(workspace_root: Path, code: str, *, timeout_seconds: int = 240) -> dict[str, Any]:
    proc = _run_command(
        [sys.executable, "-c", textwrap.dedent(code).strip()],
        cwd=workspace_root,
        env=_base_env(workspace_root),
        timeout_seconds=timeout_seconds,
    )
    stdout = proc.stdout.strip()
    candidate_payloads: list[str] = []
    if stdout:
        candidate_payloads.extend(line.strip() for line in stdout.splitlines()[::-1] if line.strip())
        candidate_payloads.append(stdout)
    try:
        payload: dict[str, Any] | None = None
        for candidate in candidate_payloads:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
        if payload is None:
            raise ValueError("no JSON object found in subprocess stdout")
    except Exception as exc:  # pragma: no cover - defensive
        raise ReleaseSmokeError(f"failed to parse JSON from subprocess: {proc.stdout}") from exc
    return payload


def _run_selector_smoke(workspace_root: Path, *, universe_source: str, mode: str) -> dict[str, Any]:
    started = time.perf_counter()
    summary = _run_python_json(
        workspace_root,
        f"""
        import json
        import os
        from pathlib import Path

        import pandas as pd

        from src.openalpha.demo_data import get_demo_provider
        from src.openalpha.preflight import PreflightReport
        import src.openalpha.preflight as preflight_module
        import src.openalpha.selector as selector_module
        import scripts.run_ai_selector as runner

        os.environ["OPENALPHA_UNIVERSE"] = {universe_source!r}
        os.environ["OPENALPHA_FAST_START_ONLY"] = "1"
        os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
        os.environ["OPENALPHA_RESTART_TOP"] = "0"
        os.environ["OPENALPHA_SKIP_YFINANCE_HISTORY"] = "1"
        os.environ["OPENALPHA_DIRECT_HISTORY"] = "1"
        os.environ["OPENALPHA_HTTP_TIMEOUT_SECONDS"] = "1"
        os.environ["OPENALPHA_FETCH_NEWS"] = "0"

        provider = get_demo_provider()
        demo_symbols = provider.symbols

        original_preflight = preflight_module.run_preflight
        def _demo_preflight(*args, **kwargs):
            return PreflightReport(
                selection_run_id="release-smoke",
                diagnostic_preflight=False,
                market_state="DEMO",
                is_trading_day=True,
                session_label="DEMO",
                session_reason="release_smoke",
                current_session_date="2026-08-02",
                previous_session_date="2026-08-01",
                run_mode={mode!r},
                data_mode="DEMO",
                symbols_checked=len(demo_symbols),
                quotes_available=len(demo_symbols),
                ohlcv_available=len(demo_symbols),
                quote_coverage_pct=100.0,
                ohlcv_coverage_pct=100.0,
            )
        preflight_module.run_preflight = _demo_preflight

        original_qfc = selector_module._QualityFilterContext
        class DemoQualityContext:
            def __init__(self):
                pass
            def history_metrics(self, symbol: str):
                upper = str(symbol).strip().upper()
                df = provider.get_ohlcv(upper) if upper in provider.symbols else None
                if df is not None and len(df) >= 10:
                    avg_vol = float(df["Volume"].tail(10).mean())
                    closes = df["Close"].values
                    chg = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0 if len(closes) >= 4 and closes[-4] > 0 else 0.0
                    return (avg_vol, chg, float(closes[-1]))
                return (2_000_000.0, 0.0, 50.0)
            def quote_metrics(self, symbol: str):
                upper = str(symbol).strip().upper()
                price = provider.price_at(upper) if upper in provider.symbols else None
                price = price or 50.0
                spread = price * 0.0002
                return (price, price - spread, price + spread, True)
            def close(self):
                pass
        selector_module._QualityFilterContext = DemoQualityContext

        original_run_selection = selector_module.AIStrategySelector.run_selection
        def _demo_run_selection(self, *args, **kwargs):
            self.selection_size = 5
            self.universe._load_local_snapshot = lambda: list(demo_symbols)
            self.news.collect_for_symbols = lambda symbols: {{s: [] for s in symbols}}
            def _demo_load_history(symbol: str) -> pd.DataFrame:
                try:
                    return provider.get_ohlcv(str(symbol).strip().upper())
                except KeyError:
                    return pd.DataFrame()
            self.scorer._load_history = _demo_load_history
            return original_run_selection(self, *args, **kwargs)
        selector_module.AIStrategySelector.run_selection = _demo_run_selection

        try:
            runner.main({mode!r})
        finally:
            preflight_module.run_preflight = original_preflight
            selector_module._QualityFilterContext = original_qfc
            selector_module.AIStrategySelector.run_selection = original_run_selection

        print(json.dumps({{"status": "completed"}}, ensure_ascii=False))
        """
    )
    elapsed = time.perf_counter() - started

    manifest_path = workspace_root / "state" / "selection_bundle_manifest.json"
    manifest = _read_json(manifest_path)
    if not manifest:
        raise ReleaseSmokeError("selection bundle manifest missing after selector run")

    bundle_root_raw = str(
        manifest.get("bundle_root")
        or manifest.get("selection_bundle_root_path")
        or manifest.get("bundle_root_path")
        or ""
    ).strip()
    if not bundle_root_raw:
        raise ReleaseSmokeError("selection bundle manifest missing bundle_root path")
    bundle_root = Path(bundle_root_raw)
    if not bundle_root.is_absolute():
        bundle_root = workspace_root / bundle_root_raw
    if not bundle_root.exists():
        raise ReleaseSmokeError(f"selection bundle root missing: {bundle_root}")

    report_path = bundle_root / "ai_selection_report.json"
    report = _read_json(report_path)
    if not report:
        raise ReleaseSmokeError("selection report missing after selector run")
    selection_run_id = str(
        report.get("selection_run_id")
        or manifest.get("selection_run_id")
        or ""
    ).strip()

    top_paths = [workspace_root / "configs" / f"TOP{i}.yaml" for i in range(1, 4)]
    missing_top = [str(path) for path in top_paths if not path.exists()]
    if missing_top:
        raise ReleaseSmokeError(f"missing TOP config files: {missing_top}")
    top_configs: list[dict[str, Any]] = []
    for path in top_paths:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise ReleaseSmokeError(f"failed to parse TOP config: {path}") from exc
        if not isinstance(loaded, dict):
            raise ReleaseSmokeError(f"TOP config is not a mapping: {path}")
        top_configs.append(dict(loaded))

    candidate_validation_snapshot = _read_dashboard_snapshot(workspace_root, "candidate_validation")
    candidate_validation_data = candidate_validation_snapshot.get("data", {})
    if selection_run_id and str(candidate_validation_snapshot.get("source_run_id") or "").strip() not in {"", selection_run_id}:
        raise ReleaseSmokeError("candidate_validation snapshot source_run_id mismatch")

    return {
        "elapsed_seconds": round(elapsed, 3),
        "selection_run_id": selection_run_id,
        "manifest_path": str(manifest_path.relative_to(workspace_root)),
        "bundle_root": str(bundle_root.relative_to(workspace_root)),
        "report_path": str(report_path.relative_to(workspace_root)),
        "top_paths": [str(path.relative_to(workspace_root)) for path in top_paths],
        "report": report,
        "top_configs": top_configs,
        "candidate_validation_snapshot": {
            "generated_at": candidate_validation_snapshot.get("generated_at"),
            "source_run_id": candidate_validation_snapshot.get("source_run_id"),
            "data_keys": sorted(candidate_validation_data.keys()),
        },
        "selected_symbols": list(report.get("selected_symbols") or []),
        "final_selected_symbols": list(report.get("final_selected_symbols") or []),
        "selected_top_n": int(report.get("selected_top_n") or 0),
    }


def _write_candidate_model_evaluation_snapshot(workspace_root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    summary = _run_python_json(
        workspace_root,
        """
        import json
        from pathlib import Path
        from src.candidate_validation.model_evaluation import CandidateModelEvaluationService

        workspace_root = Path.cwd()
        service = CandidateModelEvaluationService(
            candidate_root=workspace_root / "artifacts" / "candidates",
            backtest_root=workspace_root / "artifacts" / "backtests",
            model_root=workspace_root / "config" / "candidate_models",
            evaluation_root=workspace_root / "artifacts" / "candidate_models" / "evaluation",
        )
        report = service.write()
        print(json.dumps({
            "report_keys": sorted(report.keys()),
            "approval_status": report.get("approval_status"),
            "generated_at": report.get("generated_at"),
        }, ensure_ascii=False))
        """,
    )
    elapsed = time.perf_counter() - started
    snapshot = _read_dashboard_snapshot(workspace_root, "candidate_model_evaluation")
    return {
        "elapsed_seconds": round(elapsed, 3),
        "report_keys": list(summary.get("report_keys") or []),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_source_run_id": snapshot.get("source_run_id"),
        "snapshot_data_keys": sorted((snapshot.get("data") or {}).keys()),
        "approval_status": summary.get("approval_status"),
    }


def _write_candidate_research_snapshot(workspace_root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    summary = _run_python_json(
        workspace_root,
        """
        import json
        from pathlib import Path
        from src.candidate_validation.research_report import CandidateDailyResearchReportGenerator

        workspace_root = Path.cwd()
        generator = CandidateDailyResearchReportGenerator(
            root_dir=workspace_root / "artifacts" / "research" / "daily",
            candidate_root=workspace_root / "artifacts" / "candidates",
        )
        report = generator.write()
        print(json.dumps({
            "report_keys": sorted(report.keys()),
            "selection_outcome": report.get("selection_outcome"),
            "generated_at": report.get("generated_at"),
        }, ensure_ascii=False))
        """,
    )
    elapsed = time.perf_counter() - started
    snapshot = _read_dashboard_snapshot(workspace_root, "candidate_research_report")
    return {
        "elapsed_seconds": round(elapsed, 3),
        "report_keys": list(summary.get("report_keys") or []),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_source_run_id": snapshot.get("source_run_id"),
        "snapshot_data_keys": sorted((snapshot.get("data") or {}).keys()),
        "selection_outcome": summary.get("selection_outcome"),
    }


def _fake_shadow_bars(symbol: str, *, base: float, count: int = 12) -> list[dict[str, Any]]:
    start = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        price = base + index * 0.12
        rows.append(
            {
                "symbol": symbol,
                "timestamp": (start + timedelta(minutes=15 * index)).isoformat(),
                "open": round(price, 4),
                "high": round(price + 0.18, 4),
                "low": round(price - 0.18, 4),
                "close": round(price + 0.04, 4),
                "volume": 1000 + index * 10,
                "turnover": 10000 + index * 100,
            }
        )
    return list(reversed(rows))


def _write_shadow_snapshot(workspace_root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    summary = _run_python_json(
        workspace_root,
        """
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backtest.models import Bar
from src.shadow import (
    ShadowMarketBundle,
    ShadowMarketDataSource,
    ShadowObserver,
    ShadowRuntimeConfig,
    ShadowRuntimeStateStore,
    ShadowSafetyConfig,
)

shadow_env = {
    "SHADOW_MODE": "true",
    "TRADING_ENABLED": "false",
    "ORDER_SUBMISSION_ENABLED": "false",
    "BROKER_WRITE_ENABLED": "false",
    "LONGBRIDGE_APP_KEY": "release-smoke-key",
    "LONGBRIDGE_APP_SECRET": "release-smoke-secret",
    "LONGBRIDGE_ACCESS_TOKEN": "release-smoke-token",
}
os.environ.update(shadow_env)


def fake_shadow_bars(symbol: str, *, base: float, count: int = 12) -> list[dict[str, object]]:
    start = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        price = base + index * 0.12
        rows.append(
            {
                "symbol": symbol,
                "timestamp": (start + timedelta(minutes=15 * index)).isoformat(),
                "open": round(price, 4),
                "high": round(price + 0.18, 4),
                "low": round(price - 0.18, 4),
                "close": round(price + 0.04, 4),
                "volume": 1000 + index * 10,
                "turnover": 10000 + index * 100,
            }
        )
    return list(reversed(rows))


class _FakeShadowMarketSource(ShadowMarketDataSource):
    def __init__(self) -> None:
        pass

    def fetch_bundle(
        self,
        *,
        symbol: str,
        benchmark_symbols: tuple[str, ...],
        frequency: str,
        lookback_days: int,
        end_dt=None,
    ) -> ShadowMarketBundle:
        symbol_bars = [
            Bar(
                symbol=symbol,
                timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                source="release_smoke",
            )
            for row in fake_shadow_bars(symbol, base=10.0)
        ]
        benchmark_bars = {}
        benchmark_validation = {}
        for offset, benchmark_symbol in enumerate(benchmark_symbols, start=1):
            bars = [
                Bar(
                    symbol=benchmark_symbol,
                    timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                    open=float(row["open"]) + offset,
                    high=float(row["high"]) + offset,
                    low=float(row["low"]) + offset,
                    close=float(row["close"]) + offset,
                    volume=int(row["volume"]),
                    source="release_smoke",
                )
                for row in fake_shadow_bars(benchmark_symbol, base=20.0 + offset)
            ]
            benchmark_bars[benchmark_symbol] = bars
            benchmark_validation[benchmark_symbol] = {"status": "VALID", "detail": "release_smoke"}
        return ShadowMarketBundle(
            symbol=symbol,
            benchmark_symbols=tuple(benchmark_symbols),
            frequency=frequency,
            start_timestamp=symbol_bars[0].timestamp,
            end_timestamp=symbol_bars[-1].timestamp,
            symbol_bars=symbol_bars,
            benchmark_bars=benchmark_bars,
            benchmark_validation=benchmark_validation,
            source="release_smoke",
        )


workspace_root = Path.cwd()
runtime = ShadowRuntimeConfig(
    output_dir=Path("artifacts/shadow/release_smoke"),
    symbol="SOXS.US",
    benchmark_symbols=("SOXX.US", "SMH.US"),
    frequency="15m",
    timeframe="15m",
    strategy_version="baseline",
    strategy_family="range_etf",
    risk_profile="strict",
    regular_session_only=True,
    shadow_enabled=True,
    trading_enabled=False,
    symbol_class="inverse_etf",
    lookback_days=30,
    initial_cash=10_000.0,
    page_size=4,
    max_retries=3,
    request_interval_seconds=0.0,
    poll_interval_seconds=1.0,
    run_once=True,
)
observer = ShadowObserver(
    safety_config=ShadowSafetyConfig.from_env(),
    runtime_config=runtime,
    market_source=_FakeShadowMarketSource(),
    state_store=ShadowRuntimeStateStore(runtime.output_dir / "runtime_state.json"),
)
result = observer.run_once()
snapshot_path = workspace_root / "state" / "dashboard_snapshots" / "shadow_status.json"
payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
print(json.dumps({
    "result_keys": sorted(result.keys()),
    "ok": result.get("ok"),
    "snapshot_generated_at": payload.get("generated_at"),
    "snapshot_source_run_id": payload.get("source_run_id"),
    "snapshot_data_keys": sorted((payload.get("data") or {}).keys()),
}, ensure_ascii=False))
        """,
    )
    elapsed = time.perf_counter() - started
    return {
        "elapsed_seconds": round(elapsed, 3),
        "result_keys": list(summary.get("result_keys") or []),
        "snapshot_generated_at": summary.get("snapshot_generated_at"),
        "snapshot_source_run_id": summary.get("snapshot_source_run_id"),
        "snapshot_data_keys": list(summary.get("snapshot_data_keys") or []),
        "ok": summary.get("ok"),
    }


def _verify_telegram_mock(
    workspace_root: Path,
    *,
    selection_report: dict[str, Any],
    top_configs: list[dict[str, Any]],
) -> dict[str, Any]:
    ledger_path = workspace_root / "state" / "notifications" / "notification_ledger.jsonl"
    summary = _run_python_json(
        workspace_root,
        f"""
        import json
        import os
        import time
        from pathlib import Path

        from src.notifier import alerts

        selection_report = json.loads({json.dumps(selection_report, ensure_ascii=False, sort_keys=True, default=str)!r})
        top_configs = json.loads({json.dumps(top_configs, ensure_ascii=False, sort_keys=True, default=str)!r})
        notification_cfg = {{
            "ai_selector_telegram_bot_token": "release-smoke-token",
            "ai_selector_telegram_chat_id": "release-public-chat",
            "ai_selector_admin_chat_id": "release-admin-chat",
            "trade_summary_interval": 5,
        }}

        workspace_root = Path.cwd()
        ledger_path = workspace_root / "state" / "notifications" / "notification_ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        old_ledger = os.environ.get("SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH")
        os.environ["SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH"] = str(ledger_path)

        old_loader = alerts._load_ai_selector_notification_config
        old_send = alerts.Notifier._telegram_send
        try:
            alerts._load_ai_selector_notification_config = lambda: dict(notification_cfg)

            def _fake_send(self, *args, **kwargs):
                return alerts.TelegramDeliveryResult(
                    configured=True,
                    attempted=True,
                    success=True,
                    chunks_total=1,
                    chunks_successful=1,
                )

            alerts.Notifier._telegram_send = _fake_send
            started = time.perf_counter()
            alerts.notify_ai_selection_result(selection_report, top_configs=top_configs)
            elapsed = time.perf_counter() - started
            if not ledger_path.exists():
                raise RuntimeError("telegram smoke did not produce notification ledger")
            ledger_lines = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            latest = ledger_lines[-1] if ledger_lines else {{}}
            print(json.dumps({{
                "elapsed_seconds": round(elapsed, 3),
                "ledger_path": str(ledger_path.relative_to(workspace_root)),
                "ledger_entries": len(ledger_lines),
                "status": latest.get("status"),
                "final_status": latest.get("final_status"),
                "public_channel_ok": latest.get("public_channel_ok"),
                "admin_channel_ok": latest.get("admin_channel_ok"),
                "telegram_attempted": latest.get("telegram_attempted"),
            }}, ensure_ascii=False))
        finally:
            alerts._load_ai_selector_notification_config = old_loader
            alerts.Notifier._telegram_send = old_send
            if old_ledger is None:
                os.environ.pop("SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH", None)
            else:
                os.environ["SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH"] = old_ledger
        """,
    )
    ledger_entries = int(summary.get("ledger_entries") or 0)
    if ledger_entries <= 0:
        raise ReleaseSmokeError("telegram smoke did not produce notification ledger")
    return {
        "elapsed_seconds": round(float(summary.get("elapsed_seconds") or 0.0), 3),
        "ledger_path": str((workspace_root / str(summary.get("ledger_path") or "")).relative_to(workspace_root)),
        "ledger_entries": ledger_entries,
        "status": summary.get("status"),
        "final_status": summary.get("final_status"),
        "public_channel_ok": summary.get("public_channel_ok"),
        "admin_channel_ok": summary.get("admin_channel_ok"),
        "telegram_attempted": summary.get("telegram_attempted"),
    }


def _verify_paper_broker(workspace_root: Path) -> dict[str, Any]:
    from src.broker.paper_broker import PaperBroker

    started = time.perf_counter()
    broker = PaperBroker(persist_portfolio_state=False)
    account = broker.get_account()
    elapsed = time.perf_counter() - started
    return {
        "elapsed_seconds": round(elapsed, 3),
        "cash": getattr(account, "cash", None),
        "equity": getattr(account, "equity", None),
        "buying_power": getattr(account, "buying_power", None),
        "positions": len(getattr(account, "positions", []) or []),
    }


def _verify_system_health(workspace_root: Path) -> dict[str, Any]:
    import importlib.util

    script_path = workspace_root / "scripts" / "system_health.py"
    spec = importlib.util.spec_from_file_location("release_smoke_system_health", script_path)
    if spec is None or spec.loader is None:
        raise ReleaseSmokeError("unable to load system_health.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    report = module.generate_report(project_dir=workspace_root)
    if not isinstance(report, dict):
        raise ReleaseSmokeError("system_health report is invalid")
    for section in ("scheduler", "ai_selector", "notifier", "paper_portfolio", "orphan_monitor", "processes"):
        if section not in report:
            raise ReleaseSmokeError(f"system_health missing section: {section}")
    return {
        "sections": sorted(report.keys()),
        "orphan_monitor_keys": sorted((report.get("orphan_monitor") or {}).keys()),
        "scheduler_keys": sorted((report.get("scheduler") or {}).keys()),
        "ai_selector_keys": sorted((report.get("ai_selector") or {}).keys()),
    }


def _verify_dashboard_api_status(workspace_root: Path) -> dict[str, Any]:
    import importlib.util
    from werkzeug.serving import make_server

    dashboard_path = workspace_root / "src" / "dashboard" / "combined.py"
    spec = importlib.util.spec_from_file_location("release_smoke_dashboard", dashboard_path)
    if spec is None or spec.loader is None:
        raise ReleaseSmokeError("unable to load dashboard combined.py")
    dashboard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dashboard
    spec.loader.exec_module(dashboard)

    # The smoke gate should hit snapshot-first paths, never the slow fallbacks.
    dashboard._candidate_validation_snapshot = lambda: (_ for _ in ()).throw(AssertionError("candidate_validation fallback should not run"))
    dashboard._candidate_model_evaluation_snapshot = lambda: (_ for _ in ()).throw(AssertionError("candidate_model_evaluation fallback should not run"))
    dashboard._candidate_research_report_snapshot = lambda: (_ for _ in ()).throw(AssertionError("candidate_research_report fallback should not run"))
    dashboard._shadow_status_snapshot = lambda: (_ for _ in ()).throw(AssertionError("shadow_status fallback should not run"))

    server = make_server("127.0.0.1", 0, dashboard.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/status"
        started = time.perf_counter()
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read().decode("utf-8")
            status_code = response.status
        elapsed = time.perf_counter() - started
        payload = json.loads(body)
        if status_code != 200 or not isinstance(payload, dict) or not payload.get("ok"):
            raise ReleaseSmokeError(f"/api/status returned unexpected payload: status={status_code}")
        if elapsed > 5.0:
            raise ReleaseSmokeError(f"/api/status too slow after snapshot path: {elapsed:.3f}s")
        return {
            "status_code": status_code,
            "latency_seconds": round(elapsed, 3),
            "selection_run_id": str((payload.get("ai_selection") or {}).get("selection_run_id") or ""),
            "candidate_validation_status": str((payload.get("candidate_validation") or {}).get("status_label") or ""),
            "candidate_model_status": str((payload.get("candidate_model_evaluation") or {}).get("approval_status") or ""),
            "shadow_status": str((payload.get("shadow") or {}).get("status_label") or ""),
            "research_report_status": str((payload.get("research_report") or {}).get("status_label") or ""),
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _scan_tracebacks(workspace_root: Path) -> list[str]:
    paths: list[str] = []
    logs_root = workspace_root / "logs"
    if not logs_root.exists():
        return paths
    for path in logs_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".log", ".err", ".out", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if "Traceback (most recent call last)" in text or "Traceback:" in text:
            paths.append(str(path.relative_to(workspace_root)))
    return paths


def run_release_smoke(
    *,
    repo_root: Path | None = None,
    mode: str = DEFAULT_SELECTOR_MODE,
    universe_source: str = DEFAULT_UNIVERSE_SOURCE,
) -> dict[str, Any]:
    source_root = Path(repo_root or PROJECT_DIR).resolve()
    with _smoke_workspace(source_root) as workspace_root:
        selector = _run_selector_smoke(workspace_root, universe_source=universe_source, mode=mode)
        candidate_model = _write_candidate_model_evaluation_snapshot(workspace_root)
        candidate_research = _write_candidate_research_snapshot(workspace_root)
        shadow = _write_shadow_snapshot(workspace_root)
        telegram = _verify_telegram_mock(workspace_root, selection_report=selector["report"], top_configs=selector["top_configs"])
        paper = _verify_paper_broker(workspace_root)
        system_health = _verify_system_health(workspace_root)
        dashboard = _verify_dashboard_api_status(workspace_root)
        traceback_files = _scan_tracebacks(workspace_root)
        if traceback_files:
            raise ReleaseSmokeError(f"tracebacks detected in logs: {traceback_files}")

        summary = {
            "workspace_root": str(workspace_root),
            "selector": selector,
            "candidate_model_evaluation": candidate_model,
            "candidate_research_report": candidate_research,
            "shadow_status": shadow,
            "telegram": telegram,
            "paper_broker": paper,
            "system_health": system_health,
            "dashboard": dashboard,
            "traceback_files": traceback_files,
        }
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the QuantCairn release smoke test.")
    parser.add_argument("--mode", default=DEFAULT_SELECTOR_MODE, help="Selector mode for the smoke run.")
    parser.add_argument(
        "--universe-source",
        default=DEFAULT_UNIVERSE_SOURCE,
        choices=("sample", "managed", "sp500"),
        help="Selector universe source for the smoke run.",
    )
    args = parser.parse_args(argv)

    try:
        summary = run_release_smoke(mode=args.mode, universe_source=args.universe_source)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, **summary}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
