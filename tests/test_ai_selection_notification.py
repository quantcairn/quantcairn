from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.notifier import alerts


@pytest.fixture(autouse=True)
def _isolate_notification_side_effects(monkeypatch):
    class NetworkForbiddenNotifier:
        def __init__(self, *args, **kwargs):
            self._telegram_enabled = False
            self.webhook_url = ""

        def _send(self, *args, **kwargs):
            raise AssertionError("notification test attempted a remote send")

    monkeypatch.setattr(alerts, "load_committed_selection_bundle", lambda *args, **kwargs: None)
    monkeypatch.setattr(alerts, "_load_ai_selector_notification_config", lambda: {})
    monkeypatch.setattr(alerts, "Notifier", NetworkForbiddenNotifier)
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TG_BOT_TOKEN",
        "TG_CHAT_ID",
        "WEBHOOK_URL",
        "SOXS_OPENALPHA_WEBHOOK",
        "OPENALPHA_WEBHOOK",
        "SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN",
        "SOXS_OPENALPHA_TELEGRAM_CHAT_ID",
        "SOXS_TELEGRAM_BOT_TOKEN",
        "SOXS_TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _fixed_notification_time() -> datetime:
    return datetime(2026, 7, 18, 6, 25, tzinfo=ZoneInfo("Asia/Shanghai"))


def _sample_top(rank: int, ticker: str, *, fallback_used: bool = False, reason: str = "High range fitness") -> dict:
    return {
        "ticker": ticker,
        "ai_score": 82 + rank,
        "range_score": 90 - rank,
        "final_score": 88 - rank,
        "current_price": 4.5 if ticker == "SOXS" else 17.73,
        "size": 63 - rank,
        "leveraged_etf": ticker in {"SOXS", "LABD", "DRIP"},
        "trade_filter_passed": True,
        "current_validation_status": "DATA_VALID",
        "trade_admission_status": "TRADABLE",
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "score_source": "current_run_candidate_ranking",
        "score_provider": "local_factor_scoring",
        "score_generated_at": "2026-07-09T09:00:00-04:00",
        "score_is_current_run": True,
        "selection_penalty_reason": reason,
        "selection": {
            "selection_date": "2026-07-09",
            "ai_score": 82 + rank,
            "range_score": 90 - rank,
            "final_score": 88 - rank,
            "trade_filter_passed": True,
            "leveraged_etf": ticker in {"SOXS", "LABD", "DRIP"},
            "fallback_used": fallback_used,
            "reason": reason,
        },
        "allocation": {
            "target_capital": 315.0,
            "target_shares": max(1, 65 - rank),
        },
    }


def _sample_report(*, fallback_used: bool = False, selection_count: int = 3) -> dict:
    return {
        "selection_date": "2026-07-09",
        "selection_count": selection_count,
        "target_top_n": 3,
        "fallback_used": fallback_used,
        "providers_used": ["TradingAgents", "FinRobot", "OpenBB"],
        "providers_disabled": ["FMP"],
        "quality_filter_report": {"warnings": ["TradingAgents timeout"]},
        "composition_filter": {"warnings": ["leveraged_etf_limit_reached"]},
        "provider_audit": {
        "tradingagents": {
            "provider_name": "tradingagents",
            "attempted": 3,
            "success": 0,
            "failure": 3,
            "timed_out": 1,
            "fallback_used": 1,
            "mock_used": 1,
            "contributed_fields": ["score", "reason"],
        },
            "finrobot": {
                "provider_name": "finrobot",
                "attempted": 3,
                "success": 2,
                "failure": 1,
                "timed_out": 0,
                "fallback_used": 1,
                "mock_used": 1,
                "contributed_fields": ["score", "reason"],
            },
            "openbb": {
                "provider_name": "openbb",
                "attempted": 3,
                "success": 3,
                "failure": 0,
                "timed_out": 0,
                "fallback_used": 0,
                "mock_used": 0,
                "contributed_fields": ["score", "reason"],
            },
        },
        "provider_outputs": {
            "tradingagents": {"SOXS": {"source": "tradingagents_mock", "reason": "timeout mock fallback"}},
            "finrobot": {"SOXS": {"source": "finrobot_mock", "reason": "mock data"}},
            "openbb": {"SOXS": {"source": "openbb", "reason": "real data"}},
        },
    }


