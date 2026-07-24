from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

from src.dashboard import combined


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "run_ai_selector.py"


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
    spec = importlib.util.spec_from_file_location("test_price_band_run_ai_selector", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _formal_candidate_row(ticker: str, score: float, price: float, *, reason: str = "research_complete", source: str = "selector") -> dict:
    return {
        "ticker": ticker,
        "score": score,
        "final_score": score,
        "ai_score": score,
        "range_score": score,
        "current_price": price,
        "range_low": round(price * 0.95, 4),
        "range_high": round(price * 1.05, 4),
        "average_dollar_volume_20d": 250000000.0,
        "atr_20_percentage": 2.5,
        "market_cap": 3000000000.0,
        "ma20": round(price * 0.98, 4),
        "ma50": round(price * 0.95, 4),
        "ma200": round(price * 0.90, 4),
        "quote_timestamp": "2026-07-16T13:00:00Z",
        "quote_age_seconds": 60,
        "daily_data_as_of": "2026-07-15",
        "benchmark_data_as_of": "2026-07-15",
        "benchmark_status": "VALID",
        "benchmark_alignment_status": "VALID",
        "daily_data_status": "VALID",
        "freshness_status": "SAFE",
        "quote_status": "COMPLETE",
        "ohlcv_status": "COMPLETE",
        "history_status": "COMPLETE",
        "history_rows": 30,
        "close_history": [price] * 30,
        "open": round(price * 0.99, 4),
        "high": round(price * 1.01, 4),
        "low": round(price * 0.98, 4),
        "close": price,
        "volume": 1_000_000,
        "data_status": "COMPLETE",
        "scoring_eligible": True,
        "current_validation_status": "DATA_VALID",
        "trade_admission_status": "TRADABLE",
        "trade_admission": "TRADABLE",
        "score_source": "current_run_candidate_ranking",
        "score_provider": "local_factor_scoring",
        "score_generated_at": "2026-07-16T09:00:00-04:00",
        "score_is_current_run": True,
        "confidence": 0.8,
        "reason": reason,
        "source": source,
    }


def _patch_common(module, tmpdir: Path):
    module.PROJECT_DIR = tmpdir
    module.REPORTS_DIR = tmpdir / "reports"
    module.load_local_ai_env = lambda: None
    module.load_runtime_settings = lambda: {
        "min_price": 5.0,
        "max_price": 300.0,
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
            "run_selection": lambda self, write_configs=True, symbols_override=None: {
                "top10": [
                    _formal_candidate_row("BRK.A", 80.0, 600000.0, reason="research_complete"),
                    _formal_candidate_row("SOFI", 70.0, 13.72, reason="research_complete"),
                    _formal_candidate_row("LOWVOL", 60.0, 112.97, reason="research_complete"),
                ],
                "top5": [
                    _formal_candidate_row("BRK.A", 80.0, 600000.0, reason="research_complete"),
                    _formal_candidate_row("SOFI", 70.0, 13.72, reason="research_complete"),
                    _formal_candidate_row("LOWVOL", 60.0, 112.97, reason="research_complete"),
                ],
                "top3": [],
                "report": [],
                "settings": {"selection_stage": "fast_preliminary"},
                "quality_filter_report": {},
            },
            "_format_report_rows": lambda self, selected: [
                {"rank": idx + 1, "ticker": row["ticker"], "score": row["score"]}
                for idx, row in enumerate(selected)
            ],
        },
    )
    module._apply_range_scores = lambda rows: list(rows)
    module._apply_trade_filter = lambda rows: (
        list(rows),
        {"fallback_used": False, "accepted": list(rows), "rejected": [], "warnings": []},
    )
    module._apply_composition_filter = lambda rows, top_n=3: (list(rows), {"rejected": [], "warnings": []})
    module._enrich_candidate_quality_rows = lambda rows, provider_audit=None, provider_outputs=None: [dict(item) for item in rows]
    module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: list(selector_top10 or selected)
    module._prioritize_ai_rank = lambda rows, signal_map: list(rows)
    module._split_selected_and_protected_positions = lambda candidates, positions, limit=3: (list(candidates)[:limit], [])
    module._live_candidate_price = lambda ticker: {
        "BRK.A": 600000.0,
        "SOFI": 13.72,
        "LOWVOL": 112.97,
    }.get(str(ticker).upper())


def test_universe_filter_rejects_only_rule_violations():
    module = _load_module()
    candidates = [
        {"ticker": "AAPL", "current_price": 150.0, "asset_type": "common_stock", "market_cap": 3_000_000_000_000, "average_dollar_volume_20d": 8_000_000_000, "atr_20_percentage": 2.0},
        {"ticker": "SOXS", "current_price": 20.0, "asset_type": "inverse_etf", "average_dollar_volume_20d": 50_000_000, "atr_20_percentage": 5.0},
        {"ticker": "BRK.A", "current_price": 600000.0, "asset_type": "common_stock", "market_cap": 900_000_000_000, "average_dollar_volume_20d": 300_000_000, "atr_20_percentage": 2.0},
        {"ticker": "NONE"},
    ]

    accepted, rejected = module._finalize_universe_filter(candidates)

    assert [item["ticker"] for item in accepted] == ["AAPL", "SOXS"]
    rejected_by_ticker = {item["ticker"]: item for item in rejected}
    assert "price_out_of_range" in rejected_by_ticker["BRK"]["rejection_reason"]
    assert "price_missing" in rejected_by_ticker["NONE"]["rejection_reason"]


