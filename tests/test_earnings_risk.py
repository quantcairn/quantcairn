from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.openalpha.earnings_provider import (
    CandidateEarningsProvider,
    EarningsInfo,
    EarningsRiskLevel,
    calculate_trading_days_to_earnings,
    get_default_earnings_provider,
    normalize_earnings_info,
)
from src.openalpha.selection_bundle import build_selection_bundle, load_committed_selection_bundle, persist_selection_bundle
from src.openalpha.selector import AIStrategySelector
import src.openalpha.selector as selector_module
from src.utils.market_calendar import next_us_market_trading_day


def _earnings_payload(*, symbol: str = "NVDA", earnings_date: str, earnings_time: str = "AMC", market_timezone: str = "America/New_York", source: str = "unit_test", confidence: float = 0.9) -> dict[str, object]:
    return {
        "symbol": symbol,
        "earnings_date": earnings_date,
        "earnings_time": earnings_time,
        "market_timezone": market_timezone,
        "source": source,
        "updated_at": "2026-08-01T12:00:00Z",
        "confidence": confidence,
    }


def test_earnings_risk_level_mapping_uses_trading_days():
    as_of = datetime(2026, 8, 3, 16, 0, tzinfo=timezone.utc)
    same_day = normalize_earnings_info(_earnings_payload(earnings_date=as_of.date().isoformat()), as_of=as_of)
    assert same_day is not None
    assert same_day.trading_days_to_earnings == 0
    assert same_day.earnings_risk_level == EarningsRiskLevel.VERY_HIGH

    next_day = next_us_market_trading_day(as_of)
    one_day = normalize_earnings_info(_earnings_payload(earnings_date=next_day.isoformat()), as_of=as_of)
    assert one_day is not None
    assert one_day.trading_days_to_earnings == 1
    assert one_day.earnings_risk_level == EarningsRiskLevel.VERY_HIGH

    second_day = next_us_market_trading_day(next_day)
    two_days = normalize_earnings_info(_earnings_payload(earnings_date=second_day.isoformat()), as_of=as_of)
    assert two_days is not None
    assert two_days.trading_days_to_earnings == 2
    assert two_days.earnings_risk_level == EarningsRiskLevel.HIGH

    med_target = second_day
    for _ in range(3):
        med_target = next_us_market_trading_day(med_target)
    medium = normalize_earnings_info(_earnings_payload(earnings_date=med_target.isoformat()), as_of=as_of)
    assert medium is not None
    assert medium.trading_days_to_earnings == 5
    assert medium.earnings_risk_level == EarningsRiskLevel.MEDIUM

    low_target = med_target
    for _ in range(4):
        low_target = next_us_market_trading_day(low_target)
    low = normalize_earnings_info(_earnings_payload(earnings_date=low_target.isoformat()), as_of=as_of)
    assert low is not None
    assert low.trading_days_to_earnings == 9
    assert low.earnings_risk_level == EarningsRiskLevel.LOW


def test_earnings_trading_day_calculation_handles_timezone_and_missing_data():
    as_of = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    target = next_us_market_trading_day(date(2026, 8, 3))
    assert calculate_trading_days_to_earnings(target, as_of=as_of, market_timezone="America/New_York") == 1
    assert calculate_trading_days_to_earnings("not-a-date", as_of=as_of) is None
    assert normalize_earnings_info({"symbol": "NVDA", "earnings_date": "not-a-date"}, as_of=as_of) is None
    assert normalize_earnings_info({"symbol": "NVDA"}, as_of=as_of) is None


def test_earnings_provider_normalizes_candidate_payload_and_handles_invalid_response():
    provider = CandidateEarningsProvider()
    info = provider.get_earnings_info(
        {
            "ticker": "NVDA",
            "earnings_info": {
                "symbol": "NVDA",
                "earnings_date": "2026-08-04",
                "earnings_time": "AMC",
                "market_timezone": "America/New_York",
                "source": "provider-feed",
                "confidence": 0.88,
            },
        },
        as_of=date(2026, 8, 3),
    )
    assert isinstance(info, EarningsInfo)
    assert info.symbol == "NVDA"
    assert info.earnings_risk_level in {EarningsRiskLevel.VERY_HIGH, EarningsRiskLevel.HIGH}
    assert info.to_dict()["earnings_risk_level"] == info.earnings_risk_level.value
    assert provider.get_earnings_info({"ticker": "NVDA", "earnings_info": {"symbol": "NVDA"}}, as_of=date(2026, 8, 3)) is None
    assert get_default_earnings_provider().get_earnings_info({"ticker": "NVDA"}, as_of=date(2026, 8, 3)) is None