def _sample_report_with_rich_top3() -> dict:
    report = _sample_report()
    report["top3"] = [
        {
            "ticker": "SOXS",
            "ai_score": 82.58,
            "range_score": 71.11,
            "final_score": 77.99,
            "confidence": 0.58,
            "reason": "first pick",
            "source": "selector_core",
            "leveraged_etf": True,
            "trade_filter_passed": True,
            "current_validation_status": "DATA_VALID",
            "trade_admission_status": "TRADABLE",
            "data_status": "COMPLETE",
            "scoring_eligible": True,
            "score_source": "current_run_candidate_ranking",
            "score_provider": "local_factor_scoring",
            "score_generated_at": "2026-07-09T09:00:00-04:00",
            "score_is_current_run": True,
            "selection_date": "2026-07-09",
            "allocation": {
                "target_capital": 4920,
                "target_shares": 1000,
                "weight": 0.15,
                "atr_pct": 0.05,
                "risk_pct": 1.0,
                "reason": "risk_adjusted_allocation",
            },
        },
        {
            "ticker": "SOFI",
            "ai_score": 50.0,
            "range_score": 74.76,
            "final_score": 59.9,
            "confidence": 0.55,
            "reason": "fallback mock",
            "source": "mock",
            "leveraged_etf": False,
            "trade_filter_passed": True,
            "current_validation_status": "DATA_VALID",
            "trade_admission_status": "TRADABLE",
            "data_status": "COMPLETE",
            "scoring_eligible": True,
            "score_source": "current_run_candidate_ranking",
            "score_provider": "local_factor_scoring",
            "score_generated_at": "2026-07-09T09:00:00-04:00",
            "score_is_current_run": True,
            "selection_date": "2026-07-09",
            "allocation": {
                "target_capital": 17821,
                "target_shares": 1000,
                "weight": 0.1,
                "atr_pct": 0.05,
                "risk_pct": 1.0,
                "reason": "risk_adjusted_allocation",
            },
        },
        {
            "ticker": "DRIP",
            "ai_score": 50.73,
            "range_score": 60.95,
            "final_score": 54.82,
            "confidence": 0.35,
            "reason": "fallback mock",
            "source": "mock",
            "leveraged_etf": True,
            "trade_filter_passed": True,
            "current_validation_status": "DATA_VALID",
            "trade_admission_status": "TRADABLE",
            "data_status": "COMPLETE",
            "scoring_eligible": True,
            "score_source": "current_run_candidate_ranking",
            "score_provider": "local_factor_scoring",
            "score_generated_at": "2026-07-09T09:00:00-04:00",
            "score_is_current_run": True,
            "selection_date": "2026-07-09",
            "allocation": {
                "target_capital": 4920,
                "target_shares": 1000,
                "weight": 0.15,
                "atr_pct": 0.05,
                "risk_pct": 1.0,
                "reason": "risk_adjusted_allocation",
            },
        },
    ]
    return report


def _sample_report_with_semantics() -> dict:
    report = _sample_report(fallback_used=True, selection_count=1)
    report.update(
        {
            "execution_status": "COMPLETED",
            "selection_stage": "FINALIZED",
            "result_quality": "DEGRADED",
            "research_admission": "RESEARCH_ONLY",
            "top_n_complete": False,
            "top_n_missing_count": 2,
            "warnings_structured": [
                {
                    "warning_code": "top_n_not_filled",
                    "stage": "FINALIZED",
                    "requested_count": 3,
                    "selected_count": 1,
                    "missing_count": 2,
                    "symbols": ["SOXS"],
                    "details": "final TOP still below requested count",
                },
                {
                    "warning_code": "top_n_not_filled",
                    "stage": "FINALIZED",
                    "requested_count": 3,
                    "selected_count": 1,
                    "missing_count": 2,
                    "symbols": ["SOXS"],
                    "details": "final TOP still below requested count",
                },
            ],
            "provider_audit": {"tradingagents": {"attempted": 1, "fallback_used": 1, "mock_used": 0}},
        }
    )
    report["top3"] = [
        {
            "ticker": "SOXS",
            "ai_score": 82.58,
            "range_score": 71.11,
            "final_score": 77.99,
            "confidence": 0.58,
            "reason": "first pick",
            "source": "selector_core",
            "leveraged_etf": True,
            "trade_filter_passed": True,
            "fallback_used": True,
            "candidate_fallback": True,
            "fallback_sources": ["tradingagents"],
            "mock_used": False,
            "mock_sources": [],
            "data_status": "VALID",
            "current_validation_status": "AI_CANDIDATE",
            "trade_admission_status": "NOT_TRADABLE",
            "fallback_scope": "EXPLANATION_ONLY",
            "fallback_severity": "INFO",
            "affected_fields": ["reason", "summary"],
            "selection_date": "2026-07-09",
            "allocation": {
                "target_capital": 4920,
                "target_shares": 1000,
                "weight": 0.15,
                "atr_pct": 0.05,
                "risk_pct": 1.0,
                "reason": "risk_adjusted_allocation",
            },
        }
    ]
    return report


