from __future__ import annotations

import json
from types import SimpleNamespace

import src.candidate_validation.model_evaluation as model_evaluation_module
from src.candidate_validation import (
    CandidateRecord,
    CandidateModelEvaluationService,
    CandidateModelManifest,
    CandidateModelRegistry,
    CandidateModelStatus,
    CandidateOutcomeDatasetBuilder,
    CandidateScoreCalibrator,
    CandidateOutcomeSample,
    CandidateDailyResearchReportGenerator,
    CandidateValidationStore,
    load_candidate_model_evaluation_snapshot,
)
from src.dashboard import combined as dashboard


def _seed_model_inputs(root):
    candidate_root = root / "candidates"
    backtest_root = root / "backtests"
    store = CandidateValidationStore(candidate_root)
    store.save_candidates(
        [
            CandidateRecord.from_ai_candidate(
                symbol="AAPL.US",
                selected_at="2026-07-01T00:00:00+00:00",
                source="model_evaluation_test",
                ai_score=90.0,
                candidate_score=92.0,
                liquidity_score=90.0,
                trend_score=85.0,
                volatility_score=80.0,
                risk_score=75.0,
                strategy_fit_score=88.0,
                recommended_strategy="mean_reversion",
                data_mode="live",
                data_freshness="fresh",
                data_status="VALID",
                scoring_eligible=True,
                trade_filter_passed=True,
                asset_type="common_stock",
                benchmarks=("QQQ.US", "SPY.US"),
                strategy_family="equity_mean_reversion",
                risk_profile="balanced",
                metadata={"data_as_of": "2026-07-01T00:00:00+00:00"},
            )
        ]
    )
    artifact_dir = backtest_root / "sample-run"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "comparison_summary.json").write_text(
        json.dumps(
            {
                "symbol": "AAPL.US",
                "generated_at": "2026-06-30T00:00:00Z",
                "data_end": "2026-06-30T00:00:00Z",
                "summary": {"best_version": "baseline", "ranking_status": "PASS"},
                "metrics": [
                    {
                        "version": "baseline",
                        "reconciliation_status": "OK",
                        "evidence_status": "ELIGIBLE",
                        "total_return": 0.05,
                        "max_drawdown": 0.1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return candidate_root, backtest_root


def test_candidate_outcome_dataset_builder_produces_leakage_safe_samples(tmp_path):
    candidate_root, backtest_root = _seed_model_inputs(tmp_path)
    dataset = CandidateOutcomeDatasetBuilder(candidate_root=candidate_root, backtest_root=backtest_root).build()

    assert dataset.sample_count > 0
    assert dataset.warnings == []
    assert dataset.training_period["start"] is not None
    assert dataset.training_period["end"] is not None
    assert dataset.training_period["start"] <= dataset.training_period["end"]
    for sample in dataset.samples:
        assert sample.candidate_id
        assert sample.symbol
        assert sample.selection_date
        assert sample.data_as_of
        assert not sample.leakage_reason
        assert sample.label_data_valid in {True, False}
        assert sample.label_backtest_pass in {True, False}
        assert sample.label_walk_forward_pass in {True, False}
        assert sample.label_positive_return in {True, False, None}
        assert sample.label_max_drawdown_ok in {True, False, None}
        if sample.artifact_path:
            assert sample.artifact_path.endswith(".json")


def test_candidate_score_calibration_preserves_raw_scores():
    samples = [
        CandidateOutcomeSample(
            candidate_id="cand_1",
            symbol="AAPL.US",
            selection_date="2026-07-01T00:00:00+00:00",
            data_as_of="2026-07-01T00:00:00+00:00",
            scoring_version="v1",
            liquidity_score=90.0,
            trend_score=85.0,
            volatility_score=80.0,
            risk_score=75.0,
            strategy_fit_score=88.0,
            candidate_score=92.0,
            recommended_strategy="mean_reversion",
            data_quality_score=95.0,
            validation_result="DATA_VALID",
            backtest_result="BACKTEST_COMPLETE",
            walk_forward_result="WALK_FORWARD_COMPLETE",
            shadow_result="SHADOW_COMPLETE",
            paper_result="PAPER_ELIGIBLE",
            outcome_window="2026-06-01 -> 2026-06-30",
            label_data_valid=True,
            label_backtest_pass=True,
            label_walk_forward_pass=True,
            label_positive_return=True,
            label_max_drawdown_ok=True,
        ),
        CandidateOutcomeSample(
            candidate_id="cand_2",
            symbol="SOXS.US",
            selection_date="2026-07-02T00:00:00+00:00",
            data_as_of="2026-07-02T00:00:00+00:00",
            scoring_version="v1",
            liquidity_score=60.0,
            trend_score=40.0,
            volatility_score=45.0,
            risk_score=55.0,
            strategy_fit_score=50.0,
            candidate_score=68.0,
            recommended_strategy="inverse_range",
            data_quality_score=70.0,
            validation_result="DATA_VALID",
            backtest_result="BACKTEST_FAILED",
            walk_forward_result="WALK_FORWARD_FAILED",
            shadow_result="SHADOW_OBSERVING",
            paper_result="PAPER_INELIGIBLE",
            outcome_window="2026-06-01 -> 2026-06-30",
            label_data_valid=True,
            label_backtest_pass=False,
            label_walk_forward_pass=False,
            label_positive_return=False,
            label_max_drawdown_ok=False,
        ),
    ]

    calibrator = CandidateScoreCalibrator()
    calibration = calibrator.fit(samples, model_version="calibration_test")

    probability, calibrated_score = calibration.calibrate(92.0)
    assert probability is not None
    assert calibrated_score is not None
    assert calibrated_score != 92.0
    raw_probability, raw_score = calibration.calibrate(68.0)
    assert raw_probability is not None
    assert raw_score is not None
    assert calibration.raw_score_min is not None
    assert calibration.raw_score_max is not None
    assert calibration.sample_count == 2
    assert calibration.calibration_error is not None
    assert calibration.to_dict()["source_metric"] == "composite_target"
    assert calibration.to_dict()["warnings"] is not None


def test_candidate_model_evaluation_write_publishes_dashboard_snapshot(monkeypatch, tmp_path):
    report = {
        "title": "Candidate Model Evaluation",
        "generated_at": "2026-08-02T10:00:00+00:00",
        "source_run_id": "model-eval-run-1",
        "baseline_version": "baseline_v1",
        "training_sample_count": 12,
    }
    calls = []

    monkeypatch.setattr(CandidateModelEvaluationService, "evaluate", lambda self: dict(report))

    def fake_write_dashboard_snapshot(name, data, *, source_run_id=None, generated_at=None, state_dir=None):
        calls.append(
            {
                "name": name,
                "data": dict(data),
                "source_run_id": source_run_id,
                "generated_at": generated_at,
                "state_dir": state_dir,
            }
        )
        return tmp_path / "state" / "dashboard_snapshots" / f"{name}.json"

    monkeypatch.setattr(model_evaluation_module, "write_dashboard_snapshot", fake_write_dashboard_snapshot)

    service = CandidateModelEvaluationService(evaluation_root=tmp_path / "evaluation")
    result = service.write()

    assert result == report
    artifact_path = tmp_path / "evaluation" / "candidate_model_evaluation.json"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == report
    assert calls == [
        {
            "name": "candidate_model_evaluation",
            "data": report,
            "source_run_id": "model-eval-run-1",
            "generated_at": "2026-08-02T10:00:00+00:00",
            "state_dir": None,
        }
    ]


def test_candidate_model_evaluation_dashboard_snapshot_failure_does_not_abort(monkeypatch, tmp_path, capsys):
    report = {
        "title": "Candidate Model Evaluation",
        "generated_at": "2026-08-02T10:00:00+00:00",
        "baseline_version": "baseline_v1",
        "training_sample_count": 12,
    }

    monkeypatch.setattr(CandidateModelEvaluationService, "evaluate", lambda self: dict(report))

    def fail_write_dashboard_snapshot(*args, **kwargs):
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(model_evaluation_module, "write_dashboard_snapshot", fail_write_dashboard_snapshot)

    service = CandidateModelEvaluationService(evaluation_root=tmp_path / "evaluation")
    result = service.write()

    assert result == report
    artifact_path = tmp_path / "evaluation" / "candidate_model_evaluation.json"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == report
    assert "failed to write candidate model evaluation dashboard snapshot" in capsys.readouterr().out


def test_candidate_model_registry_requires_human_approval_for_active(tmp_path):
    registry = CandidateModelRegistry(tmp_path)
    manifest = CandidateModelManifest(
        model_version="proposal_test",
        sample_count=120,
        feature_names=["liquidity_score", "trend_score", "volatility_score", "risk_score", "strategy_fit_score"],
        weights={
            "liquidity_score": 0.30,
            "trend_score": 0.25,
            "volatility_score": 0.15,
            "risk_score": 0.15,
            "strategy_fit_score": 0.15,
        },
        target_definition="composite_target",
    )
    assert manifest.can_transition(CandidateModelStatus.BACKTESTED)
    assert not manifest.can_transition(CandidateModelStatus.ACTIVE)
    backtested = manifest.transition(CandidateModelStatus.BACKTESTED)
    validated = backtested.transition(CandidateModelStatus.WALK_FORWARD_VALIDATED)
    review_required = validated.transition(CandidateModelStatus.REVIEW_REQUIRED)
    approved = review_required.transition(CandidateModelStatus.APPROVED, approved_by_human=True, reason="manual review")
    assert approved.approval_status == CandidateModelStatus.APPROVED.value
    assert not approved.can_transition(CandidateModelStatus.ACTIVE)
    assert approved.can_transition(CandidateModelStatus.ACTIVE, approved_by_human=True)
    active = approved.transition(CandidateModelStatus.ACTIVE, approved_by_human=True, reason="human approved")
    path = registry.save_manifest(active)
    assert path.exists()
    loaded = registry.load_manifest("proposal_test")
    assert loaded is not None
    assert loaded.approval_status == CandidateModelStatus.ACTIVE.value
    assert loaded.status == CandidateModelStatus.ACTIVE.value


def test_candidate_model_evaluation_snapshot_is_read_only_and_exposed_in_dashboard(monkeypatch, tmp_path):
    candidate_root, backtest_root = _seed_model_inputs(tmp_path / "artifacts")
    monkeypatch.setenv("SOXS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setattr(dashboard, "_fetch_live_account_summary", lambda: (_ for _ in ()).throw(AssertionError("broker should not be called")))
    monkeypatch.setattr(
        dashboard,
        "_load_dashboard_config",
        lambda: SimpleNamespace(
            mode="paper",
            broker=SimpleNamespace(
                longbridge=SimpleNamespace(
                    enabled=False,
                    environment="prod",
                    account_type="",
                    allow_live_order=False,
                )
            ),
        ),
    )
    monkeypatch.setattr(dashboard, "_load_ai_selection_report", lambda: {"execution_status": "COMPLETED", "result_quality": "DEGRADED", "research_admission": "RESEARCH_ONLY", "selection_stage": "FINALIZED", "fallback_used": True, "top_n_missing_count": 1, "top3": []})
    monkeypatch.setattr(dashboard, "summarize_trade_log", lambda *args, **kwargs: {"execution_mode": "paper", "reduce_only": False, "new_entries_allowed": True, "risk_pause_reason": "", "decision_count": 0, "execution_count": 0, "buy_count": 0, "sell_count": 0, "order_qty": 0, "tickers": [], "latest_line": ""})
    monkeypatch.setattr(dashboard, "_selection_sync_status", lambda: {"ok": True, "mismatch_reason": "", "selection_state_symbols": ["SOFI"], "current_top_config_symbols": ["SOFI"], "required_date": "2026-07-15", "state_date": "2026-07-15"})
    monkeypatch.setattr(dashboard, "_load_top_modes", lambda: ["paper", "paper", "paper"])
    monkeypatch.setattr(dashboard, "_combined_process_count", lambda: 1)
    monkeypatch.setattr(dashboard, "_load_config_defaults", lambda name: {"ticker": name.replace(".yaml", ""), "initial_capital": 700.0, "support": 10.0, "resistance": 12.0})
    monkeypatch.setattr(dashboard, "_fetch_status", lambda port: None)
    monkeypatch.setattr(dashboard, "has_live_top_configs", lambda: False)

    snapshot = load_candidate_model_evaluation_snapshot(candidate_root=candidate_root, backtest_root=backtest_root)
    assert snapshot["title"] == "Candidate Model Evaluation"
    assert snapshot["training_sample_count"] > 0
    assert snapshot["baseline_version"] == "baseline_v1"

    client = dashboard.app.test_client()
    response = client.get("/api/candidate-model/evaluation")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["title"] == "Candidate Model Evaluation"
    assert payload["training_sample_count"] > 0
    assert payload["approval_status"] in {"DRAFT", "BACKTESTED", "WALK_FORWARD_VALIDATED", "REVIEW_REQUIRED", "APPROVED", "ACTIVE", "REJECTED", "RETIRED"}
    api_status = client.get("/api/status").get_json()
    assert api_status["candidate_model_evaluation"]["title"] == "Candidate Model Evaluation"
    assert api_status["candidate_model_evaluation"]["baseline_version"] == "baseline_v1"
    html = client.get("/").data.decode("utf-8")
    assert "候选模型评估" in html
    assert "基准模型完整指标" in html
    assert "挑战模型完整指标" in html
    assert "校准曲线" in html
    assert "候选权重" in html
    assert "submit_order" not in html
    assert "APP_KEY" not in response.get_data(as_text=True)


def test_daily_research_report_includes_candidate_model_evaluation(tmp_path):
    report_root = tmp_path / "research"
    generator = CandidateDailyResearchReportGenerator(root_dir=report_root)
    report = generator.build()
    assert report["candidate_model_evaluation"]["title"] == "Candidate Model Evaluation"
    assert report["candidate_model_evaluation"]["baseline_version"] == "baseline_v1"
    assert "candidate_model_evaluation" in report

    generator.write()
    assert generator.json_path.exists()
    assert generator.markdown_path.exists()
    markdown = generator.markdown_path.read_text(encoding="utf-8")
    assert "## Candidate Model Evaluation" in markdown
    assert "Baseline Metrics" in markdown
    assert "Challenger Metrics" in markdown