def test_main_drops_out_of_band_tickers_before_writing_top_configs(monkeypatch):
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for folder in ("configs", "reports", "state", "logs", "runtime"):
            (tmpdir / folder).mkdir()
        (tmpdir / "configs" / "TOP3.yaml").write_text("ticker: OLD\nmode: paper\n", encoding="utf-8")

        from src.openalpha import config_writer
        from src.openalpha import selection_state

        original_base = config_writer.BASE
        captured_bundles: list[dict] = []
        original_bundle_writer = module.write_selection_bundle_atomic
        try:
            monkeypatch.setenv("SOXS_PROJECT_DIR", str(tmpdir))
            monkeypatch.setenv("SOXS_STATE_DIR", str(tmpdir / "state"))
            monkeypatch.setattr(selection_state, "PROJECT_DIR", tmpdir)
            config_writer.BASE = str(tmpdir)
            _patch_common(module, tmpdir)
            try:
                def _capture_bundle(**payload):
                    captured_bundles.append(dict(payload))
                    return original_bundle_writer(**payload)

                module.write_selection_bundle_atomic = _capture_bundle
                os.environ["OPENALPHA_RESTART_TOP"] = "0"
                os.environ["OPENALPHA_BACKGROUND_REFINEMENT"] = "0"
                try:
                    module.main()
                finally:
                    os.environ.pop("OPENALPHA_RESTART_TOP", None)
                    os.environ.pop("OPENALPHA_BACKGROUND_REFINEMENT", None)
            finally:
                config_writer.BASE = original_base
                module.write_selection_bundle_atomic = original_bundle_writer
        finally:
            config_writer.BASE = original_base
            module.write_selection_bundle_atomic = original_bundle_writer

        assert captured_bundles
        summary = captured_bundles[0]["summary"]
        assert summary["settings"]["min_price"] == 5.0
        assert summary["settings"]["max_price"] == 300.0
        assert summary["settings"]["price_band"] == {"min": 5.0, "max": 300.0}
        assert "universe_filter" in summary["settings"]
        assert summary["selection_count"] == 2
        assert summary["top_n_filled"] is False
        assert summary["quality_filter_report"]["removed_by_universe_filter"]
        rejected_tickers = {item["ticker"] for item in summary["quality_filter_report"]["removed_by_universe_filter"]}
        assert "BRK" in rejected_tickers
        assert "LOWVOL" not in rejected_tickers

        assert [item["ticker"] for item in captured_bundles[0]["top_items"]] == ["SOFI", "LOWVOL"]
        top1 = yaml.safe_load((tmpdir / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))
        top2 = yaml.safe_load((tmpdir / "configs" / "TOP2.yaml").read_text(encoding="utf-8"))
        top3 = yaml.safe_load((tmpdir / "configs" / "TOP3.yaml").read_text(encoding="utf-8"))
        assert top1["enabled"] is True
        assert top2["enabled"] is True
        assert top3["enabled"] is False
        assert top2.get("reason", "") in {"top_n_not_filled", "selection_blocked", ""}
        assert top3["reason"] in {"top_n_not_filled", "selection_blocked"}


def test_combined_dashboard_shows_universe_filter_when_report_missing_price_settings(monkeypatch):
    monkeypatch.setattr(combined, "_fetch_live_account_summary", lambda: None)
    monkeypatch.setattr(combined, "_fetch_status", lambda port: None)
    monkeypatch.setattr(combined, "_load_config_defaults", lambda name: {
        "ticker": name.replace(".yaml", ""),
        "initial_capital": 1000.0,
        "support": 100.0,
        "resistance": 110.0,
    })
    monkeypatch.setattr(combined, "summarize_trade_log", lambda *args, **kwargs: {
        "execution_mode": "paper",
        "reduce_only": False,
        "new_entries_allowed": True,
        "decision_count": 0,
        "execution_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "order_qty": 0,
        "tickers": [],
        "latest_line": "",
    })
    monkeypatch.setattr(combined, "_selection_sync_status", lambda: {
        "ok": True,
        "level": "green",
        "label": "已对齐",
        "detail": "当天配置已对齐（美东 2026-07-09）",
        "required_date": "2026-07-09",
        "state_date": "2026-07-09",
        "selection_state_symbols": ["SOFI"],
        "current_top_config_symbols": ["SOFI"],
    })
    monkeypatch.setattr(combined, "_load_ai_selection_report", lambda: {
        "timestamp": "2026-07-10T01:48:21.554602",
        "report": [],
        "top3": [{"ticker": "SOFI"}],
        "top10": [],
        "settings": {"entry_proximity_enabled": True, "entry_proximity_weight": 0.0},
        "protected_positions": [],
    })

    with combined.app.test_request_context("/"):
        html = combined.index()

    assert "Universe筛选：普通股 $5-$200 / ETF $5-$300 / 杠杆与反向ETF $5-$100" in html
    assert "价格范围：" not in html