def _committed_bundle_report(**extra) -> dict:
    report = {
        **_sample_report_with_rich_top3(),
        "selection_run_id": "bundle-run-1",
        "selection_bundle_hash": "bundle-hash-1",
        "selection_date": "2026-07-16",
        "generated_at": "2026-07-17T23:42:21+08:00",
        "selection_stage": "FINALIZED",
        "execution_status": "COMPLETED",
        "pipeline_status": "COMPLETED",
        "result_quality": "COMPLETE",
        "research_admission": "RESEARCH_READY",
        "top_sync_status": "OK",
        "requested_top_n": 3,
        "selected_top_n": 3,
    }
    report.update(extra)
    return report


def test_ai_selection_message_includes_top3():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    title, body = alerts._build_ai_selection_message(
        _sample_report(),
        [
            _sample_top(1, "SOXS"),
            _sample_top(2, "AAPL"),
            _sample_top(3, "SOFI"),
        ],
    )
    monkeypatch.undo()

    assert title == "【AI 选股完成】"
    assert "选股日期来源：report_payload" in body
    assert "TOP1：SOXS" in body
    assert "TOP2：AAPL" in body
    assert "TOP3：SOFI" in body
    assert "状态：成功" not in body
    assert "流程：UNKNOWN" in body
    assert "执行状态：" in body
    assert "结果质量：" in body
    assert "研究准入：" in body
    assert "Provider 尝试：" in body
    assert "类型：杠杆/反向ETF" in body
    assert "仓位：$" in body


def test_ai_selection_message_handles_only_top2():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        _sample_report(selection_count=2),
        [
            _sample_top(1, "SOXS"),
            _sample_top(2, "SOFI"),
        ],
    )
    monkeypatch.undo()

    assert "正式TOP：2/3" in body
    assert "TOP3：空槽" in body
    assert "selected_symbols=SOXS,SOFI" in body
    assert "missing_slots=TOP3" in body
    assert "原因：正式候选不足" in body


def test_ai_selection_message_shows_zero_of_three_formal_top():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        {
            "selection_run_id": "run-zero",
            "selection_date": "2026-07-16",
            "generated_at": "2026-07-17T23:42:21+08:00",
            "requested_top_n": 3,
            "selected_top_n": 0,
            "top_n_missing_count": 3,
            "top3": [],
            "rejection_reason_counts": {"low_dollar_volume": 7, "price_out_of_range": 2},
        },
        [],
    )
    monkeypatch.undo()

    assert "正式TOP：0/3" in body
    assert "缺失槽位：3（TOP1, TOP2, TOP3）" in body
    assert "候选不足主要原因：" in body
    assert "- 低成交额：7" in body
    assert "- 价格超出范围：2" in body


def test_ai_selection_message_warns_on_fallback():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        _sample_report(fallback_used=True),
        [
            _sample_top(1, "SOXS", fallback_used=True),
            _sample_top(2, "SOFI"),
            _sample_top(3, "AAPL"),
        ],
    )
    monkeypatch.undo()

    assert "执行状态：已完成 (COMPLETED)" in body
    assert "结果质量：降级 (DEGRADED)" in body
    assert "研究准入：仅研究 (RESEARCH_ONLY)" in body
    assert "交易含义：本次结果仅供研究" in body
    assert "不建议直接 live" not in body


def test_ai_selection_message_uses_top_level_fields_when_selection_missing():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        _sample_report(),
        [
            {
                "ticker": "SOXS",
                "ai_score": 81.2,
                "range_score": 76.5,
                "final_score": 78.9,
                "current_price": 4.5,
                "size": 119,
                "leveraged_etf": True,
                "trade_filter_passed": True,
                "current_validation_status": "DATA_VALID",
                "trade_admission_status": "TRADABLE",
                "data_status": "COMPLETE",
                "scoring_eligible": True,
                "score_source": "current_run_candidate_ranking",
                "score_provider": "local_factor_scoring",
                "score_generated_at": "2026-07-09T09:00:00-04:00",
                "score_is_current_run": True,
                "selection_penalty_reason": "first pick",
            }
        ],
    )
    monkeypatch.undo()

    assert "分数：final 78.9 / AI 81.2 / Range 76.5" in body
    assert "类型：杠杆/反向ETF" in body
    assert "仓位：$536 / 119股" in body
    assert "理由：first pick" in body


def test_ai_selection_message_merges_report_fields_when_yaml_sparse():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        _sample_report_with_rich_top3(),
        [
            {
                "ticker": "SOXS",
                "selection": {
                    "selection_date": "2026-07-09",
                    "score": 77.99,
                    "reason": "first pick",
                },
            },
            {
                "ticker": "SOFI",
                "selection": {
                    "selection_date": "2026-07-09",
                    "score": 59.9,
                    "reason": "fallback mock",
                },
            },
            {
                "ticker": "DRIP",
                "selection": {
                    "selection_date": "2026-07-09",
                    "score": 54.82,
                    "reason": "fallback mock",
                },
            },
        ],
    )
    monkeypatch.undo()

    assert "分数：final 77.99 / AI 82.58 / Range 71.11" in body
    assert "仓位：$4920 / 1000股" in body
    assert "TOP2：SOFI" in body
    assert "TOP3：DRIP" in body


