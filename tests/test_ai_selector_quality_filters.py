from __future__ import annotations

import json
import os as _os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.openalpha import selector as selector_module
from src.openalpha.selector import AIStrategySelector, apply_quality_filters


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = []

    def setattr(self, obj, name, value):
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, original in reversed(self._originals):
            setattr(obj, name, original)


@contextmanager
def _with_sample_universe():
    """Force OPENALPHA_UNIVERSE=sample so mocked _load_local_snapshot takes effect."""
    prev = _os.environ.get("OPENALPHA_UNIVERSE")
    _os.environ["OPENALPHA_UNIVERSE"] = "sample"
    try:
        yield
    finally:
        if prev is None:
            _os.environ.pop("OPENALPHA_UNIVERSE", None)
        else:
            _os.environ["OPENALPHA_UNIVERSE"] = prev


def _candidate(ticker: str, score: float, existing_position: bool = False) -> dict:
    return {
        "ticker": ticker,
        "sector": "Technology",
        "score": score,
        "base_score": score,
        "volatility_score": 75.0,
        "volume_score": 72.0,
        "trend_fit_score": 68.0,
        "repeatability_score": 66.0,
        "drawdown_safety_score": 64.0,
        "correlation_penalty": 0.0,
        "range_low": 20.0,
        "range_high": 24.0,
        "suggested_range": "$20.00 - $24.00",
        "risk": {"stop_loss_pct": 1.5},
        "series": {"returns": [0.01] * 30},
        "existing_position": existing_position,
    }


def test_apply_quality_filters_removes_volume_spread_and_volatility_failures():
    monkeypatch = SimpleMonkeyPatch()
    try:
        class FakeContext:
            def history_metrics(self, symbol):
                return {
                    "PASS": (1_000_000, 0.0, 20.0),
                    "LOWVOL": (100_000, 0.0, 20.0),
                    "WIDESPREAD": (1_000_000, 0.0, 20.0),
                    "MOMO": (1_000_000, 25.0, 20.0),
                    "SOFI": (100_000, None, 18.0),
                }[symbol]

            def quote_metrics(self, symbol):
                return {
                    "PASS": (20.0, 19.98, 20.02, True),
                    "LOWVOL": (20.0, 19.98, 20.02, True),
                    "WIDESPREAD": (20.0, 19.0, 20.2, True),
                    "MOMO": (20.0, 19.98, 20.02, True),
                    "SOFI": (18.0, None, None, False),
                }[symbol]

        monkeypatch.setattr(selector_module, "_QualityFilterContext", FakeContext)
        candidates = [
            _candidate("PASS", 80.0),
            _candidate("LOWVOL", 90.0),
            _candidate("WIDESPREAD", 88.0),
            _candidate("MOMO", 87.0),
            _candidate("SOFI", 0.0, existing_position=True),
        ]

        filtered = apply_quality_filters(candidates)
        tickers = [item["ticker"] for item in filtered]

        assert tickers == ["PASS", "SOFI"]
        assert filtered[1]["existing_position"] is True
    finally:
        monkeypatch.restore()


def test_apply_quality_filters_blocks_unconfirmed_spread():
    monkeypatch = SimpleMonkeyPatch()
    try:
        class FakeContext:
            def history_metrics(self, symbol):
                return (1_000_000, 0.0, 20.0)

            def quote_metrics(self, symbol):
                return (20.0, 20.0, 20.0, False)

        monkeypatch.setattr(selector_module, "_QualityFilterContext", FakeContext)

        filtered = apply_quality_filters([_candidate("PASS", 80.0)])

        assert filtered == []
    finally:
        monkeypatch.restore()


