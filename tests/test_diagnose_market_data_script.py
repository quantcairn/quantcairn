from __future__ import annotations

import importlib.util
from pathlib import Path

from src.openalpha.data_diagnostics import MarketDataAudit


def _load_script_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_market_data.py"
    spec = importlib.util.spec_from_file_location("diagnose_market_data_script", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_script_prints_read_only_table(monkeypatch, capsys):
    module = _load_script_module()

    def _fake_diagnose(symbols, **_kwargs):
        return [
            MarketDataAudit(
                symbol=symbols[0],
                provider_attempts=(),
                provider_used="YAHOO_CHART",
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
            ),
            MarketDataAudit(
                symbol=symbols[1],
                provider_attempts=(),
                provider_used="YAHOO_CHART",
                cache_status="COMPLETE",
                quote_status="MISSING",
                ohlcv_status="MISSING",
                history_status="MISSING",
                benchmark_status="INVALID",
                first_failure_node="quote",
                normalized_failure_reason="quote_missing",
                retry_count=0,
                formal_data_ready=False,
                record_completeness="COMPLETE",
                market_data_sufficiency="FAILED",
                research_evidence_status="FAILED",
                formal_scoring_eligibility=False,
                data_status="INVALID",
                freshness_status="STALE",
                quote_fetch_status="MISSING",
                ohlcv_fetch_status="MISSING",
                benchmark_alignment_status="INVALID",
            ),
        ]

    monkeypatch.setattr(module, "diagnose_market_data", _fake_diagnose)
    exit_code = module.main(["--symbols", "CRM", "DIS", "--no-trade"])
    captured = capsys.readouterr().out
    assert exit_code == 1
    assert "Symbol" in captured
    assert "Formal Ready" in captured
    assert "CRM" in captured
    assert "DIS" in captured