def test_ai_selection_message_shows_execution_and_result_semantics():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        _sample_report_with_semantics(),
        [
            {
                "ticker": "SOXS",
                "selection": {
                    "selection_date": "2026-07-09",
                    "score": 77.99,
                    "reason": "first pick",
                },
            }
        ],
    )
    monkeypatch.undo()

    assert "执行状态：已完成 (COMPLETED)" in body
    assert "结果质量：降级 (DEGRADED)" in body
    assert "研究准入：仅研究 (RESEARCH_ONLY)" in body
    assert "状态：AI_CANDIDATE" in body
    assert "NOT_TRADABLE" in body
    assert "数据标记：VALID · candidate_fallback=是 · mock=否" in body
    assert "fallback来源：TRADINGAGENTS" in body
    assert "TOP3：空槽" in body
    assert "原因：正式候选不足" in body
    assert "Provider 尝试：" in body
    assert "Provider 成功：" in body
    assert "Provider 超时：" in body
    assert "Provider Mock：" in body
    assert "交易含义：本次流程已完成，但没有生成任何正式可交易候选" in body
    assert "不得进入 Backtest、Walk-Forward、Paper 或 Live" in body
    assert "状态解释：FINALIZED=流程已完成" in body


def test_ai_selection_message_renders_provider_audit_sections():
    report = _sample_report(fallback_used=True)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        report,
        [
            {
                "ticker": "SOFI",
                "selection": {
                    "selection_date": "2026-07-09",
                    "score": 59.9,
                    "reason": "fallback mock",
                },
                "candidate_fallback": True,
                "mock_used": True,
                "fallback_sources": ["finrobot", "tradingagents"],
                "mock_sources": ["finrobot", "tradingagents"],
                "data_status": "INVALID",
                "current_validation_status": "AI_CANDIDATE",
                "trade_admission_status": "NOT_TRADABLE",
                "fallback_scope": "CRITICAL_MARKET_DATA",
                "fallback_severity": "CRITICAL",
                "affected_fields": ["current_price", "close", "atr_20_percentage"],
            }
        ],
    )
    monkeypatch.undo()

    assert "fallback：是" in body
    assert "mock=是" in body
    assert "fallback来源：FINROBOT / TRADINGAGENTS" in body
    assert "mock来源：FINROBOT / TRADINGAGENTS" in body
    assert "Provider 尝试：finrobot / openbb / tradingagents" in body
    assert "Provider 成功：finrobot / openbb" in body
    assert "Provider 超时：tradingagents" in body
    assert "Provider Mock：finrobot / tradingagents" in body
    assert "Provider 真实贡献：openbb" in body
    assert "Provider 模拟解释：finrobot / tradingagents" in body
    assert "fallback范围：CRITICAL_MARKET_DATA" in body
    assert "fallback级别：CRITICAL" in body
    assert "fallback影响：current_price, close, atr_20_percentage" in body


def test_ai_selection_message_uses_manifest_first_bundle_and_time_fields(monkeypatch):
    monkeypatch.setattr(
        alerts,
        "load_committed_selection_bundle",
        lambda *_args, **_kwargs: {
            "report": {
                "selection_run_id": "bundle-run-1",
                "selection_date": "2026-07-16",
                "generated_at": "2026-07-17T23:42:21+08:00",
                "selection_stage": "FINALIZED",
                "result_quality": "DEGRADED",
                "research_admission": "RESEARCH_ONLY",
                "selected_top_n": 1,
                "requested_top_n": 3,
                "top_n_missing_count": 2,
                "top_n_shortfall_reason": "top_n_not_filled",
                "rejection_reason_counts": {
                    "low_dollar_volume": 7,
                    "price_out_of_range": 2,
                    "entry_quality_too_low": 1,
                },
                "top3": [
                    {
                        "ticker": "SOFI",
                        "selection": {
                            "selection_date": "2026-07-16",
                            "score": 59.9,
                            "reason": "bundle top",
                        },
                        "trade_admission_status": "NOT_TRADABLE",
                        "data_status": "VALID",
                    }
                ],
            }
        },
    )
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)

    _, body = alerts._build_ai_selection_message(
        {
            "selection_run_id": "legacy-run-9",
            "selection_date": "2026-07-15",
            "date": "2026-07-14",
            "generated_at": "2026-07-15T08:00:00+08:00",
            "requested_top_n": 3,
            "selected_top_n": 0,
            "top_n_missing_count": 3,
        },
        [
            {
                "ticker": "SOXS",
                "selection": {"selection_date": "2026-07-15", "score": 71.0},
            }
        ],
    )

    assert "选股数据日：2026-07-16（美东交易日）" in body
    assert "选股日期来源：selection_bundle" in body
    assert "结果生成：2026-07-17 11:42 ET" in body
    assert "通知发送：2026-07-18 06:25 北京时间" in body
    assert "正式TOP：0/3" in body
    assert "研究候选：1/3" in body
    assert "可交易候选：0/3" in body
    assert "未准入研究候选：" in body
    assert "研究候选1：SOFI" in body
    assert "缺失槽位：3（TOP1, TOP2, TOP3）" in body
    assert "候选不足主要原因：" in body
    assert "- 低成交额：7" in body
    assert "- 价格超出范围：2" in body
    assert "- 入场质量不足：1" in body
    assert "交易含义：本次流程已完成，但没有生成任何正式可交易候选" in body
    assert "不得进入 Backtest、Walk-Forward、Paper 或 Live" in body


