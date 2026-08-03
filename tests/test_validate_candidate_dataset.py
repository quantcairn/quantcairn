from __future__ import annotations

import os
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.candidate_validation import (
    CandidateRecord,
    CandidateTransitionError,
    CandidateValidationStore,
    ValidationStatus,
)
from src.candidate_validation import dataset_validation_runner as runner


def _write_ohlcv_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "frequency", "timezone", "source"]
    lines = [",".join(header)]
    for row in rows:
        lines.append(
            ",".join(
                str(row.get(column, ""))
                for column in header
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_pending_candidate(store: CandidateValidationStore, *, symbol: str, benchmarks: tuple[str, ...], asset_type: str, strategy_family: str, risk_profile: str = "strict") -> CandidateRecord:
    candidate = CandidateRecord.from_ai_candidate(
        symbol=symbol,
        selected_at="2026-07-10T21:32:41+08:00",
        source="ai_selector",
        ai_score=91.2,
        ai_reason="manual validation smoke",
        asset_type=asset_type,
        benchmarks=benchmarks,
        strategy_family=strategy_family,
        risk_profile=risk_profile,
        timeframe="15m",
        market="US",
        metadata={"source": "test"},
    )
    store.save_candidates([candidate])
    for status in (
        ValidationStatus.CLASSIFIED,
        ValidationStatus.BENCHMARK_ASSIGNED,
        ValidationStatus.STRATEGY_ASSIGNED,
        ValidationStatus.PENDING_DATA_VALIDATION,
    ):
        store.transition(candidate.candidate_id, status, metadata={})
    return store.get_candidate(candidate.candidate_id)  # type: ignore[return-value]


def _two_bar_csv_rows(symbol: str, *, base_utc: str = "2026-07-10T13:30:00+00:00") -> list[dict[str, object]]:
    first = datetime.fromisoformat(base_utc)
    second = first.replace(minute=45)
    return [
        {
            "symbol": symbol,
            "timestamp": first.isoformat(),
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "close": 10.2,
            "volume": 1000,
            "frequency": "15m",
            "timezone": "UTC",
            "source": "unit_test",
        },
        {
            "symbol": symbol,
            "timestamp": second.isoformat(),
            "open": 10.2,
            "high": 10.6,
            "low": 10.1,
            "close": 10.4,
            "volume": 1200,
            "frequency": "15m",
            "timezone": "UTC",
            "source": "unit_test",
        },
    ]


def test_validate_candidate_dataset_dry_run_leaves_state_unchanged(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )
    data_root = tmp_path / "data"
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", _two_bar_csv_rows("AAPL.US"))
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", _two_bar_csv_rows("QQQ.US"))
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)

    result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation",
        download_missing=False,
        dry_run=True,
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.PENDING_DATA_VALIDATION.value
    assert result["dry_run"] is True
    assert result["new_status"] == ValidationStatus.DATA_VALID.value
    assert json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))["applied"] is False
    assert json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))["candidate_current_status"] == ValidationStatus.PENDING_DATA_VALIDATION.value
    assert result["new_status"] == ValidationStatus.DATA_VALID.value
    assert Path(result["report_path"]).exists()
    assert result["data_sources"][0]["source"] == "local"


def test_validate_candidate_dataset_apply_updates_state_and_metadata(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )
    data_root = tmp_path / "data"
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", _two_bar_csv_rows("AAPL.US"))
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", _two_bar_csv_rows("QQQ.US"))
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)

    result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation",
        download_missing=False,
        dry_run=False,
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.DATA_VALID.value
    assert refreshed.metadata["dataset_validation_report_path"] == str(Path(candidate.candidate_id) / "dataset_validation_report.json")
    assert refreshed.metadata["validator_version"] == runner.VALIDATOR_VERSION
    assert refreshed.trading_enabled is False
    assert refreshed.shadow_enabled is False
    assert refreshed.paper_enabled is False
    assert refreshed.live_enabled is False
    report_payload = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert report_payload["applied"] is True
    assert report_payload["candidate_current_status"] == ValidationStatus.PENDING_DATA_VALIDATION.value
    assert report_payload["validation_status_after"] == ValidationStatus.DATA_VALID.value
    history = store.load_latest_history()
    assert any(item["to_status"] == ValidationStatus.DATA_VALID.value for item in history)
    assert all(item["to_status"] != ValidationStatus.PENDING_BACKTEST.value for item in history)


def test_validate_candidate_dataset_rejects_non_pending_candidate(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = CandidateRecord.from_ai_candidate(
        symbol="AAPL.US",
        selected_at="2026-07-10T21:32:41+08:00",
        asset_type="common_stock",
        benchmarks=("QQQ.US",),
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
        timeframe="15m",
    )
    store.save_candidates([candidate])
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", tmp_path / "data")

    with pytest.raises(CandidateTransitionError, match="candidate_must_be_pending_data_validation"):
        runner.validate_candidate_dataset(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=tmp_path / "validation",
            download_missing=False,
            dry_run=True,
        )


def test_validate_candidate_dataset_missing_benchmark_fails_closed(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )
    data_root = tmp_path / "data"
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", _two_bar_csv_rows("AAPL.US"))
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)

    result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation",
        download_missing=False,
        dry_run=False,
    )

    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.DATA_INVALID.value
    assert result["new_status"] == ValidationStatus.DATA_INVALID.value
    assert "missing_symbol_or_benchmark_data" in result["reason"]


