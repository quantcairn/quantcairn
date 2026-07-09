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


def _patch_common(module, tmpdir: Path):
    module.PROJECT_DIR = tmpdir
    module.REPORTS_DIR = tmpdir / "reports"
    module.load_local_ai_env = lambda: None
    module.load_runtime_settings = lambda: {
        "min_price": 4.0,
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
            "run_selection": lambda self, write_configs=True, symbols_override=None: {
                "top10": [
                    {"ticker": "BAC", "score": 80.0, "range_low": 56.0, "range_high": 62.0, "risk": {"stop_loss_pct": 1.5}, "size": 1, "confidence": 0.8, "reason": "stub", "source": "stub"},
                    {"ticker": "SOFI", "score": 70.0, "range_low": 12.0, "range_high": 15.0, "risk": {"stop_loss_pct": 1.5}, "size": 1, "confidence": 0.7, "reason": "stub", "source": "stub"},
                    {"ticker": "INTC", "score": 60.0, "range_low": 100.0, "range_high": 120.0, "risk": {"stop_loss_pct": 1.5}, "size": 1, "confidence": 0.6, "reason": "stub", "source": "stub"},
                ],
                "top5": [
                    {"ticker": "BAC", "score": 80.0, "range_low": 56.0, "range_high": 62.0, "risk": {"stop_loss_pct": 1.5}, "size": 1, "confidence": 0.8, "reason": "stub", "source": "stub"},
                    {"ticker": "SOFI", "score": 70.0, "range_low": 12.0, "range_high": 15.0, "risk": {"stop_loss_pct": 1.5}, "size": 1, "confidence": 0.7, "reason": "stub", "source": "stub"},
                    {"ticker": "INTC", "score": 60.0, "range_low": 100.0, "range_high": 120.0, "risk": {"stop_loss_pct": 1.5}, "size": 1, "confidence": 0.6, "reason": "stub", "source": "stub"},
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
    module._build_report_top10 = lambda selector_top10, selected, signal_map, live_positions: list(selector_top10 or selected)
    module._prioritize_ai_rank = lambda rows, signal_map: list(rows)
    module._split_selected_and_protected_positions = lambda candidates, positions, limit=3: (list(candidates)[:limit], [])
    module._live_candidate_price = lambda ticker: {
        "BAC": 59.285,
        "SOFI": 13.72,
        "INTC": 112.97,
    }.get(str(ticker).upper())


def test_price_band_final_filter_rejects_out_of_range_candidates():
    module = _load_module()
    candidates = [
        {"ticker": "BAC", "current_price": 59.285},
        {"ticker": "SOFI", "current_price": 13.72},
        {"ticker": "INTC", "current_price": 112.97},
        {"ticker": "NONE"},
    ]

    accepted, rejected = module._finalize_price_band(candidates, 4.0, 50.0)

    assert [item["ticker"] for item in accepted] == ["SOFI"]
    assert {item["ticker"] for item in rejected} == {"BAC", "INTC", "NONE"}
    assert rejected[0]["min_price"] == 4.0
    assert rejected[0]["max_price"] == 50.0
    assert any(item["reason"] == "price_missing" for item in rejected)


def test_main_drops_out_of_band_tickers_before_writing_top_configs():
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for folder in ("configs", "reports", "state", "logs", "runtime"):
            (tmpdir / folder).mkdir()
        (tmpdir / "configs" / "TOP3.yaml").write_text("ticker: OLD\nmode: paper\n", encoding="utf-8")

        from src.ai_selector import config_writer

        original_base = config_writer.BASE
        written_reports: list[dict] = []
        try:
            config_writer.BASE = str(tmpdir)
            _patch_common(module, tmpdir)
            module._write_reports = lambda summary: (
                written_reports.append(json.loads(json.dumps(summary))) or True
            ) and (tmpdir / "reports" / "latest.json", tmpdir / "reports" / "dated.json")
            os.environ["AI_SELECTOR_RESTART_TOP"] = "0"
            os.environ["AI_SELECTOR_BACKGROUND_REFINEMENT"] = "0"
            try:
                module.main()
            finally:
                os.environ.pop("AI_SELECTOR_RESTART_TOP", None)
                os.environ.pop("AI_SELECTOR_BACKGROUND_REFINEMENT", None)
        finally:
            config_writer.BASE = original_base

        assert written_reports
        summary = written_reports[0]
        assert summary["settings"]["min_price"] == 4.0
        assert summary["settings"]["max_price"] == 50.0
        assert summary["settings"]["price_band"] == {"min": 4.0, "max": 50.0}
        assert summary["selection_count"] == 1
        assert summary["top_n_filled"] is False
        assert summary["quality_filter_report"]["removed_out_of_price_band"]
        rejected_tickers = {item["ticker"] for item in summary["quality_filter_report"]["removed_out_of_price_band"]}
        assert "BAC" in rejected_tickers
        assert "INTC" in rejected_tickers

        top1 = yaml.safe_load((tmpdir / "configs" / "TOP1.yaml").read_text(encoding="utf-8"))
        assert top1["ticker"] == "SOFI"
        assert top1["selection"]["selection_date"] == summary["selection_date"]
        assert top1["selection"]["trade_filter_passed"] in {True, False}
        assert top1["selection"]["reject_reason"] == ""
        assert top1["selection"]["fallback_used"] is False
        assert not (tmpdir / "configs" / "TOP2.yaml").exists()
        assert not (tmpdir / "configs" / "TOP3.yaml").exists()


def test_combined_dashboard_defaults_price_band_display_when_report_missing_settings(monkeypatch):
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

    assert "价格范围：$4.00 - $50.00 (default)" in html