def test_ai_selection_message_shows_structured_no_selection_diagnostics():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        {
            "selection_run_id": "run-zero",
            "selection_date": "2026-07-16",
            "generated_at": "2026-07-17T23:42:21+08:00",
            "requested_top_n": 3,
            "selected_top_n": 0,
            "top_n_missing_count": 3,
            "top3": [],
            "rejection_reason_counts": {
                "trade_admission_not_tradable": 4,
                "validation_status_ai_candidate": 3,
                "research_evidence_failed": 2,
                "unknown": 1,
            },
            "nearest_rejected_candidates": [
                {
                    "symbol": "SOFI",
                    "formal_candidate_score": 82.3,
                    "score_type": "FORMAL",
                    "market_data_sufficiency": "COMPLETE",
                    "formal_scoring_eligibility": True,
                    "research_evidence_status": "FAILED",
                    "trade_admission_status": "NOT_TRADABLE",
                    "rejection_stage": "FORMAL_ELIGIBILITY",
                    "rejection_reason_codes": ["validation_status_ai_candidate"],
                }
            ],
        },
        [],
    )
    monkeypatch.undo()

    assert "- 未取得交易准入：4" in body
    assert "- 仍处于研究候选阶段：3" in body
    assert "- 研究证据不足：2" in body
    assert "最接近入选候选：" in body
    assert "1. SOFI" in body
    assert "评分：82.3（FORMAL）" in body
    assert "行情数据：COMPLETE" in body
    assert "正式评分资格：是" in body
    assert "交易准入：NOT_TRADABLE" in body
    assert "淘汰阶段：FORMAL_ELIGIBILITY" in body
    assert "原因：仍处于研究候选阶段" in body


def test_ai_selection_message_suppresses_unstructured_unknown_shortfall():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    _, body = alerts._build_ai_selection_message(
        {
            "selection_run_id": "run-zero",
            "selection_date": "2026-07-16",
            "generated_at": "2026-07-17T23:42:21+08:00",
            "requested_top_n": 3,
            "selected_top_n": 0,
            "top_n_missing_count": 3,
            "top3": [],
            "rejection_reason_counts": {"unknown": 9},
            "nearest_rejected_candidates": [
                {
                    "symbol": "SOFI",
                    "stage": "UNIVERSE_FILTER",
                    "reason_code": "unknown",
                    "reason_detail": "stage_removed_without_structured_reason",
                }
            ],
        },
        [],
    )
    monkeypatch.undo()

    assert "其他原因：9" not in body
    assert "最接近入选候选：" in body
    assert "暂无可解释的最近候选" in body


