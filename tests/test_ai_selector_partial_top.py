from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "run_ai_selector.py"
MULTI_LAUNCH = PROJECT_DIR / "multi_launch.sh"
HEALTH_CHECK = PROJECT_DIR / "health_check.sh"


def _load_module():
    os.environ["SOXS_SKIP_VENV_REEXEC"] = "1"
    if "bs4" not in sys.modules:
        fake_bs4 = importlib.util.module_from_spec(importlib.util.spec_from_loader("bs4", loader=None))

        class _FakeSoup:
            def __init__(self, *args, **kwargs):
                pass

        fake_bs4.BeautifulSoup = _FakeSoup
        sys.modules["bs4"] = fake_bs4
    if "yfinance" not in sys.modules:
        fake_yfinance = importlib.util.module_from_spec(importlib.util.spec_from_loader("yfinance", loader=None))

        class _FakeTicker:
            def __init__(self, *args, **kwargs):
                self.fast_info = {}

            def history(self, *args, **kwargs):
                return None

        fake_yfinance.Ticker = _FakeTicker
        sys.modules["yfinance"] = fake_yfinance
    if "ta" not in sys.modules:
        fake_ta = importlib.util.module_from_spec(importlib.util.spec_from_loader("ta", loader=None))
        fake_ta_momentum = importlib.util.module_from_spec(importlib.util.spec_from_loader("ta.momentum", loader=None))
        fake_ta_trend = importlib.util.module_from_spec(importlib.util.spec_from_loader("ta.trend", loader=None))
        fake_ta_volatility = importlib.util.module_from_spec(importlib.util.spec_from_loader("ta.volatility", loader=None))

        def _noop(*args, **kwargs):
            return 50.0

        class _FakeMACD:
            def __init__(self, *args, **kwargs):
                pass

            def macd_diff(self):
                return 0.0

            def macd(self):
                return 0.0

            def macd_signal(self):
                return 0.0

        class _FakeATR:
            def __init__(self, *args, **kwargs):
                pass

            def average_true_range(self):
                return 1.0

        fake_ta_momentum.rsi = _noop
        fake_ta_trend.MACD = _FakeMACD
        fake_ta_volatility.AverageTrueRange = _FakeATR
        sys.modules["ta"] = fake_ta
        sys.modules["ta.momentum"] = fake_ta_momentum
        sys.modules["ta.trend"] = fake_ta_trend
        sys.modules["ta.volatility"] = fake_ta_volatility
    spec = importlib.util.spec_from_file_location("test_partial_top_run_ai_selector", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _base_selector_result() -> dict:
    return {
        "top10": [
            {
                "ticker": "SOXS",
                "score": 80.0,
                "range_low": 4.0,
                "range_high": 5.4,
                "risk": {"stop_loss_pct": 1.5},
                "size": 1000,
                "confidence": 0.8,
                "reason": "stub",
                "source": "stub",
                "trade_filter_passed": True,
                "reject_reason": "",
                "fallback_used": False,
            },
            {
                "ticker": "SOFI",
                "score": 70.0,
                "range_low": 15.0,
                "range_high": 19.0,
                "risk": {"stop_loss_pct": 1.5},
                "size": 500,
                "confidence": 0.7,
                "reason": "stub",
                "source": "stub",
                "trade_filter_passed": True,
                "reject_reason": "",
                "fallback_used": False,
            },
        ],
        "top5": [
            {
                "ticker": "SOXS",
                "score": 80.0,
                "range_low": 4.0,
                "range_high": 5.4,
                "risk": {"stop_loss_pct": 1.5},
                "size": 1000,
                "confidence": 0.8,
                "reason": "stub",
                "source": "stub",
                "trade_filter_passed": True,
                "reject_reason": "",
                "fallback_used": False,
            },
            {
                "ticker": "SOFI",
                "score": 70.0,
                "range_low": 15.0,
                "range_high": 19.0,
                "risk": {"stop_loss_pct": 1.5},
                "size": 500,
                "confidence": 0.7,
                "reason": "stub",
                "source": "stub",
                "trade_filter_passed": True,
                "reject_reason": "",
                "fallback_used": False,
            },
        ],
        "top3": [],
        "report": [],
        "settings": {"selection_stage": "quality_refined"},
        "quality_filter_report": {},
    }


def _patch_common(module, tmpdir: Path):
    original_universe_filter = module._finalize_universe_filter

    def _finalize_universe_filter_with_test_metrics(candidates):
        enriched = []
        for raw in candidates or []:
            item = dict(raw)
            ticker = str(item.get("ticker") or "").strip().upper()
            if item.get("current_price") is None:
                price = module._live_candidate_price(ticker)
                if price is None:
                    low = item.get("range_low")
                    high = item.get("range_high")
                    if low and high and high > low:
                        price = (float(low) + float(high)) / 2.0
                if price is not None:
                    item["current_price"] = price
            if item.get("asset_type") is None:
                if ticker in {"SOXS", "LABD", "DRIP", "SQQQ", "TZA", "FAZ", "YANG"}:
                    item["asset_type"] = "inverse_etf"
                elif ticker in {"YINN", "SOXL"}:
                    item["asset_type"] = "leveraged_etf"
                else:
                    item["asset_type"] = "common_stock"
            item.setdefault("average_dollar_volume_20d", 100_000_000)
            item.setdefault("atr_20_percentage", 4.0)
            if item["asset_type"] == "common_stock":
                item.setdefault("market_cap", 10_000_000_000)
            item.setdefault("ma20", float(item["current_price"]) * 0.98 if item.get("current_price") else 1.0)
            item.setdefault("ma50", float(item["current_price"]) * 0.95 if item.get("current_price") else 1.0)
            item.setdefault("ma200", float(item["current_price"]) * 0.90 if item.get("current_price") else 1.0)
            item.setdefault("quote_timestamp", "2026-07-16T13:00:00Z")
            item.setdefault("quote_age_seconds", 60)
            item.setdefault("daily_data_as_of", "2026-07-15")
            item.setdefault("benchmark_data_as_of", "2026-07-15")
            item.setdefault("benchmark_status", "VALID")
            item.setdefault("benchmark_alignment_status", "VALID")
            item.setdefault("daily_data_status", "VALID")
            item.setdefault("freshness_status", "SAFE")
            item.setdefault("quote_status", "COMPLETE")
            item.setdefault("ohlcv_status", "COMPLETE")
            item.setdefault("history_status", "COMPLETE")
            item.setdefault("history_rows", 250)
            item.setdefault("history_available_bars", 250)
            item.setdefault("history_required_bars", 200)
            item.setdefault("history_missing_windows", [])
            if not item.get("close_history"):
                item["close_history"] = [float(item["current_price"]) if item.get("current_price") else 1.0] * 250
            item.setdefault("open", float(item["current_price"]) * 0.99 if item.get("current_price") else 1.0)
            item.setdefault("high", float(item["current_price"]) * 1.01 if item.get("current_price") else 1.0)
            item.setdefault("low", float(item["current_price"]) * 0.98 if item.get("current_price") else 1.0)
            item.setdefault("close", float(item["current_price"]) if item.get("current_price") else 1.0)
            item.setdefault("volume", 1_000_000)
            item.setdefault("data_status", "COMPLETE")
            item.setdefault("scoring_eligible", True)
            item.setdefault("current_validation_status", "DATA_VALID")
            item.setdefault("trade_admission_status", "TRADABLE")
            item.setdefault("trade_admission", "TRADABLE")
            item.setdefault("fallback_scope", "EXPLANATION_ONLY")
            item.setdefault("fallback_severity", "INFO")
            enriched.append(item)
        return original_universe_filter(enriched)

    module.PROJECT_DIR = tmpdir
    module.REPORTS_DIR = tmpdir / "reports"
    module.load_local_ai_env = lambda: None
    module.load_runtime_settings = lambda: {
        "min_price": 10.0,
        "max_price": 50.0,
        "auto_refresh_minutes": 5,
        "max_symbols": 20,
    }
    module._live_equity_positions = lambda: []
    module._has_live_top_configs = lambda: False
    module._restart_top_engines = lambda: 0
    module._spawn_background_refinement = lambda timestamp: None
    module.write_selection_filter_log = lambda payload: None
    module.write_selection_state = lambda **payload: None
    module._notify_selection_result = lambda *args, **kwargs: None
    module._run_integrated_ai_selector = lambda: {
        "enabled": True,
        "top3": [],
        "top10": [],
        "preferred_symbols": [],
        "signal_map": {},
        "providers_used": [],
        "providers_disabled": ["openbb", "fmp"],
        "fmp_enabled": False,
        "fallback_used": False,
    }
    module.AIStrategySelector = type(
        "FakeSelector",
        (),
        {
            "selection_size": 3,
            "__init__": lambda self, *args, **kwargs: None,
            "run_selection": lambda self, write_configs=True, symbols_override=None: _base_selector_result(),
            "_format_report_rows": lambda self, selected: [
                {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                for idx, row in enumerate(selected)
            ],
        },
    )
    module._apply_range_scores = lambda rows: list(rows)
    module._enrich_candidate_quality_rows = lambda rows, provider_audit=None, provider_outputs=None: [dict(item) for item in rows]
    module._apply_trade_filter = lambda rows: (
        list(rows),
        {"fallback_used": False, "accepted": list(rows), "rejected": [], "warnings": []},
    )
    module._apply_composition_filter = lambda rows, top_n=3: (list(rows), {"rejected": [], "warnings": []})
    module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: list(selector_top10 or selected)
    module._prioritize_ai_rank = lambda rows, signal_map: list(rows)
    module._split_selected_and_protected_positions = lambda candidates, positions, limit=3: (list(candidates)[:limit], [])
    module._enforce_price_band = lambda candidates, min_price, max_price: (list(candidates), [])
    module._finalize_universe_filter = _finalize_universe_filter_with_test_metrics
    module._live_candidate_price = lambda ticker: {
        "PLTR": 25.0,
        "AMD": 28.0,
        "AAPL": 27.0,
        "BAC": 29.0,
        "F": 12.0,
        "T": 18.0,
        "PFE": 28.0,
        "KO": 29.0,
        "INTC": 25.0,
        "SOFI": 17.0,
    }.get(str(ticker).upper())


def test_fast_preliminary_final_top_enforces_leveraged_etf_limit_and_fallback_metadata():
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for folder in ("configs", "reports", "state", "logs", "runtime"):
            (tmpdir / folder).mkdir()
        (tmpdir / "configs" / "TOP2.yaml").write_text("ticker: OLD2\nmode: paper\n", encoding="utf-8")
        (tmpdir / "configs" / "TOP3.yaml").write_text("ticker: OLD3\nmode: paper\n", encoding="utf-8")

        from src.openalpha import config_writer

        original_base = config_writer.BASE
        original_composition_filter = module._apply_composition_filter
        captured_bundles: list[dict] = []

        def _row(ticker: str, score: float) -> dict:
            price = {"SOXS": 12.0, "YINN": 25.0, "DRIP": 11.0}[ticker]
            return {
                "ticker": ticker,
                "score": score,
                "final_score": score,
                "ai_score": score,
                "range_score": score,
                "range_low": price * 0.9,
                "range_high": price * 1.1,
                "current_price": price,
                "risk": {"stop_loss_pct": 1.5},
                "size": 10,
                "confidence": 0.7,
                "reason": "stub",
                "source": "stub",
            }

        result = {
            "top10": [_row("SOXS", 90.0), _row("YINN", 89.0), _row("DRIP", 88.0)],
            "top5": [_row("SOXS", 90.0), _row("YINN", 89.0), _row("DRIP", 88.0)],
            "top3": [],
            "report": [],
            "settings": {"selection_stage": "fast_preliminary"},
            "quality_filter_report": {},
        }

        try:
            config_writer.BASE = str(tmpdir)
            _patch_common(module, tmpdir)
            module._apply_composition_filter = original_composition_filter
            module._live_candidate_price = lambda ticker: {"SOXS": 12.0, "YINN": 25.0, "DRIP": 11.0}.get(str(ticker).upper())
            module._run_integrated_ai_selector = lambda: {
                "enabled": True,
                "top3": [],
                "top10": [],
                "preferred_symbols": [],
                "signal_map": {},
                "providers_used": [],
                "providers_disabled": ["openbb", "fmp"],
                "fmp_enabled": False,
                "fallback_used": True,
            }
            module.AIStrategySelector = type(
                "FakeSelector",
                (),
                {
                    "selection_size": 3,
                    "__init__": lambda self, *args, **kwargs: None,
                    "run_selection": lambda self, write_configs=True, symbols_override=None: result,
                    "_format_report_rows": lambda self, selected: [
                        {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                        for idx, row in enumerate(selected)
                    ],
                },
            )
            module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
                "selection_run_id": payload.get("selection_run_id", "run-1"),
                "selection_bundle_hash": "bundle-hash",
                "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
                "selection_date": payload.get("selection_date", "2026-07-16"),
                "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
                "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
                "disabled_slots": [2, 3],
                "selected_symbols": ["SOXS"],
                "audit_path": "state/selection_sync_audit.json",
                "state_path": "state/ai_selection_state.json",
                "report_path": "reports/ai_selection_latest.json",
                "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
            }
            os.environ["OPENALPHA_RESTART_TOP"] = "0"
            os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
            try:
                module.main()
            finally:
                os.environ.pop("OPENALPHA_RESTART_TOP", None)
                os.environ.pop("OPENALPHA_BACKGROUND_REFINEMENT", None)
        finally:
            config_writer.BASE = original_base

        assert captured_bundles
        summary = captured_bundles[0]["summary"]
        assert summary["fallback_used"] is True
        assert summary["settings"]["fallback_used"] is True
        assert summary["selection_count"] == 0
        assert summary["top_n_filled"] is False
        assert any(
            item.get("reason") == "leveraged_etf_limit_exceeded"
            for item in summary["composition_filter"]["rejected"]
        )
        top_items = captured_bundles[0]["top_items"]
        assert top_items == []
        assert summary["missing_slots"] == 3


def test_partial_top_uses_conservative_fallback_pool_and_writes_top3():
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for folder in ("configs", "reports", "state", "logs", "runtime"):
            (tmpdir / folder).mkdir()
        (tmpdir / "configs" / "TOP3.yaml").write_text("ticker: OLD\nmode: paper\n", encoding="utf-8")

        from src.openalpha import config_writer

        original_base = config_writer.BASE
        original_fallback_builder = module._build_conservative_fallback_candidates
        original_apply_trade_filter = module._apply_trade_filter
        captured_bundles: list[dict] = []
        try:
            config_writer.BASE = str(tmpdir)
            _patch_common(module, tmpdir)
            module._apply_trade_filter = original_apply_trade_filter
            module._build_conservative_fallback_candidates = original_fallback_builder
            original_live_candidate_price = module._live_candidate_price
            module._live_candidate_price = lambda ticker: {
                "PLTR": 25.0,
                "AMD": 28.0,
                "AAPL": 309.4,
                "BAC": 29.0,
                "F": 12.0,
                "T": 18.0,
                "PFE": 28.0,
                "KO": 29.0,
                "INTC": 25.0,
                "SOFI": 17.0,
            }.get(str(ticker).upper())
            module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
                "selection_run_id": payload.get("selection_run_id", "run-1"),
                "selection_bundle_hash": "bundle-hash",
                "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
                "selection_date": payload.get("selection_date", "2026-07-16"),
                "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
                "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
                "disabled_slots": [2],
                "selected_symbols": ["SOFI", "AMD", "BAC"],
                "audit_path": "state/selection_sync_audit.json",
                "state_path": "state/ai_selection_state.json",
                "report_path": "reports/ai_selection_latest.json",
                "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
            }
            os.environ["OPENALPHA_RESTART_TOP"] = "0"
            os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
            try:
                module.main()
            finally:
                os.environ.pop("OPENALPHA_RESTART_TOP", None)
                os.environ.pop("OPENALPHA_BACKGROUND_REFINEMENT", None)
        finally:
            module._live_candidate_price = original_live_candidate_price
            config_writer.BASE = original_base

        assert captured_bundles
        summary = captured_bundles[0]["summary"]
        assert summary["selection_count"] == 0
        assert summary["target_top_n"] == 3
        assert summary["top_n_filled"] is False
        assert summary["missing_slots"] == 3
        assert summary["fallback_pool_used"] is True
        assert summary["disabled_configs"] == ["TOP1.yaml", "TOP2.yaml", "TOP3.yaml"]
        assert summary["quality_filter_report"]["fallback_pool_used"] is True
        assert summary["quality_filter_report"]["top_n_filled"] is False
        assert captured_bundles[0]["top_items"] == []


def test_partial_top_without_fallback_deletes_stale_top3_and_reports_missing_slot():
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for folder in ("configs", "reports", "state", "logs", "runtime"):
            (tmpdir / folder).mkdir()
        (tmpdir / "configs" / "TOP3.yaml").write_text("ticker: OLD\nmode: paper\n", encoding="utf-8")

        from src.openalpha import config_writer

        original_base = config_writer.BASE
        captured_bundles: list[dict] = []
        try:
            config_writer.BASE = str(tmpdir)
            _patch_common(module, tmpdir)
            module._build_conservative_fallback_candidates = lambda existing_symbols=None: []
            module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
                "selection_run_id": payload.get("selection_run_id", "run-1"),
                "selection_bundle_hash": "bundle-hash",
                "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
                "selection_date": payload.get("selection_date", "2026-07-16"),
                "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
                "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
                "disabled_slots": [2, 3],
                "selected_symbols": ["SOXS"],
                "audit_path": "state/selection_sync_audit.json",
                "state_path": "state/ai_selection_state.json",
                "report_path": "reports/ai_selection_latest.json",
                "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
            }
            os.environ["OPENALPHA_RESTART_TOP"] = "0"
            os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
            try:
                module.main()
            finally:
                os.environ.pop("OPENALPHA_RESTART_TOP", None)
                os.environ.pop("OPENALPHA_BACKGROUND_REFINEMENT", None)
        finally:
            config_writer.BASE = original_base

        assert captured_bundles
        summary = captured_bundles[0]["summary"]
        assert summary["selection_count"] == 0
        assert summary["target_top_n"] == 3
        assert summary["top_n_filled"] is False
        assert summary["missing_slots"] == 3
        assert summary["fallback_pool_used"] is False
        assert summary["disabled_configs"] == ["TOP1.yaml", "TOP2.yaml", "TOP3.yaml"]
        assert any(
            str(warning).startswith("top_n_not_filled")
            for warning in summary["quality_filter_report"]["composition_filter"]["warnings"]
        )
        top_items = captured_bundles[0]["top_items"]
        assert top_items == []
        assert summary["missing_slots"] == 3


