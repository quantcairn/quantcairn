from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.openalpha.data_diagnostics import MarketDataAudit, build_market_data_audit, diagnose_market_data


def _base_snapshot(**overrides):
    snapshot = {
        "symbol": "SOFI",
        "cache_status": "COMPLETE",
        "cache_error_message": None,
        "quote_status": "COMPLETE",
        "quote_fetch_status": "COMPLETE",
        "quote_fetch_error_code": None,
        "quote_fetch_error_message": None,
        "quote_provider_used": "YAHOO_CHART",
        "quote_retry_count": 0,
        "ohlcv_status": "COMPLETE",
        "ohlcv_fetch_status": "COMPLETE",
        "ohlcv_fetch_error_code": None,
        "ohlcv_fetch_error_message": None,
        "ohlcv_provider_used": "YAHOO_CHART_HISTORY",
        "ohlcv_retry_count": 0,
        "history_status": "COMPLETE",
        "history_provider_used": "YAHOO_CHART_HISTORY",
        "history_retry_count": 0,
        "benchmark_status": "VALID",
        "benchmark_alignment_status": "VALID",
        "benchmark_symbols": ["SPY.US"],
        "benchmark_quote_fetch_status": {"SPY.US": "COMPLETE"},
        "benchmark_quote_error_code": {"SPY.US": None},
        "benchmark_quote_error_message": {"SPY.US": None},
        "benchmark_quote_provider_used": {"SPY.US": "YAHOO_CHART"},
        "benchmark_quote_retry_count": {"SPY.US": 0},
        "benchmark_ohlcv_fetch_status": {"SPY.US": "COMPLETE"},
        "benchmark_ohlcv_error_code": {"SPY.US": None},
        "benchmark_ohlcv_error_message": {"SPY.US": None},
        "benchmark_ohlcv_provider_used": {"SPY.US": "YAHOO_CHART_HISTORY"},
        "benchmark_ohlcv_retry_count": {"SPY.US": 0},
        "freshness_status": "SAFE",
        "data_status": "COMPLETE",
    }
    snapshot.update(overrides)
    return snapshot


def _base_quality(**overrides):
    quality = {
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "blocking_reasons": [],
        "record_completeness": "COMPLETE",
        "market_data_sufficiency": "COMPLETE",
        "research_evidence_status": "FAILED",
        "formal_scoring_eligibility": True,
    }
    quality.update(overrides)
    return quality