def test_ai_selection_message_marks_missing_selection_date_without_today_fallback(monkeypatch):
    monkeypatch.setattr(alerts, "load_committed_selection_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)

    _, body = alerts._build_ai_selection_message(
        {
            "selection_run_id": "legacy-run-10",
            "generated_at": "2026-07-15T08:00:00+08:00",
            "requested_top_n": 3,
            "selected_top_n": 0,
            "top_n_missing_count": 3,
            "top3": [],
        },
        [],
    )

    assert "选股数据日：未知" in body
    assert "选股日期来源：missing" in body
    assert "selection_date_missing" in body
    assert "通知发送：2026-07-18 06:25 北京时间" in body


def test_ai_selection_message_marks_legacy_date_source(monkeypatch):
    monkeypatch.setattr(alerts, "load_committed_selection_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)

    _, body = alerts._build_ai_selection_message(
        {
            "selection_run_id": "legacy-run-11",
            "date": "2026-07-16",
            "generated_at": "2026-07-16T08:00:00+08:00",
            "requested_top_n": 3,
            "selected_top_n": 1,
            "top_n_missing_count": 2,
            "top3": [_sample_top(1, "SOXS")],
        },
        [_sample_top(1, "SOXS")],
    )

    assert "选股数据日：2026-07-16（美东交易日）" in body
    assert "选股日期来源：legacy_date" in body
    assert "正式TOP：1/3" in body


def test_notify_ai_selection_without_telegram_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        alerts,
        "load_committed_selection_bundle",
        lambda *_args, **_kwargs: {"report": _committed_bundle_report()},
    )
    alerts.notify_ai_selection_result(
        _sample_report(),
        [_sample_top(1, "SOXS"), _sample_top(2, "SOFI"), _sample_top(3, "AAPL")],
    )


def test_ai_selection_message_fails_closed_without_quality_semantics(monkeypatch):
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)

    _, body = alerts._build_ai_selection_message(
        _sample_report(),
        [_sample_top(1, "SOXS"), _sample_top(2, "SOFI"), _sample_top(3, "AAPL")],
    )

    assert "结果质量：无效 (INVALID)" in body
    assert "研究准入：已阻止 (BLOCKED)" in body
    assert "result_quality_missing" in body
    assert "research_admission_missing" in body


def test_ai_selection_message_separates_research_only_candidates_from_formal_top(monkeypatch):
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    report = {
        **_sample_report(),
        "execution_status": "COMPLETED",
        "selection_stage": "FINALIZED",
        "result_quality": "DEGRADED",
        "research_admission": "RESEARCH_ONLY",
        "requested_top_n": 3,
        "top3": [
            {
                "ticker": "NVDA",
                "final_score": 91.5,
                "ai_score": 91.5,
                "range_score": 88.0,
                "reason": "research_complete",
                "current_validation_status": "AI_CANDIDATE",
                "trade_admission_status": "NOT_TRADABLE",
                "data_status": "COMPLETE",
                "data_sufficiency": False,
                "scoring_eligible": False,
                "score_source": "UNKNOWN",
                "score_provider": "UNKNOWN",
                "score_is_current_run": False,
            },
            {
                "ticker": "MSFT",
                "final_score": 88.2,
                "ai_score": 88.2,
                "range_score": 84.0,
                "reason": "research_complete",
                "current_validation_status": "AI_CANDIDATE",
                "trade_admission_status": "NOT_TRADABLE",
                "data_status": "COMPLETE",
                "data_sufficiency": False,
                "scoring_eligible": False,
                "score_source": "HISTORICAL",
                "score_provider": "prior_bundle",
                "score_is_current_run": False,
            },
        ],
        "provider_audit": {
            "tradingagents": {"attempted": 1, "success": 0, "failure": 1, "contributed_fields": []},
            "finrobot": {"attempted": 1, "success": 0, "failure": 1, "contributed_fields": []},
            "openbb": {"attempted": 1, "success": 0, "failure": 1, "contributed_fields": []},
        },
    }

    _, body = alerts._build_ai_selection_message(report, report["top3"])

    assert "正式TOP：0/3" in body
    assert "选股结果：NO_TRADABLE_SELECTION" in body
    assert "已产生正式候选：否" in body
    assert "不得进入 Backtest、Walk-Forward、Paper 或 Live" in body
    assert "才可进入 Backtest 或 Paper" not in body
    assert "TOP1：NVDA" not in body
    assert "TOP2：MSFT" not in body
    assert "研究候选1：NVDA" in body
    assert "研究候选2：MSFT" in body
    assert "Trade Admission：NOT_TRADABLE" in body
    assert "分数状态：INVALID_SCORE_PROVENANCE" in body
    assert "Research Evidence：FAILED" in body
    assert "理由：research_complete" not in body


def test_ai_selection_message_renders_mixed_formal_and_research_candidates(monkeypatch):
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    tradable = _sample_top(1, "SOXS")
    research = _sample_top(2, "NVDA")
    research.update(
        {
            "current_validation_status": "AI_CANDIDATE",
            "trade_admission_status": "NOT_TRADABLE",
            "scoring_eligible": False,
            "data_sufficiency": False,
        }
    )
    report = {
        **_sample_report(),
        "execution_status": "COMPLETED",
        "selection_stage": "FINALIZED",
        "result_quality": "DEGRADED",
        "research_admission": "RESEARCH_ONLY",
        "requested_top_n": 3,
        "top3": [tradable, research],
    }

    _, body = alerts._build_ai_selection_message(report, [tradable, research])

    assert "正式TOP：1/3" in body
    assert "TOP1：SOXS" in body
    assert "TOP2：NVDA" not in body
    assert "未准入研究候选：" in body
    assert "研究候选1：NVDA" in body


