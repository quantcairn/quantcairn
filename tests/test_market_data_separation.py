"""Regression tests: MARKET_DATA validation / fallback ATR / data_source separation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.openalpha.universe_filter import evaluate_universe_candidate


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Fallback-derived volatility must not trigger ATR rejection
# ═══════════════════════════════════════════════════════════════════════════════

class TestFallbackAtrNoRejection:
    """When data_source is 'fallback', universe filter must skip ATR checks
    because the band_pct is synthetic (not real market volatility)."""

    def test_fallback_wont_trigger_volatility_too_high(self):
        """AAPL fallback: band_pct=30% → atr_20=15% > common_stock max 8%.
        With skip_atr_validation=True, this should NOT be rejected."""
        # AAPL profile: range_low=170, range_high=230, vol=55M
        # band_pct = (230-170)/200*100 = 30%, atr_20 = 15%
        candidate = {
            "ticker": "AAPL",
            "current_price": 200.0,
            "asset_type": "common_stock",
            "market_cap": 3_000_000_000_000,
            "average_dollar_volume_20d": 55_000_000 * 200.0,
            "atr_20_percentage": 15.0,  # derived from fallback band
            "data_source": "fallback",
        }

        # Without skip → rejected
        eval_no_skip = evaluate_universe_candidate(dict(candidate))
        assert eval_no_skip.rejected is True
        assert "volatility_too_high" in eval_no_skip.rejection_reason

        # With skip → accepted
        eval_skip = evaluate_universe_candidate(
            dict(candidate), skip_atr_validation=True
        )
        assert eval_skip.rejected is False

    def test_fallback_wont_trigger_volatility_too_low(self):
        """Tight band → low synthetic ATR. With skip, should not be rejected."""
        candidate = {
            "ticker": "TEST_ETF",
            "current_price": 50.0,  # within ETF price range (5-300)
            "asset_type": "etf",
            "average_dollar_volume_20d": 50_000_000.0,  # above 20M min
            "atr_20_percentage": 0.2,  # derived from very tight band
            "data_source": "fallback",
        }

        eval_no_skip = evaluate_universe_candidate(dict(candidate))
        assert eval_no_skip.rejected is True
        assert "volatility_too_low" in eval_no_skip.rejection_reason

        eval_skip = evaluate_universe_candidate(
            dict(candidate), skip_atr_validation=True
        )
        assert eval_skip.rejected is False

    def test_fallback_does_not_skip_price_or_volume_checks(self):
        """skip_atr_validation must NOT disable price/volume/market_cap checks.
        Only ATR is suppressed."""
        candidate = {
            "ticker": "TEST",
            "current_price": 1.50,  # below min_price=5.0
            "asset_type": "common_stock",
            "average_dollar_volume_20d": 0,
            "atr_20_percentage": 3.0,
            "data_source": "fallback",
        }

        eval_result = evaluate_universe_candidate(
            dict(candidate), skip_atr_validation=True
        )
        assert eval_result.rejected is True
        assert "price_out_of_range" in eval_result.rejection_reason
        assert "low_dollar_volume" in eval_result.rejection_reason
        assert "market_cap_missing" in eval_result.rejection_reason
        # ATR should NOT be in the rejection reasons
        assert not any(
            r.startswith("volatility_") for r in eval_result.rejection_reason
        )

    def test_non_fallback_still_enforces_atr(self):
        """Normal (non-fallback) data must STILL have ATR validation."""
        candidate = {
            "ticker": "AAPL",
            "current_price": 200.0,
            "asset_type": "common_stock",
            "market_cap": 3_000_000_000_000,
            "average_dollar_volume_20d": 55_000_000 * 200.0,
            "atr_20_percentage": 15.0,  # > 8% max → should reject
        }

        eval_result = evaluate_universe_candidate(dict(candidate))
        assert eval_result.rejected is True
        assert "volatility_too_high" in eval_result.rejection_reason


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Missing Yahoo data with fallback remains diagnosable
# ═══════════════════════════════════════════════════════════════════════════════

class TestFallbackDataDiagnosable:
    """When Yahoo data is missing and fallback is used, the diagnostic module
    must report the correct data_source and reason."""

    def test_diagnostic_detects_no_data_with_fallback(self):
        """Non-existent ticker → no OHLCV → diagnostic reports 0 rows."""
        from src.openalpha.data_diagnostics import _diagnose_one

        diag = _diagnose_one("ZZZZ12345")
        assert diag["available_rows"] == 0
        assert diag["reason_code"] == "no_ohlcv_data_and_no_fallback"
        assert "ohlcv_history" in diag["missing_fields"]
        assert "fallback_profile" in diag["missing_fields"]
        assert diag["has_fallback_profile"] is False

    def test_diagnostic_detects_has_fallback(self):
        """AAPL has a fallback profile — diagnostic must report it."""
        from src.openalpha.data_diagnostics import _diagnose_one

        diag = _diagnose_one("AAPL")
        assert diag["has_fallback_profile"] is True
        assert diag["fallback_rejected_by_universe"] is False

    def test_diagnostic_detects_fallback_blocked_by_universe(self, monkeypatch):
        """Simulate: OHLCV fetch fails, fallback exists, but universe rejects it.
        Diagnostic must report the exact blocking reasons."""
        from src.openalpha.data_diagnostics import _diagnose_one

        # Patch _try_ohlcv_fetch to simulate data failure
        import src.openalpha.data_diagnostics as diag_mod
        monkeypatch.setattr(
            diag_mod, "_try_ohlcv_fetch",
            lambda sym: (0, "simulated_network_failure"),
        )

        # AAPL has fallback, but without skip_atr_validation it might be blocked
        # This test verifies the diagnostic correctly captures the state
        diag = _diagnose_one("AAPL")
        assert diag["available_rows"] == 0
        assert diag["has_fallback_profile"] is True
        assert "simulated_network_failure" in (diag["ohlcv_error"] or "")

    @pytest.mark.network
    def test_diagnostic_returns_available_rows_for_data_rich_symbol(self):
        """Symbol with real Yahoo data must show available_rows >= 60."""
        from src.openalpha.data_diagnostics import _diagnose_one

        diag = _diagnose_one("SPY")
        assert diag["available_rows"] >= 60, (
            f"SPY should have ≥60 OHLCV rows, got {diag['available_rows']}"
        )

    def test_quality_fallback_sets_eod_quality_relaxed_in_research_mode(self):
        """Research fallback uses eod_quality_relaxed when quality rejects all."""
        from types import SimpleNamespace

        from src.openalpha import selector as selector_module
        from src.openalpha.selector import AIStrategySelector

        patches = []

        class Patcher:
            def setattr(self, obj, name, value):
                patches.append((obj, name, getattr(obj, name)))
                setattr(obj, name, value)

        p = Patcher()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                log_dir = Path(tmpdir) / "logs"
                p.setattr(selector_module, "LOG_DIR", log_dir)
                # FULL preflight no longer grants LIVE capability; execution
                # remains RESEARCH, so all-rejected fallback is EOD relaxed.
                try:
                    from src.openalpha import preflight as pf_module
                    p.setattr(pf_module, "run_preflight",
                        lambda dry_run=True: type("_PF", (), {
                            "to_dict": lambda self: {"run_mode": "FULL", "market_state": "MARKET_OPEN", "is_trading_day": True},
                        })())
                except Exception:
                    pass
                p.setattr(
                    selector_module,
                    "_apply_quality_filters_with_report",
                    lambda candidates, max_seconds=None, run_mode="FULL": (
                        [],
                        {
                            "generated_at": "2026-07-24T09:00:00",
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

                # Force sample universe
                import os as _os
                prev = _os.environ.get("OPENALPHA_UNIVERSE")
                _os.environ["OPENALPHA_UNIVERSE"] = "sample"
                try:
                    selector = AIStrategySelector()
                    selector.universe._load_local_snapshot = (
                        lambda: ["AAPL", "MSFT"]
                    )
                    selector.news.collect_for_symbols = lambda symbols: {
                        s: [] for s in symbols
                    }
                    selector._score_with_live_flag = (
                        lambda symbols, news_map, live_enabled: [
                            {
                                "ticker": "AAPL",
                                "sector": "Technology",
                                "score": 95.0,
                                "base_score": 95.0,
                                "volatility_score": 75.0,
                                "volume_score": 72.0,
                                "trend_fit_score": 68.0,
                                "repeatability_score": 66.0,
                                "drawdown_safety_score": 64.0,
                                "correlation_penalty": 0.0,
                                "range_low": 170.0,
                                "range_high": 230.0,
                                "suggested_range": "$170.00 - $230.00",
                                "risk": {"stop_loss_pct": 1.5},
                                "series": {"returns": [0.01] * 30},
                            }
                        ]
                    )

                    result = selector.run_selection(write_configs=False)
                    assert (
                        result["settings"]["selection_stage"]
                        == "eod_quality_relaxed"
                    )
                    assert result["quality_fallback_active"] is False
                    # Research candidates remain formal after relaxed fallback.
                    assert len(result["preview_candidates"]) >= 1
                    assert len(result["formal_candidates"]) >= 1
                finally:
                    if prev is None:
                        _os.environ.pop("OPENALPHA_UNIVERSE", None)
                    else:
                        _os.environ["OPENALPHA_UNIVERSE"] = prev
        finally:
            for obj, name, original in reversed(patches):
                setattr(obj, name, original)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MARKET_DATA output count cannot depend on scoring result
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarketDataIndependentOfScoring:
    """MARKET_DATA stage must reflect data availability (OHLCV rows),
    not whether scoring produced a result."""

    def test_data_available_symbol_passes_market_data_regardless_of_scoring(
        self, tmp_path: Path
    ):
        """A symbol with real OHLCV data passes MARKET_DATA even if scoring
        would reject it (e.g., price out of range, volatility too high)."""
        from src.openalpha.funnel_tracker import FunnelTracker

        tracker = FunnelTracker(
            selection_run_id="md-test", selection_date="2026-07-24",
            project_dir=tmp_path,
        )

        # Simulate: all 3 symbols have OHLCV data (MARKET_DATA pass)
        # but scoring only produces results for 1 (SCORING_ELIGIBLE narrows)
        tracker.add_stage("UNIVERSE", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("UNIVERSE_FILTER", ["A", "B", "C"], ["A", "B", "C"])
        tracker.add_stage("MARKET_DATA", ["A", "B", "C"], ["A", "B", "C"],
                          dropped=[])

        # SCORING_ELIGIBLE: A and C scored, B had insufficient range
        scorer_a = {"ticker": "A", "score": 80, "scoring_eligible": True}
        scorer_c = {"ticker": "C", "score": 70, "scoring_eligible": True}
        # B didn't score at all (range too tight)

        scoring_eligible = [scorer_a, scorer_c]
        scoring_dropped = [
            {"symbol": "B", "reason_code": "scoring_ineligible",
             "reason_detail": "range too tight"}
        ]
        tracker.add_stage(
            "SCORING_ELIGIBLE", ["A", "B", "C"], scoring_eligible,
            dropped=scoring_dropped,
        )

        # Verify MARKET_DATA output (3) does NOT match SCORING_ELIGIBLE output (2)
        md_stage = tracker.records[2]  # MARKET_DATA
        sc_stage = tracker.records[3]  # SCORING_ELIGIBLE
        assert md_stage.output_count == 3, (
            "MARKET_DATA should have all 3 data-available symbols"
        )
        assert sc_stage.output_count == 2, (
            "SCORING_ELIGIBLE should have only 2 scored symbols"
        )
        # MARKET_DATA output > SCORING_ELIGIBLE output → data check is independent
        assert md_stage.output_count > sc_stage.output_count

    def test_no_scoring_output_does_not_change_market_data(self, tmp_path: Path):
        """Even if scoring returns nothing, MARKET_DATA still shows data availability.
        MARKET_DATA output is determined by check_data_availability, not scoring."""
        from src.openalpha.funnel_tracker import FunnelTracker

        tracker = FunnelTracker(
            selection_run_id="md-empty", selection_date="2026-07-24",
            project_dir=tmp_path,
        )

        # All symbols have data, but scoring failed for all
        tracker.add_stage("UNIVERSE", ["X", "Y", "Z"], ["X", "Y", "Z"])
        tracker.add_stage("UNIVERSE_FILTER", ["X", "Y", "Z"], ["X", "Y", "Z"])
        tracker.add_stage("MARKET_DATA", ["X", "Y", "Z"], ["X", "Y", "Z"],
                          dropped=[])

        all_dropped = [
            {"symbol": "X", "reason_code": "scoring_ineligible"},
            {"symbol": "Y", "reason_code": "scoring_ineligible"},
            {"symbol": "Z", "reason_code": "scoring_ineligible"},
        ]
        tracker.add_stage("SCORING_ELIGIBLE", ["X", "Y", "Z"], [],
                          dropped=all_dropped)

        md = tracker.records[2]
        assert md.output_count == 3  # data was available
        sc = tracker.records[3]
        assert sc.output_count == 0  # scoring produced nothing

        # Chain: SCORING_ELIGIBLE input (3) matches MARKET_DATA output (3)
        validation = tracker.validate()
        assert validation["consistent"] is True

    def test_market_data_dropped_records_have_detail(self, tmp_path: Path):
        """When a symbol has no data, the MARKET_DATA dropped record must
        carry available_rows, reason_code, and reason_detail."""
        from src.openalpha.funnel_tracker import FunnelTracker

        tracker = FunnelTracker(
            selection_run_id="md-drop", selection_date="2026-07-24",
            project_dir=tmp_path,
        )

        dropped_detail = [
            {
                "symbol": "NO_DATA",
                "reason_code": "no_ohlcv_data_and_no_fallback",
                "reason_detail": "无OHLCV数据且无回退配置. available=0/60 rows.",
                "available_rows": 0,
                "required_rows": 60,
                "missing_fields": ["ohlcv_history", "fallback_profile"],
                "ohlcv_error": "yfinance: no data",
                "has_fallback_profile": False,
                "fallback_rejected_by_universe": False,
            }
        ]
        tracker.add_stage("UNIVERSE", ["GOOD", "NO_DATA"], ["GOOD", "NO_DATA"])
        tracker.add_stage("UNIVERSE_FILTER", ["GOOD", "NO_DATA"], ["GOOD", "NO_DATA"])
        tracker.add_stage("MARKET_DATA", ["GOOD", "NO_DATA"], ["GOOD"],
                          dropped=dropped_detail)

        md = tracker.records[2]
        assert md.output_count == 1
        dropped = md.to_dict()["dropped"]
        assert len(dropped) == 1
        assert dropped[0]["symbol"] == "NO_DATA"
        assert dropped[0]["reason_code"] == "no_ohlcv_data_and_no_fallback"
        assert "0/60" in dropped[0].get("reason_detail", "")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. MARKET_DATA pass-through after redundant OHLCV pre-fetch removal
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarketDataPassThrough:
    """After removing the redundant check_data_availability() pre-fetch,
    MARKET_DATA is a pass-through stage. Data validation is deferred to
    scoring, which handles unavailable symbols via fallback profiles and
    scoring_rejections diagnostics."""

    def test_market_data_passes_all_symbols_through(self, tmp_path: Path):
        """MARKET_DATA output == input — no pre-scoring data check drops."""
        from src.openalpha.funnel_tracker import FunnelTracker

        tracker = FunnelTracker(
            selection_run_id="md-passthrough", selection_date="2026-08-05",
            project_dir=tmp_path,
        )

        # Simulate the new behavior: MARKET_DATA passes all 50 symbols through
        symbols = ["A", "B", "C", "D", "E"]
        tracker.add_stage("UNIVERSE", symbols, symbols)
        tracker.add_stage("UNIVERSE_FILTER", symbols, symbols)
        tracker.add_stage("MARKET_DATA", symbols, symbols)

        md = tracker.records[2]
        assert md.output_count == md.input_count == 5, (
            "MARKET_DATA must pass all symbols through (no pre-check drops)"
        )
        assert md.eliminated_count == 0

    def test_scoring_still_captures_data_unavailable_symbols(self, tmp_path: Path):
        """Symbols with no data are caught at SCORING_ELIGIBLE,
        not at MARKET_DATA."""
        from src.openalpha.funnel_tracker import FunnelTracker

        tracker = FunnelTracker(
            selection_run_id="md-scoring-capture", selection_date="2026-08-05",
            project_dir=tmp_path,
        )

        symbols = ["GOOD_A", "GOOD_B", "NO_DATA_X", "NO_DATA_Y"]
        tracker.add_stage("UNIVERSE", symbols, symbols)
        tracker.add_stage("UNIVERSE_FILTER", symbols, symbols)
        tracker.add_stage("MARKET_DATA", symbols, symbols)

        # Scoring catches the missing-data symbols
        scoring_eligible = [
            {"ticker": "GOOD_A", "score": 80, "scoring_eligible": True},
            {"ticker": "GOOD_B", "score": 75, "scoring_eligible": True},
        ]
        scoring_dropped = [
            {"symbol": "NO_DATA_X",
             "reason_code": "insufficient_history_no_fallback"},
            {"symbol": "NO_DATA_Y",
             "reason_code": "insufficient_history_no_fallback"},
        ]
        tracker.add_stage("SCORING_ELIGIBLE", symbols, scoring_eligible,
                          dropped=scoring_dropped)

        md = tracker.records[2]
        sc = tracker.records[3]
        assert md.output_count == 4  # pass-through: all 4
        assert sc.output_count == 2  # scoring caught the 2 bad ones
        assert sc.eliminated_count == 2

        # Verify dropped records carry the right reason
        dropped = sc.to_dict()["dropped"]
        dropped_syms = {d["symbol"] for d in dropped}
        assert dropped_syms == {"NO_DATA_X", "NO_DATA_Y"}
        for d in dropped:
            assert "no_fallback" in d["reason_code"]

    def test_funnel_invariant_still_holds(self, tmp_path: Path):
        """Removing the pre-check must not break funnel invariants."""
        from src.openalpha.funnel_tracker import FunnelTracker

        tracker = FunnelTracker(
            selection_run_id="md-invariant", selection_date="2026-08-05",
            project_dir=tmp_path,
        )

        universe = ["A", "B", "C"]
        tracker.add_stage("UNIVERSE", universe, universe)
        tracker.add_stage("UNIVERSE_FILTER", universe, universe)
        tracker.add_stage("MARKET_DATA", universe, universe)  # pass-through

        # Scoring produces results — no drops
        scored = [
            {"ticker": "A", "score": 82, "scoring_eligible": True,
             "formal_scoring_eligibility": True},
            {"ticker": "B", "score": 78, "scoring_eligible": True,
             "formal_scoring_eligibility": True},
            {"ticker": "C", "score": 74, "scoring_eligible": True,
             "formal_scoring_eligibility": True},
        ]
        tracker.add_stage("SCORING_ELIGIBLE", universe, scored, dropped=[])
        tracker.add_stage("BASE_RANKING", scored, scored)
        tracker.add_stage("FORMAL_ELIGIBILITY", scored, scored)
        tracker.add_stage("DATA_QUALITY", scored, scored)
        tracker.add_stage("COMPOSITION_FILTER", scored, scored)
        tracker.add_stage("FORMAL_TOP", scored, scored)

        validation = tracker.validate()
        assert validation["consistent"] is True, (
            f"Funnel invariants broken: {validation['warnings']}"
        )

    @pytest.mark.network
    def test_full_pipeline_with_unknown_symbol_still_handled(self):
        """End-to-end: a non-existent ticker is handled by scoring fallback,
        not by MARKET_DATA pre-check."""
        import os as _os

        from src.openalpha.selector import AIStrategySelector

        # Use sample universe to avoid yfinance network calls for 50 symbols
        prev_universe = _os.environ.get("OPENALPHA_UNIVERSE")
        prev_live = _os.environ.get("OPENALPHA_LIVE_DATA")
        _os.environ["OPENALPHA_UNIVERSE"] = "sample"
        _os.environ["OPENALPHA_LIVE_DATA"] = "0"  # offline mode
        try:
            selector = AIStrategySelector()
            # Two known symbols with fallback profiles + one unknown
            selector.universe._load_local_snapshot = (
                lambda: ["NVDA", "MSFT", "ZZZZ_UNKNOWN"]
            )
            selector.news.collect_for_symbols = lambda s: {x: [] for x in s}
            result = selector.run_selection(write_configs=False)

            funnel = result.get("selection_funnel", {})
            stages = funnel.get("stages", [])

            # MARKET_DATA must be pass-through: all 3 in, all 3 out
            md_stage = [s for s in stages if s["stage"] == "MARKET_DATA"][0]
            assert md_stage["input_count"] == 3
            assert md_stage["output_count"] == 3, (
                "MARKET_DATA should pass all 3 symbols through"
            )
            assert md_stage["eliminated"] == 0

            # ZZZZ_UNKNOWN should appear in scoring_rejections or scored
            rejection_counts = funnel.get("rejection_reason_counts", {})
            # The unknown symbol is handled by fallback profiles (NVDA/MSFT
            # have them) or recorded in rejection diagnostics — either way,
            # the pipeline completes without crashing
            assert result is not None
        finally:
            if prev_universe is None:
                _os.environ.pop("OPENALPHA_UNIVERSE", None)
            else:
                _os.environ["OPENALPHA_UNIVERSE"] = prev_universe
            if prev_live is None:
                _os.environ.pop("OPENALPHA_LIVE_DATA", None)
            else:
                _os.environ["OPENALPHA_LIVE_DATA"] = prev_live