def test_selector_earnings_metadata_does_not_change_selection_output(tmp_path, monkeypatch):
    monkeypatch.setattr(selector_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(selector_module, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("OPENALPHA_TOP_K", "2")
    monkeypatch.setenv("OPENALPHA_MAX_SYMBOLS", "2")

    class FakePreflight:
        def to_dict(self) -> dict[str, object]:
            return {
                "market_state": "REGULAR",
                "run_mode": "RESEARCH",
                "data_mode": "EOD_ONLY",
                "selection_run_id": "run-earnings",
            }

    def _fake_preflight(*args, **kwargs):
        return FakePreflight()

    def _fake_quality_filters(candidates, **kwargs):
        rows = [dict(item) for item in candidates]
        report = {
            "rows": [],
            "timed_out": False,
            "final_selected_symbols": [str(item.get("ticker") or "").upper() for item in rows],
            "existing_real_positions_preserved": [],
        }
        return rows, report

    def _fake_score_universe(self, symbols, news_map):
        rows = []
        scores = {
            "NVDA": 96.0,
            "MSFT": 91.0,
        }
        for ticker in symbols:
            score = scores.get(ticker, 80.0)
            rows.append(
                {
                    "ticker": ticker,
                    "score": score,
                    "final_score": score,
                    "candidate_score": score,
                    "ai_score": score,
                    "volatility_score": 70.0 if ticker == "NVDA" else 66.0,
                    "volume_score": 80.0 if ticker == "NVDA" else 78.0,
                    "trend_fit_score": 75.0 if ticker == "NVDA" else 73.0,
                    "repeatability_score": 65.0 if ticker == "NVDA" else 64.0,
                    "drawdown_safety_score": 60.0 if ticker == "NVDA" else 59.0,
                    "sector": "Technology" if ticker == "NVDA" else "Software",
                    "series": {"returns": [0.01, 0.02, 0.015, 0.013, 0.018, 0.02]},
                    "data_status": "COMPLETE",
                    "scoring_eligible": True,
                    "formal_scoring_eligibility": True,
                    "current_validation_status": "DATA_VALID",
                    "validation_status": "DATA_VALID",
                    "trade_admission_status": "TRADABLE",
                    "trade_admission": "TRADABLE",
                    "quote_status": "COMPLETE",
                    "ohlcv_status": "COMPLETE",
                    "history_status": "COMPLETE",
                    "benchmark_status": "VALID",
                    "benchmark_alignment_status": "VALID",
                    "freshness_status": "SAFE",
                }
            )
        return rows

    monkeypatch.setattr(selector_module, "_apply_quality_filters_with_report", _fake_quality_filters)
    monkeypatch.setattr(selector_module, "score_candidate", lambda item: dict(item))
    monkeypatch.setattr("src.openalpha.data_diagnostics.check_data_availability", lambda symbols: (list(symbols), []))
    monkeypatch.setattr("src.openalpha.preflight.run_preflight", _fake_preflight)

    class StaticEarningsProvider:
        def __init__(self):
            self.calls: list[str] = []

        def get_earnings_info(self, candidate, *, as_of=None):
            ticker = str(candidate.get("ticker") or "").upper()
            self.calls.append(ticker)
            if ticker != "NVDA":
                return None
            return normalize_earnings_info(
                {
                    "symbol": ticker,
                    "earnings_date": "2026-08-04",
                    "earnings_time": "AMC",
                    "market_timezone": "America/New_York",
                    "source": "static_provider",
                    "confidence": 0.77,
                },
                as_of=date(2026, 8, 3),
            )

    base_selector = AIStrategySelector(earnings_provider=StaticEarningsProvider())
    base_selector.news = None
    with_provider = base_selector.run_selection(
        write_configs=False,
        symbols_override=["NVDA", "MSFT"],
        selection_run_id="run-earnings",
    )

    plain_selector = AIStrategySelector()
    plain_selector.news = None
    plain_selector.earnings_provider = type(
        "_NullProvider",
        (),
        {"get_earnings_info": lambda self, candidate, *, as_of=None: None},
    )()
    without_provider = plain_selector.run_selection(
        write_configs=False,
        symbols_override=["NVDA", "MSFT"],
        selection_run_id="run-earnings",
    )

    def _strip(rows):
        return [
            {
                **{
                    key: value
                    for key, value in dict(item).items()
                    if key not in {
                        "earnings_info",
                        "earnings_risk_level",
                        "trading_days_to_earnings",
                        "earnings_date",
                        "earnings_time",
                        "earnings_market_timezone",
                        "earnings_source",
                        "earnings_updated_at",
                        "earnings_confidence",
                    }
                },
            }
            for item in rows
        ]

    assert [item["ticker"] for item in with_provider["top3"]] == [item["ticker"] for item in without_provider["top3"]]
    assert [item["ticker"] for item in with_provider["top5"]] == [item["ticker"] for item in without_provider["top5"]]
    assert _strip(with_provider["top3"]) == _strip(without_provider["top3"])
    assert _strip(with_provider["top5"]) == _strip(without_provider["top5"])
    nvda_row = next(item for item in with_provider["top5"] if item["ticker"] == "NVDA")
    assert nvda_row["earnings_info"]["symbol"] == "NVDA"
    assert nvda_row["earnings_info"]["earnings_risk_level"] in {"VERY_HIGH", "HIGH"}


def test_selector_earnings_provider_failure_falls_back_without_changing_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(selector_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(selector_module, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("OPENALPHA_TOP_K", "1")
    monkeypatch.setenv("OPENALPHA_MAX_SYMBOLS", "1")
    monkeypatch.setattr(selector_module, "_apply_quality_filters_with_report", lambda candidates, **kwargs: (list(candidates), {"rows": [], "timed_out": False, "final_selected_symbols": [str(item.get("ticker") or "").upper() for item in candidates], "existing_real_positions_preserved": []}))
    monkeypatch.setattr(selector_module, "score_candidate", lambda item: dict(item))
    monkeypatch.setattr("src.openalpha.data_diagnostics.check_data_availability", lambda symbols: (list(symbols), []))
    monkeypatch.setattr("src.openalpha.preflight.run_preflight", lambda *args, **kwargs: type("PF", (), {"to_dict": lambda self: {"market_state": "REGULAR", "run_mode": "RESEARCH", "data_mode": "EOD_ONLY", "selection_run_id": "run-broken"}})())
    monkeypatch.setattr(
        selector_module.AIStrategySelector,
        "_score_with_live_flag",
        lambda self, symbols, news_map, live_enabled: [
            {
                "ticker": "NVDA",
                "score": 92.0,
                "final_score": 92.0,
                "candidate_score": 92.0,
                "ai_score": 92.0,
                "volatility_score": 70.0,
                "volume_score": 80.0,
                "trend_fit_score": 75.0,
                "repeatability_score": 65.0,
                "drawdown_safety_score": 60.0,
                "sector": "Technology",
                "series": {"returns": [0.01, 0.02, 0.015, 0.013, 0.018, 0.02]},
                "data_status": "COMPLETE",
                "scoring_eligible": True,
                "formal_scoring_eligibility": True,
                "current_validation_status": "DATA_VALID",
                "validation_status": "DATA_VALID",
                "trade_admission_status": "TRADABLE",
                "trade_admission": "TRADABLE",
                "quote_status": "COMPLETE",
                "ohlcv_status": "COMPLETE",
                "history_status": "COMPLETE",
                "benchmark_status": "VALID",
                "benchmark_alignment_status": "VALID",
                "freshness_status": "SAFE",
            }
        ],
    )

    class ExplodingProvider:
        def get_earnings_info(self, candidate, *, as_of=None):
            raise RuntimeError("boom")

    selector = AIStrategySelector(earnings_provider=ExplodingProvider())
    selector.news = None
    result = selector.run_selection(write_configs=False, symbols_override=["NVDA"], selection_run_id="run-broken")

    assert [item["ticker"] for item in result["top3"]] == ["NVDA"]
    assert "earnings_info" not in result["top3"][0] or result["top3"][0].get("earnings_info") in ({}, None)