def test_ai_selection_message_prefers_final_market_data_over_precheck_snapshot(monkeypatch):
    monkeypatch.setattr(alerts, "_current_notification_sent_at", _fixed_notification_time)
    candidate = _sample_top(1, "SOFI")
    candidate.update(
        {
            "current_validation_status": "AI_CANDIDATE",
            "trade_admission_status": "NOT_TRADABLE",
            "data_status": "COMPLETE",
            "record_completeness": "COMPLETE",
            "market_data_sufficiency": "COMPLETE",
            "research_evidence_status": "FAILED",
            "formal_scoring_eligibility": True,
            "scoring_eligible": True,
            "score_type": "FORMAL",
            "score_is_formal": True,
            "quality_state_conflict": True,
            "quality_state_conflict_fields": ["ohlcv_status", "history_status", "scoring_eligible"],
            "data_sufficiency": {
                "data_status": "VALID",
                "quote_status": "OK",
                "ohlcv_status": "MISSING",
                "history_status": "DEGRADED",
                "scoring_eligible": False,
            },
        }
    )
    report = {
        **_sample_report(selection_count=0),
        "execution_status": "COMPLETED",
        "selection_stage": "FINALIZED",
        "result_quality": "DEGRADED",
        "research_admission": "RESEARCH_ONLY",
        "selection_outcome": "NO_TRADABLE_SELECTION",
        "completed_with_selection": False,
        "requested_top_n": 3,
        "selected_top_n": 0,
        "top3": [candidate],
    }

    _, body = alerts._build_ai_selection_message(report, [candidate])

    assert "正式TOP：0/3" in body
    assert "研究候选：1/3" in body
    assert "可交易候选：0/3" in body
    assert "研究候选1：SOFI" in body
    assert "Market Data Sufficiency：COMPLETE" in body
    assert "Data Sufficiency：通过" in body
    assert "Scoring Eligible：是" in body
    assert "评分类型：FORMAL / formal=是" in body
    assert "预检查：VALID · 可评分=否" in body
    assert "状态冲突诊断：预检查与最终状态不一致" in body


def test_ai_selection_message_uses_research_top_candidates_as_validation_path():
    candidate = {
        "ticker": "SOFI",
        "candidate_score": 71.45,
        "final_score": 71.45,
        "score": 71.45,
        "ai_score": 71.45,
        "range_score": 70.0,
        "candidate_id": "cand_SOFI_US_test",
        "validation_status": "AI_CANDIDATE",
        "current_validation_status": "AI_CANDIDATE",
        "trade_admission_status": "NOT_TRADABLE",
        "data_status": "COMPLETE",
        "market_data_sufficiency": "COMPLETE",
        "formal_scoring_eligibility": True,
        "scoring_eligible": True,
        "score_type": "FORMAL",
        "score_is_formal": True,
        "score_source": "current_run_candidate_ranking",
        "score_provider": "local_factor_scoring",
        "score_is_current_run": True,
        "next_validation_stage": "CLASSIFICATION",
        "next_validation_stage_label": "候选分类",
        "validation_path_note": "可进入研究验证链，不可进入 Paper / Live",
    }
    report = {
        **_sample_report(selection_count=0),
        "execution_status": "COMPLETED",
        "selection_stage": "FINALIZED",
        "result_quality": "DEGRADED",
        "research_admission": "RESEARCH_ONLY",
        "selection_outcome": "NO_TRADABLE_SELECTION",
        "completed_with_selection": False,
        "requested_top_n": 3,
        "selected_top_n": 0,
        "research_requested_top_n": 3,
        "research_selected_top_n": 1,
        "tradable_requested_top_n": 3,
        "tradable_selected_top_n": 0,
        "top3": [],
        "research_top_candidates": [candidate],
    }

    _, body = alerts._build_ai_selection_message(report, [])

    assert "研究候选：1/3" in body
    assert "可交易候选：0/3" in body
    assert "正式TOP：0/3" in body
    assert "TOP1：空槽" in body
    assert "研究候选1：SOFI" in body
    assert "下一验证阶段：候选分类（CLASSIFICATION）" in body
    assert "验证说明：可进入研究验证链，不可进入 Paper / Live" in body
    assert "Paper / Live" in body


def test_ai_selection_message_truncates_long_reason():
    _, body = alerts._build_ai_selection_message(
        _sample_report(),
        [
            _sample_top(1, "SOXS", reason="A" * 200),
            _sample_top(2, "SOFI"),
            _sample_top(3, "AAPL"),
        ],
    )

    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA…" in body
    assert "A" * 120 not in body