def test_validate_candidate_dataset_duplicate_and_future_data_fail_closed(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )
    data_root = tmp_path / "data"
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", _two_bar_csv_rows("AAPL.US"))
    dup_rows = _two_bar_csv_rows("QQQ.US")
    dup_rows[1]["timestamp"] = dup_rows[0]["timestamp"]
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", dup_rows)
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)

    result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation",
        download_missing=False,
        dry_run=False,
    )

    assert result["new_status"] == ValidationStatus.DATA_INVALID.value
    assert "benchmark_data_error" in result["reason"] or "duplicate_timestamp" in result["reason"]


def test_validate_candidate_dataset_download_missing_requires_apply(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", tmp_path / "missing_data")
    called: list[tuple[str, str]] = []

    def _fake_download_missing_data(**kwargs):
        called.append((kwargs["symbol"], kwargs["frequency"]))
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / f"{kwargs['symbol'].replace('.', '_')}_{kwargs['frequency']}_20231204_latest.csv"
        _write_ohlcv_csv(file_path, _two_bar_csv_rows(kwargs["symbol"]))
        return file_path

    monkeypatch.setattr(runner, "_download_missing_data", _fake_download_missing_data)

    dry_run_result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation_dry",
        download_missing=False,
        dry_run=True,
    )
    assert called == []
    assert dry_run_result["new_status"] == ValidationStatus.DATA_INVALID.value
    with pytest.raises(CandidateTransitionError, match="download_missing_requires_apply"):
        runner.validate_candidate_dataset(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=tmp_path / "validation_dry_download",
            download_missing=True,
            dry_run=True,
        )

    apply_result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation_apply",
        download_missing=True,
        dry_run=False,
    )
    assert called == [("AAPL.US", "15m"), ("QQQ.US", "15m")]
    assert apply_result["new_status"] == ValidationStatus.DATA_VALID.value
    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.validation_status == ValidationStatus.DATA_VALID.value
    assert called == [("AAPL.US", "15m"), ("QQQ.US", "15m")]


def test_validate_candidate_dataset_cli_runs_on_known_files(tmp_path):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="SOXS.US",
        benchmarks=("SOXX.US",),
        asset_type="inverse_etf",
        strategy_family="range_etf",
        risk_profile="strict",
    )
    project_dir = tmp_path / "project"
    data_root = project_dir / "data" / "market" / "longbridge"
    _write_ohlcv_csv(data_root / "SOXS_US_15m.csv", _two_bar_csv_rows("SOXS.US"))
    _write_ohlcv_csv(data_root / "SOXX_US_15m.csv", _two_bar_csv_rows("SOXX.US"))
    output_dir = tmp_path / "out"
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate_candidate_dataset.py"
    env = os.environ.copy()
    env["SOXS_PROJECT_DIR"] = str(project_dir)
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--candidate-id",
            candidate.candidate_id,
            "--candidate-store",
            str(store.root_dir),
            "--output-dir",
            str(output_dir),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["candidate_id"] == candidate.candidate_id
    assert payload["new_status"] == ValidationStatus.DATA_VALID.value
    assert payload["candidate_current_status"] == ValidationStatus.PENDING_DATA_VALIDATION.value
    assert payload["applied"] is False
    assert Path(payload["report_path"]).exists()


def test_validate_candidate_dataset_manifest_contains_file_summaries(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )
    data_root = tmp_path / "data"
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", _two_bar_csv_rows("AAPL.US"))
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", _two_bar_csv_rows("QQQ.US"))
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)

    result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation",
        download_missing=False,
        dry_run=True,
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["symbol"] == "AAPL.US"
    assert manifest["benchmarks"] == ["QQQ.US"]
    assert manifest["timeframe"] == "15m"
    assert manifest["asset_type"] == "common_stock"
    assert manifest["strategy_family"] == "equity_mean_reversion"
    assert manifest["risk_profile"] == "balanced"
    assert manifest["checksums"]
    assert manifest["time_range"]["symbol_start"] is not None
    assert manifest["time_range"]["symbol_end"] is not None


def test_validate_candidate_dataset_rejects_path_escape_inputs(tmp_path):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )

    with pytest.raises(CandidateTransitionError, match="unsafe_path_parameter:output_dir"):
        runner.validate_candidate_dataset(
            candidate_id=candidate.candidate_id,
            candidate_store=store.root_dir,
            output_dir=Path("../escape"),
            download_missing=False,
            dry_run=True,
        )

    with pytest.raises(CandidateTransitionError, match="candidate_not_found"):
        runner.validate_candidate_dataset(
            candidate_id="../bad_id",
            candidate_store=store.root_dir,
            output_dir=tmp_path / "validation",
            download_missing=False,
            dry_run=True,
        )


def test_validate_candidate_dataset_invalid_manifest_fails_closed(tmp_path, monkeypatch):
    store = CandidateValidationStore(tmp_path / "store")
    candidate = _make_pending_candidate(
        store,
        symbol="AAPL.US",
        benchmarks=("QQQ.US",),
        asset_type="common_stock",
        strategy_family="equity_mean_reversion",
        risk_profile="balanced",
    )
    data_root = tmp_path / "data"
    _write_ohlcv_csv(data_root / "AAPL_US_15m.csv", _two_bar_csv_rows("AAPL.US"))
    _write_ohlcv_csv(data_root / "QQQ_US_15m.csv", _two_bar_csv_rows("QQQ.US"))
    (data_root / "manifest.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(runner, "DEFAULT_DATA_ROOT", data_root)

    result = runner.validate_candidate_dataset(
        candidate_id=candidate.candidate_id,
        candidate_store=store.root_dir,
        output_dir=tmp_path / "validation",
        download_missing=False,
        dry_run=False,
    )

    assert result["new_status"] == ValidationStatus.DATA_INVALID.value
    assert "manifest_invalid" in result["reason"]