def test_apply_quality_filters_accepts_fallback_candidate_with_quote_and_volume_hint():
    monkeypatch = SimpleMonkeyPatch()
    try:
        class FakeContext:
            def history_metrics(self, symbol):
                return (None, None, None)

            def quote_metrics(self, symbol):
                return (20.0, 19.98, 20.02, True)

        monkeypatch.setattr(selector_module, "_QualityFilterContext", FakeContext)
        candidate = _candidate("PASS", 80.0)
        candidate["data_source"] = "fallback"
        candidate["avg_daily_volume_hint"] = 2_000_000
        candidate["price_midpoint_hint"] = 20.0

        filtered = apply_quality_filters([candidate])

        assert [item["ticker"] for item in filtered] == ["PASS"]
        assert filtered[0]["avg_10d_volume"] == 2000000.0
    finally:
        monkeypatch.restore()


def test_apply_quality_filters_allows_liquid_special_etf_with_unconfirmed_quote_spread():
    monkeypatch = SimpleMonkeyPatch()
    try:
        class FakeContext:
            def history_metrics(self, symbol):
                return (12_000_000, -22.0, 20.0)

            def quote_metrics(self, symbol):
                return (20.0, 19.98, 20.02, False)

        monkeypatch.setattr(selector_module, "_QualityFilterContext", FakeContext)

        filtered = apply_quality_filters([_candidate("SOXL", 80.0)])

        assert [item["ticker"] for item in filtered] == ["SOXL"]
        assert filtered[0]["spread_pct_live"] == 0.2
    finally:
        monkeypatch.restore()


def test_selector_respects_quality_filter_output_without_backfill():
    """After removing the backfill loop, FORMAL_TOP only includes quality-passed candidates.
    AAA has low volume (100K avg) → rejected by volume filter.
    BBB has good volume (2M avg) → passes quality. Only BBB appears in topk."""
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            class FakeContext:
                def history_metrics(self, symbol):
                    return {
                        "AAA": (100_000, 0.0, 20.0),
                        "BBB": (2_000_000, 0.0, 25.0),
                    }[symbol]

                def quote_metrics(self, symbol):
                    return {
                        "AAA": (20.0, 19.99, 20.01, True),
                        "BBB": (25.0, 24.99, 25.01, True),
                    }[symbol]

            monkeypatch.setattr(selector_module, "_QualityFilterContext", FakeContext)
            monkeypatch.setattr(selector_module, "LOG_DIR", log_dir)

            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.universe._load_local_snapshot = lambda: ["AAA", "BBB"]
                selector.news.collect_for_symbols = lambda symbols: {symbol: [] for symbol in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAA", 95.0),
                    _candidate("BBB", 85.0),
                ]

                result = selector.run_selection(write_configs=False)

                # Only BBB passed quality; AAA was rejected by volume filter — NOT backfilled.
                assert [item["ticker"] for item in result["top5"]] == ["BBB"]
            assert not any(item.get("quality_backfill") for item in result["top5"])
            # Funnel invariant: FORMAL_TOP output (1) ≤ DATA_QUALITY output (1)
            funnel = result["selection_funnel"]
            formal_top = [s for s in funnel["stages"] if s["stage"] == "FORMAL_TOP"][0]
            data_quality = [s for s in funnel["stages"] if s["stage"] == "DATA_QUALITY"][0]
            assert formal_top["output_count"] <= data_quality["output_count"]

            log_path = log_dir / f"selection_{selector_module.datetime.now().strftime('%Y-%m-%d')}.log"
            assert log_path.exists()
            lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert lines[0]["summary"]["removed_by_volume_filter"] == 1
            assert lines[0]["summary"]["final_selected_symbols"] == ["BBB"]
            assert lines[0]["summary"]["backfilled_symbols"] == []  # No backfill
    finally:
        monkeypatch.restore()