def test_low_entry_quality_candidates_do_not_fill_top_slots():
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for folder in ("configs", "reports", "state", "logs", "runtime"):
            (tmpdir / folder).mkdir()

        from src.openalpha import config_writer

        original_base = config_writer.BASE
        captured_bundles: list[dict] = []
        try:
            config_writer.BASE = str(tmpdir)
            _patch_common(module, tmpdir)
            module._build_conservative_fallback_candidates = lambda existing_symbols=None: []
            module._apply_trade_filter = lambda rows: (
                list(rows),
                {"fallback_used": False, "accepted": list(rows), "rejected": [], "warnings": []},
            )
            module._apply_composition_filter = lambda rows, top_n=3: (list(rows), {"rejected": [], "warnings": []})
            module._live_candidate_price = lambda ticker: {"AAA": 10.0, "BBB": 11.0, "CCC": 12.0}.get(str(ticker).upper())
            module.AIStrategySelector = type(
                "FakeSelector",
                (),
                {
                    "selection_size": 3,
                    "__init__": lambda self, *args, **kwargs: None,
                    "run_selection": lambda self, write_configs=True, symbols_override=None: {
                        "top10": [
                            {
                                "ticker": "AAA",
                                "score": 90.0,
                                "current_price": 10.0,
                                "range_low": 9.5,
                                "range_high": 10.5,
                                "risk": {"stop_loss_pct": 1.5},
                                "size": 10,
                                "confidence": 0.9,
                                "reason": "current_run_score",
                                "source": "selector_core",
                                "entry": {
                                    "entry_proximity_score": 90,
                                    "good_for_entry_now": True,
                                    "entry_quality": "excellent",
                                    "entry_reason": "close to support",
                                    "range_position": 10,
                                    "dist_to_support": 2,
                                    "dist_to_resistance": 20,
                                },
                            },
                            {
                                "ticker": "BBB",
                                "score": 80.0,
                                "current_price": 11.0,
                                "range_low": 10.4,
                                "range_high": 11.6,
                                "risk": {"stop_loss_pct": 1.5},
                                "size": 10,
                                "confidence": 0.8,
                                "reason": "current_run_score",
                                "source": "selector_core",
                                "entry": {
                                    "entry_proximity_score": 30,
                                    "good_for_entry_now": False,
                                    "entry_quality": "poor",
                                    "entry_reason": "too far from support",
                                    "range_position": 70,
                                    "dist_to_support": 12,
                                    "dist_to_resistance": 6,
                                },
                            },
                            {
                                "ticker": "CCC",
                                "score": 70.0,
                                "current_price": 12.0,
                                "range_low": 11.4,
                                "range_high": 12.6,
                                "risk": {"stop_loss_pct": 1.5},
                                "size": 10,
                                "confidence": 0.7,
                                "reason": "current_run_score",
                                "source": "selector_core",
                                "entry": {
                                    "entry_proximity_score": 10,
                                    "good_for_entry_now": False,
                                    "entry_quality": "very_poor",
                                    "entry_reason": "near resistance",
                                    "range_position": 90,
                                    "dist_to_support": 15,
                                    "dist_to_resistance": 1,
                                },
                            },
                        ],
                        "top5": [
                            {
                                "ticker": "AAA",
                                "score": 90.0,
                                "current_price": 10.0,
                                "range_low": 9.5,
                                "range_high": 10.5,
                                "risk": {"stop_loss_pct": 1.5},
                                "size": 10,
                                "confidence": 0.9,
                                "reason": "current_run_score",
                                "source": "selector_core",
                                "entry": {
                                    "entry_proximity_score": 90,
                                    "good_for_entry_now": True,
                                    "entry_quality": "excellent",
                                    "entry_reason": "close to support",
                                    "range_position": 10,
                                    "dist_to_support": 2,
                                    "dist_to_resistance": 20,
                                },
                            },
                            {
                                "ticker": "BBB",
                                "score": 80.0,
                                "current_price": 11.0,
                                "range_low": 10.4,
                                "range_high": 11.6,
                                "risk": {"stop_loss_pct": 1.5},
                                "size": 10,
                                "confidence": 0.8,
                                "reason": "current_run_score",
                                "source": "selector_core",
                                "entry": {
                                    "entry_proximity_score": 30,
                                    "good_for_entry_now": False,
                                    "entry_quality": "poor",
                                    "entry_reason": "too far from support",
                                    "range_position": 70,
                                    "dist_to_support": 12,
                                    "dist_to_resistance": 6,
                                },
                            },
                            {
                                "ticker": "CCC",
                                "score": 70.0,
                                "current_price": 12.0,
                                "range_low": 11.4,
                                "range_high": 12.6,
                                "risk": {"stop_loss_pct": 1.5},
                                "size": 10,
                                "confidence": 0.7,
                                "reason": "current_run_score",
                                "source": "selector_core",
                                "entry": {
                                    "entry_proximity_score": 10,
                                    "good_for_entry_now": False,
                                    "entry_quality": "very_poor",
                                    "entry_reason": "near resistance",
                                    "range_position": 90,
                                    "dist_to_support": 15,
                                    "dist_to_resistance": 1,
                                },
                            },
                        ],
                        "top3": [],
                        "report": [],
                        "settings": {"selection_stage": "quality_refined"},
                        "quality_filter_report": {},
                    },
                    "_format_report_rows": lambda self, selected: [
                        {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                        for idx, row in enumerate(selected)
                    ],
                },
            )
            module.write_selection_bundle_atomic = lambda **payload: captured_bundles.append(dict(payload)) or {
                "selection_run_id": payload.get("selection_run_id", "run-1"),
                "selection_bundle_hash": "bundle-hash",
                "selection_bundle_manifest_path": "state/selection_bundle_manifest.json",
                "selection_date": payload.get("selection_date", "2026-07-16"),
                "generated_at": payload.get("generated_at", "2026-07-16T08:30:00-04:00"),
                "selection_stage": payload.get("selection_state_payload", {}).get("selection_stage", "FINALIZED"),
                "disabled_slots": [2, 3],
                "selected_symbols": ["AAA"],
                "audit_path": "state/selection_sync_audit.json",
                "state_path": "state/ai_selection_state.json",
                "report_path": "reports/ai_selection_latest.json",
                "top_paths": ["configs/TOP1.yaml", "configs/TOP2.yaml", "configs/TOP3.yaml"],
            }
            os.environ["OPENALPHA_RESTART_TOP"] = "0"
            os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
            try:
                module.main()
            finally:
                os.environ.pop("OPENALPHA_RESTART_TOP", None)
                os.environ.pop("OPENALPHA_BACKGROUND_REFINEMENT", None)
        finally:
            config_writer.BASE = original_base

        assert captured_bundles
        summary = captured_bundles[0]["summary"]
        assert summary["selection_count"] == 0
        assert summary["top_n_filled"] is False
        assert summary["quality_filter_report"]["removed_low_entry_quality"]
        assert summary["quality_filter_report"]["removed_low_entry_quality"][0]["reason"] == "entry_quality_too_low"
        assert summary["quality_filter_report"]["removed_low_entry_quality"][0]["ticker"] in {"BBB", "CCC"}
        top_items = captured_bundles[0]["top_items"]
        assert top_items == []
        assert summary["missing_slots"] == 3


def test_shell_scripts_treat_missing_top3_as_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for folder in ("configs", "reports", "logs", "runtime"):
            (tmpdir / folder).mkdir()
        (tmpdir / ".venv").symlink_to(PROJECT_DIR / ".venv", target_is_directory=True)
        (tmpdir / "configs" / "TOP1.yaml").write_text("ticker: SOXS\nmode: paper\n", encoding="utf-8")
        (tmpdir / "configs" / "TOP2.yaml").write_text("ticker: SOFI\nmode: paper\n", encoding="utf-8")

        env = os.environ.copy()
        env["SOXS_PROJECT_DIR"] = str(tmpdir)

        status = subprocess.run(
            ["bash", str(MULTI_LAUNCH), "status"],
            cwd=PROJECT_DIR,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert status.returncode == 0
        assert "TOP3: disabled / config missing" in status.stdout

        health = subprocess.run(
            ["bash", str(HEALTH_CHECK)],
            cwd=PROJECT_DIR,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert health.returncode == 0
        assert "TOP3: disabled / config missing" in health.stdout