def test_notify_ai_selection_result_send_failure_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH", str(tmp_path / "notification_ledger.jsonl"))
    monkeypatch.setattr(
        alerts,
        "load_committed_selection_bundle",
        lambda *_args, **_kwargs: {"report": _committed_bundle_report(selection_run_id="bundle-run-failure", selection_bundle_hash="bundle-hash-failure")},
    )
    monkeypatch.setattr(alerts, "_load_notification_config", lambda: {"webhook_url": "https://example.com/hook"})

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self._telegram_enabled = False
            self.webhook_url = "https://example.com/hook"

        def _send(self, *args, **kwargs):
            raise RuntimeError("telegram down")

    monkeypatch.setattr(alerts, "Notifier", FakeNotifier)

    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])


def test_ai_selection_notification_prefers_ai_selector_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH", str(tmp_path / "notification_ledger.jsonl"))
    monkeypatch.setattr(
        alerts,
        "load_committed_selection_bundle",
        lambda *_args, **_kwargs: {"report": _committed_bundle_report(selection_run_id="bundle-run-prefers", selection_bundle_hash="bundle-hash-prefers")},
    )
    monkeypatch.setattr(
        alerts,
        "_load_ai_selector_notification_config",
        lambda: {
            "webhook_url": "https://example.com/trade-hook",
            "telegram_bot_token": "trade-bot",
            "telegram_chat_id": "trade-chat",
            "ai_selector_webhook_url": "https://example.com/ai-hook",
            "ai_selector_telegram_bot_token": "ai-bot",
            "ai_selector_telegram_chat_id": "ai-chat",
        },
    )

    captured = {}

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self._telegram_enabled = True
            self.webhook_url = kwargs.get("webhook_url")

        def _send(self, *args, **kwargs):
            pass

    monkeypatch.setattr(alerts, "Notifier", FakeNotifier)

    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])

    assert captured["telegram_bot_token"] == "ai-bot"
    assert captured["telegram_chat_id"] == "ai-chat"
    assert captured["webhook_url"] == "https://example.com/ai-hook"


def test_ai_selection_notification_skips_report_only_payload(monkeypatch):
    called = {"notifier": False}

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            called["notifier"] = True

        def _send(self, *args, **kwargs):
            raise AssertionError("report-only payload should not send")

    monkeypatch.setattr(alerts, "Notifier", FakeNotifier)
    monkeypatch.setattr(alerts, "load_committed_selection_bundle", lambda *_args, **_kwargs: None)

    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])

    assert called["notifier"] is False


def test_ai_selection_notification_sends_once_per_bundle(monkeypatch, tmp_path):
    ledger_path = tmp_path / "notification_ledger.jsonl"
    monkeypatch.setenv("SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH", str(ledger_path))
    monkeypatch.setattr(
        alerts,
        "load_committed_selection_bundle",
        lambda *_args, **_kwargs: {"report": _committed_bundle_report(selection_run_id="bundle-run-once", selection_bundle_hash="bundle-hash-once")},
    )
    monkeypatch.setattr(
        alerts,
        "_load_ai_selector_notification_config",
        lambda: {"ai_selector_telegram_bot_token": "ai-bot", "ai_selector_telegram_chat_id": "ai-chat"},
    )
    sent = []

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self._telegram_enabled = True
            self.webhook_url = ""

        def _send(self, *args, **kwargs):
            sent.append(args)

    monkeypatch.setattr(alerts, "Notifier", FakeNotifier)

    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])
    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])

    assert len(sent) == 1
    ledger_lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(ledger_lines) == 1
    assert "AI_SELECTION_FINALIZED" in ledger_lines[0]


def test_ai_selection_notification_failure_can_retry(monkeypatch, tmp_path):
    ledger_path = tmp_path / "notification_ledger.jsonl"
    monkeypatch.setenv("SOXS_AI_SELECTION_NOTIFICATION_LEDGER_PATH", str(ledger_path))
    monkeypatch.setattr(
        alerts,
        "load_committed_selection_bundle",
        lambda *_args, **_kwargs: {"report": _committed_bundle_report(selection_run_id="bundle-run-retry", selection_bundle_hash="bundle-hash-retry")},
    )
    monkeypatch.setattr(
        alerts,
        "_load_ai_selector_notification_config",
        lambda: {"ai_selector_telegram_bot_token": "ai-bot", "ai_selector_telegram_chat_id": "ai-chat"},
    )
    attempts = {"count": 0}

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self._telegram_enabled = True
            self.webhook_url = ""

        def _send(self, *args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("telegram down")

    monkeypatch.setattr(alerts, "Notifier", FakeNotifier)

    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])
    alerts.notify_ai_selection_result(_sample_report(), [_sample_top(1, "SOXS")])

    assert attempts["count"] == 2
    ledger_text = ledger_path.read_text(encoding="utf-8")
    assert '"status": "FAILED"' in ledger_text
    assert '"status": "SENT"' in ledger_text