def test_selector_never_pads_quality_output_to_reach_selection_size():
    """When only 1 out of 3 candidates passes quality, topk must be 1 — not padded to 3.
    This is the regression test for the FORMAL_TOP 2→3 injection bug."""
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"

            class FakeContext:
                def history_metrics(self, symbol):
                    return {
                        "AAA": (2_000_000, 0.0, 20.0),
                        "BBB": (100_000, 0.0, 20.0),
                        "CCC": (100_000, 0.0, 20.0),
                    }[symbol]

                def quote_metrics(self, symbol):
                    return (20.0, 19.99, 20.01, True)

            monkeypatch.setattr(selector_module, "_QualityFilterContext", FakeContext)
            monkeypatch.setattr(selector_module, "LOG_DIR", log_dir)

            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 3
                selector.universe._load_local_snapshot = lambda: ["AAA", "BBB", "CCC"]
                selector.news.collect_for_symbols = lambda symbols: {symbol: [] for symbol in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAA", 95.0),
                    _candidate("BBB", 90.0),
                    _candidate("CCC", 85.0),
                ]

                result = selector.run_selection(write_configs=False)

                # Only AAA passed quality (2M vol). BBB and CCC rejected (100K vol).
                # No backfill — topk must be exactly 1.
                assert [item["ticker"] for item in result["top5"]] == ["AAA"]
                assert not any(item.get("quality_backfill") for item in result["top5"])
                assert result["quality_filter_report"]["backfilled_symbols"] == []

                # Funnel invariant: FORMAL_TOP output (1) ≤ DATA_QUALITY output (1)
                funnel = result["selection_funnel"]
                formal_top = [s for s in funnel["stages"] if s["stage"] == "FORMAL_TOP"][0]
                data_quality = [s for s in funnel["stages"] if s["stage"] == "DATA_QUALITY"][0]
                assert formal_top["output_count"] <= data_quality["output_count"]
                assert formal_top["output_count"] == 1
                assert data_quality["output_count"] == 1
    finally:
        monkeypatch.restore()


def test_selector_returns_fast_preliminary_when_quality_stage_times_out():
    monkeypatch = SimpleMonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"

            monkeypatch.setattr(selector_module, "LOG_DIR", log_dir)
            # Force FULL mode so quality fallback is triggered (not EOD relaxed)
            try:
                from src.openalpha import preflight as pf_module
                monkeypatch.setattr(pf_module, "run_preflight",
                    lambda dry_run=True: type("_FP", (), {
                        "to_dict": lambda self: {"run_mode": "FULL", "market_state": "MARKET_OPEN", "is_trading_day": True},
                    })())
            except Exception:
                pass
            monkeypatch.setattr(
                selector_module,
                "_apply_quality_filters_with_report",
                lambda candidates, max_seconds=None, run_mode="FULL": (
                    [],
                    {
                        "generated_at": "2026-07-05T09:00:00",
                        "total_candidates_before_filters": len(candidates),
                        "removed_by_volume_filter": 0,
                        "removed_by_spread_filter": 0,
                        "removed_by_volatility_filter": 0,
                        "removed_due_to_missing_data": 0,
                        "final_selected_symbols": [],
                        "existing_real_positions_preserved": [],
                        "timed_out": True,
                        "rows": [],
                    },
                ),
            )

            with _with_sample_universe():
                selector = AIStrategySelector()
                selector.selection_size = 3
                selector.universe._load_local_snapshot = lambda: ["AAA", "BBB", "CCC"]
                selector.news.collect_for_symbols = lambda symbols: {symbol: [] for symbol in symbols}
                selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                    _candidate("AAA", 95.0),
                    _candidate("BBB", 90.0),
                    _candidate("CCC", 85.0),
                ]

                result = selector.run_selection(write_configs=False)

                assert [item["ticker"] for item in result["top5"]] == ["AAA", "BBB", "CCC"]
                assert result["settings"]["selection_stage"] == "fast_preliminary"
    finally:
        monkeypatch.restore()


def run_test_direct():
    test_apply_quality_filters_removes_volume_spread_and_volatility_failures()
    test_apply_quality_filters_blocks_unconfirmed_spread()
    test_apply_quality_filters_accepts_fallback_candidate_with_quote_and_volume_hint()
    test_apply_quality_filters_allows_liquid_special_etf_with_unconfirmed_quote_spread()
    test_selector_respects_quality_filter_output_without_backfill()
    test_selector_never_pads_quality_output_to_reach_selection_size()
    test_selector_returns_fast_preliminary_when_quality_stage_times_out()


if __name__ == "__main__":
    run_test_direct()