@pytest.mark.parametrize(
    "name,snapshot,quality,expected",
    [
        (
            "provider_success",
            _base_snapshot(),
            _base_quality(),
            {
                "cache_status": "COMPLETE",
                "provider_used": "YAHOO_CHART",
                "first_failure_node": "",
                "normalized_failure_reason": "unknown",
                "formal_data_ready": True,
            },
        ),
        (
            "research_evidence_not_run",
            _base_snapshot(),
            _base_quality(scoring_eligible=False, formal_scoring_eligibility=False),
            {
                "provider_used": "YAHOO_CHART",
                "first_failure_node": "validation",
                "normalized_failure_reason": "research_evidence_not_run",
                "formal_data_ready": False,
            },
        ),
        (
            "sentinel_provider_falls_back",
            _base_snapshot(
                quote_provider_used="UNAVAILABLE",
                ohlcv_provider_used="UNAVAILABLE",
                history_provider_used="YAHOO_CHART_HISTORY",
            ),
            _base_quality(),
            {
                "provider_used": "YAHOO_CHART_HISTORY",
                "formal_data_ready": True,
            },
        ),
        (
            "cache_failure_provider_success",
            _base_snapshot(cache_status="CACHE_ERROR", cache_error_message="unable to open database file"),
            _base_quality(),
            {
                "cache_status": "CACHE_ERROR",
                "first_failure_node": "cache",
                "normalized_failure_reason": "cache_error",
                "formal_data_ready": True,
            },
        ),
        (
            "quote_failure",
            _base_snapshot(
                quote_status="MISSING",
                quote_fetch_status="PROVIDER_ERROR",
                quote_fetch_error_code="YAHOO_UNAUTHORIZED",
                quote_fetch_error_message="Yahoo 401 Invalid Crumb",
                data_status="INVALID",
            ),
            _base_quality(data_status="INVALID", scoring_eligible=False, formal_scoring_eligibility=False, blocking_reasons=["quote_missing"], market_data_sufficiency="FAILED"),
            {
                "quote_status": "MISSING",
                "first_failure_node": "quote",
                "normalized_failure_reason": "auth_failed",
                "formal_data_ready": False,
            },
        ),
        (
            "ohlcv_failure",
            _base_snapshot(
                ohlcv_status="MISSING",
                ohlcv_fetch_status="EMPTY_RESPONSE",
                ohlcv_fetch_error_code="NO_HISTORY",
                ohlcv_fetch_error_message="no data returned",
                history_status="MISSING",
                data_status="INVALID",
            ),
            _base_quality(data_status="INVALID", scoring_eligible=False, formal_scoring_eligibility=False, blocking_reasons=["ohlcv_missing"], market_data_sufficiency="FAILED"),
            {
                "ohlcv_status": "MISSING",
                "first_failure_node": "ohlcv",
                "normalized_failure_reason": "ohlcv_missing",
                "formal_data_ready": False,
            },
        ),
        (
            "benchmark_failure",
            _base_snapshot(
                benchmark_status="INVALID",
                benchmark_alignment_status="INVALID",
                benchmark_quote_fetch_status={"SPY.US": "COMPLETE"},
                benchmark_ohlcv_fetch_status={"SPY.US": "INVALID"},
                benchmark_ohlcv_error_code={"SPY.US": "BENCHMARK_ALIGNMENT_FAILED"},
            ),
            _base_quality(data_status="INVALID", scoring_eligible=False, formal_scoring_eligibility=False, blocking_reasons=["benchmark_invalid"], market_data_sufficiency="FAILED"),
            {
                "benchmark_status": "INVALID",
                "first_failure_node": "benchmark_ohlcv:SPY.US",
                "normalized_failure_reason": "benchmark_invalid",
                "formal_data_ready": False,
            },
        ),
        (
            "invalid_payload",
            _base_snapshot(
                quote_status="MISSING",
                quote_fetch_status="MALFORMED_RESPONSE",
                quote_fetch_error_code="NON_DICT_JSON",
                quote_fetch_error_message="chart payload type=list",
                data_status="INVALID",
            ),
            _base_quality(data_status="INVALID", scoring_eligible=False, formal_scoring_eligibility=False, blocking_reasons=["quote_missing"], market_data_sufficiency="FAILED"),
            {
                "quote_status": "MISSING",
                "first_failure_node": "quote",
                "normalized_failure_reason": "invalid_payload",
                "formal_data_ready": False,
            },
        ),
        (
            "fallback_success",
            _base_snapshot(
                quote_provider_used="YFINANCE_HISTORY_5D_1M",
                quote_retry_count=2,
                ohlcv_provider_used="YFINANCE_HISTORY",
                history_provider_used="YFINANCE_HISTORY",
            ),
            _base_quality(),
            {
                "provider_used": "YFINANCE_HISTORY_5D_1M",
                "formal_data_ready": True,
            },
        ),
    ],
)
def test_build_market_data_audit_cases(name, snapshot, quality, expected):
    audit = build_market_data_audit("SOFI", snapshot=snapshot, data_quality=quality)
    assert audit.symbol == "SOFI"
    assert audit.cache_status == expected.get("cache_status", audit.cache_status)
    assert audit.quote_status == expected.get("quote_status", audit.quote_status)
    assert audit.ohlcv_status == expected.get("ohlcv_status", audit.ohlcv_status)
    assert audit.history_status == expected.get("history_status", audit.history_status)
    assert audit.benchmark_status == expected.get("benchmark_status", audit.benchmark_status)
    assert audit.provider_used == expected.get("provider_used", audit.provider_used)
    assert audit.first_failure_node == expected.get("first_failure_node", audit.first_failure_node)
    assert audit.normalized_failure_reason == expected.get("normalized_failure_reason", audit.normalized_failure_reason)
    assert audit.formal_data_ready is expected.get("formal_data_ready", audit.formal_data_ready)


def test_market_data_audit_to_dict_includes_attempts():
    audit = build_market_data_audit("SOFI", snapshot=_base_snapshot(), data_quality=_base_quality())
    payload = audit.to_dict()
    assert payload["symbol"] == "SOFI"
    assert payload["provider_attempts"]
    assert payload["formal_data_ready"] is True


def test_diagnose_market_data_is_read_only(monkeypatch):
    called = []

    def _fake_build(symbol, **kwargs):
        called.append(symbol)
        return MarketDataAudit(
            symbol=symbol,
            provider_attempts=(),
            provider_used="YAHOO",
            cache_status="COMPLETE",
            quote_status="COMPLETE",
            ohlcv_status="COMPLETE",
            history_status="COMPLETE",
            benchmark_status="VALID",
            first_failure_node="",
            normalized_failure_reason="unknown",
            retry_count=0,
            formal_data_ready=True,
            record_completeness="COMPLETE",
            market_data_sufficiency="COMPLETE",
            research_evidence_status="FAILED",
            formal_scoring_eligibility=True,
            data_status="COMPLETE",
            freshness_status="SAFE",
            quote_fetch_status="COMPLETE",
            ohlcv_fetch_status="COMPLETE",
            benchmark_alignment_status="VALID",
        )

    monkeypatch.setattr("src.openalpha.data_diagnostics.build_market_data_audit", _fake_build)
    audits = diagnose_market_data(["CRM", "DIS"])
    assert called == ["CRM", "DIS"]
    assert [audit.symbol for audit in audits] == ["CRM", "DIS"]
    assert all(audit.formal_data_ready for audit in audits)
