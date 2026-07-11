from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from src.research_report.generator import generate_daily_research_report
from src.research_report.site import build_research_site


@dataclass
class FakeQuote:
    price: float
    bid: float = 0.0
    ask: float = 0.0


@dataclass
class FakeCandle:
    close: float
    high: float
    low: float
    volume: int


class FakeFetcher:
    def __init__(self, ticker: str):
        self.ticker = ticker

    def get_quote(self):
        mapping = {
            "SOFI": FakeQuote(17.31, 17.30, 17.32),
            "LABD": FakeQuote(33.12, 33.10, 33.14),
            "F": FakeQuote(10.24, 10.23, 10.25),
        }
        return mapping.get(self.ticker, FakeQuote(12.34, 12.33, 12.35))

    def get_ohlcv(self, period: str = "1mo", interval: str = "1d"):
        base = {
            "SOFI": [17.0, 17.1, 17.2, 17.25, 17.31],
            "LABD": [31.0, 31.5, 32.1, 32.8, 33.12],
            "F": [9.9, 10.0, 10.1, 10.15, 10.24],
        }.get(self.ticker, [12.0, 12.1, 12.2, 12.3, 12.34])
        return [
            FakeCandle(close=value, high=value * 1.01, low=value * 0.99, volume=1000000 + idx * 1000)
            for idx, value in enumerate(base)
        ]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_project_fixture(root: Path) -> None:
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "state" / "broker_cache").mkdir(parents=True, exist_ok=True)
    (root / "state" / "order_state").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)

    _write_text(
        root / "reports" / "ai_selection_latest.json",
        json.dumps(
            {
                "selection_date": "2026-07-09",
                "fallback_used": True,
                "provider_fallback_used": True,
                "settings": {
                    "min_price": 4.0,
                    "max_price": 50.0,
                    "price_band": {"min": 4.0, "max": 50.0},
                    "entry_proximity_enabled": True,
                    "entry_proximity_weight": 0.0,
                },
                "top3": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write_text(
        root / "state" / "ai_selection_state.json",
        json.dumps(
            {
                "et_date": "2026-07-09",
                "selected_symbols": ["SOFI", "LABD", "F"],
                "top_config_symbols": ["SOFI", "LABD", "F"],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    for idx, ticker in enumerate(["SOFI", "LABD", "F"], start=1):
        _write_text(
            root / "configs" / f"TOP{idx}.yaml",
            yaml.safe_dump(
                {
                    "ticker": ticker,
                    "mode": "paper",
                    "selection": {
                        "selection_date": "2026-07-09",
                        "ai_score": 60 + idx,
                        "range_score": 70 + idx,
                        "final_score": 65 + idx,
                        "confidence": 0.8,
                        "trade_filter_passed": True,
                        "fallback_used": idx == 1,
                        "leveraged_etf": ticker in {"LABD"},
                        "composition_filter_passed": True,
                        "composition_reject_reason": "",
                        "final_rank": idx,
                        "entry": {
                            "entry_proximity_score": 30 if ticker != "F" else 55,
                            "good_for_entry_now": ticker == "SOFI",
                            "entry_quality": "poor" if ticker != "F" else "neutral",
                            "entry_reason": "test",
                            "range_position": 70 if ticker != "F" else 50,
                            "dist_to_support": 20 if ticker != "F" else 5,
                            "dist_to_resistance": 8 if ticker != "F" else 4,
                        },
                    },
                    "allocation": {"target_capital": 300.0, "target_shares": 10, "weight": 0.3, "atr_pct": 0.05, "risk_pct": 1.0, "reason": "test"},
                    "portfolio": {"enabled": True, "max_positions": 3, "max_total_exposure": 1.0, "max_total_risk": 0.05, "leveraged_etf_max_single_position": 0.15, "leveraged_etf_max_group_exposure": 0.5},
                    "ai_selector": {"allow_fallback_paper_entries": True, "allow_fallback_live_entries": False, "fallback_paper_position_multiplier": 0.25, "entry_proximity_enabled": True, "entry_proximity_weight": 0.0},
                },
                sort_keys=False,
            ),
        )

    _write_text(
        root / "state" / "broker_cache" / "longbridge_account.json",
        json.dumps(
            {
                "fetched_at": 1234.0,
                "payload": {
                    "cash": 700.0,
                    "equity": 2100.0,
                    "buying_power": 900.0,
                    "positions": [
                        {"ticker": "SOFI", "quantity": 46, "avg_entry_price": 17.23, "current_price": 17.31, "market_value": 796.0, "unrealized_pnl": 3.68, "unrealized_pnl_pct": 0.46}
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write_text(
        root / "state" / "broker_cache" / "longbridge_positions.json",
        json.dumps(
            {
                "fetched_at": 1234.0,
                "payload": {
                    "positions": [
                        {"ticker": "SOFI", "quantity": 46, "avg_entry_price": 17.23, "current_price": 17.31, "market_value": 796.0, "unrealized_pnl": 3.68, "unrealized_pnl_pct": 0.46}
                    ]
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write_text(
        root / "state" / "order_state" / "YINN.json",
        json.dumps(
            {
                "ticker": "YINN",
                "updated_at": "2026-07-09T11:10:23.786345",
                "blocked": {"blocked_until": "2026-07-09T11:10:25", "reason": "The order amount exceeds the maximum buying power", "buying_power_at_block": 329.72},
                "failed_orders_today": [
                    {"ticker": "YINN", "timestamp": "2026-07-09T11:09:50.053831", "reason": "buying power", "quantity": 0, "price": 0.0, "buying_power": 100.0},
                    {"ticker": "SOFI", "timestamp": "2026-07-09T14:10:00.053831", "reason": "timeout", "quantity": 0, "price": 0.0, "buying_power": 100.0},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write_text(
        root / "logs" / "trades-20260709.jsonl",
        "\n".join(
            [
                json.dumps(
                    {
                        "phase": "decision",
                        "ticker": "F",
                        "execution_mode": "paper",
                        "signal": "HOLD",
                        "reduce_only": False,
                    }
                ),
                json.dumps(
                    {
                        "phase": "risk_decision",
                        "ticker": "YINN",
                        "execution_mode": "paper",
                        "signal": "BUY",
                        "fallback_used": True,
                        "allow_fallback_paper_entries": False,
                        "risk_approved": False,
                        "blocked_by": "ai_entry_gate",
                        "reason": "fallback_used_blocked",
                        "original_target_shares": 42,
                        "adjusted_target_shares": 0,
                        "position_multiplier": 0.25,
                        "current_price": 25.07,
                        "buying_power": 1400.0,
                        "available_cash": 210.0,
                        "required_cash": 209.16,
                        "portfolio_guard_enabled": True,
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": False,
                        "order_state_reason": "",
                        "final_action": "blocked",
                    }
                ),
                json.dumps(
                    {
                        "phase": "risk_decision",
                        "ticker": "SOFI",
                        "execution_mode": "paper",
                        "signal": "BUY",
                        "fallback_used": False,
                        "allow_fallback_paper_entries": True,
                        "risk_approved": False,
                        "blocked_by": "buying_power",
                        "reason": "insufficient buying power",
                        "original_target_shares": 12,
                        "adjusted_target_shares": 0,
                        "position_multiplier": 0.25,
                        "current_price": 18.05,
                        "buying_power": 100.0,
                        "available_cash": 20.0,
                        "required_cash": 216.6,
                        "portfolio_guard_enabled": True,
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": False,
                        "order_state_reason": "",
                        "final_action": "blocked",
                    }
                ),
                json.dumps(
                    {
                        "phase": "execution",
                        "ticker": "SOFI",
                        "execution_mode": "paper",
                        "order": {"side": "buy", "qty": 10},
                        "response": {"status": "submitted"},
                    }
                ),
            ]
        ),
    )


def test_generate_daily_research_report_writes_html_md_json(tmp_path):
    project_dir = tmp_path
    _write_project_fixture(project_dir)

    report = generate_daily_research_report(
        date(2026, 7, 9),
        project_dir=project_dir,
        reports_dir=project_dir / "reports" / "research",
        fetcher_factory=lambda ticker: FakeFetcher(ticker),
    )

    json_path = project_dir / "reports" / "research" / "daily-paper-report-2026-07-09.json"
    md_path = project_dir / "reports" / "research" / "daily-paper-report-2026-07-09.md"
    html_path = project_dir / "reports" / "research" / "daily-paper-report-2026-07-09.html"

    assert json_path.exists()
    assert md_path.exists()
    assert html_path.exists()
    assert report["date"] == "2026-07-09"
    assert report["selection_sync"]["ok"] is True
    assert [item["ticker"] for item in report["top_cards"]] == ["SOFI", "LABD", "F"]
    assert report["quality"]["entry_ready_symbols"] == ["SOFI"]
    assert report["quality"]["observation_only_symbols"] == ["LABD", "F"]
    assert report["quality"]["top_quality_rows"][0]["entry_quality"] == "poor"
    assert report["quality"]["top_quality_rows"][1]["entry_quality"] == "poor"
    assert report["quality"]["top_quality_rows"][2]["entry_quality"] == "neutral"
    assert report["strategy_review"]["success_count"] == 1
    assert report["strategy_review"]["observation_correct_count"] == 2
    assert report["strategy_review"]["failure_count"] == 0
    assert report["strategy_review"]["rows"][0]["review_result"] == "选股成功"
    assert report["strategy_review"]["rows"][1]["review_result"] == "观察正确"
    assert report["strategy_review"]["rows"][2]["review_result"] == "观察正确"
    assert report["decision_summary"]["buy_blocked_count"] == 2
    assert report["decision_summary"]["risk_block_reason_counts"]["fallback_used_blocked"] == 1
    assert report["decision_summary"]["risk_block_reason_counts"]["insufficient buying power"] == 1
    assert report["trade_activity"]["summary"]["execution_count"] >= 1
    assert any(row["ticker"] == "YINN" for row in report["order_state"]["historical"])
    assert report["market_snapshots"]["SOFI"]["available"] is True
    assert "每日研究报告 2026-07-09" in md_path.read_text(encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8")
    assert "研究报告首页" not in html_text
    assert "TOP 质量总结" in html_text
    assert "策略评分复盘" in html_text
    assert "无交易 / 风控拦截统计" in html_text


def test_generate_daily_research_report_handles_missing_inputs(tmp_path):
    project_dir = tmp_path
    (project_dir / "reports").mkdir(parents=True, exist_ok=True)
    (project_dir / "configs").mkdir(parents=True, exist_ok=True)
    (project_dir / "state" / "broker_cache").mkdir(parents=True, exist_ok=True)
    (project_dir / "state" / "order_state").mkdir(parents=True, exist_ok=True)
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)

    report = generate_daily_research_report(
        date(2026, 7, 10),
        project_dir=project_dir,
        reports_dir=project_dir / "reports" / "research",
        fetcher_factory=lambda ticker: FakeFetcher(ticker),
    )
    assert report["date"] == "2026-07-10"
    assert report["selection_sync"]["ok"] is False
    assert report["top_cards"] == []
    assert report["positions"] == []
    assert report["trade_activity"]["summary"]["execution_count"] == 0


def test_build_research_site_writes_index(tmp_path):
    project_dir = tmp_path
    reports_dir = project_dir / "reports" / "research"
    reports_dir.mkdir(parents=True, exist_ok=True)
    site_dir = project_dir / "site" / "research"
    site_dir.mkdir(parents=True, exist_ok=True)
    for day, tops, fallback in [
        ("2026-07-08", ["SOFI", "LABD"], False),
        ("2026-07-09", ["F", "SOFI", "DRIP"], True),
    ]:
        _write_text(
            reports_dir / f"daily-paper-report-{day}.json",
            json.dumps(
                    {
                        "date": day,
                        "generated_at": f"{day}T18:00:00-04:00",
                        "mode": "paper",
                        "top_cards": [{"ticker": ticker, "entry_ready": ticker == tops[0]} for ticker in tops],
                    "quality": {
                        "entry_ready_count": 1,
                        "observation_only_count": max(0, len(tops) - 1),
                        "fallback_used": fallback,
                        "provider_fallback_used": fallback,
                        "price_band": {"min": 4.0, "max": 50.0},
                        },
                        "strategy_review": {
                            "success_count": 1 if fallback else 0,
                            "observation_correct_count": max(0, len(tops) - 1),
                            "failure_count": 0 if fallback else 1,
                            "rows": [],
                        },
                        "selection_sync": {"ok": True, "reason": "ok"},
                        "trade_activity": {"summary": {"execution_count": 2, "buy_count": 1, "sell_count": 1}},
                        "warnings": ["sample warning"] if fallback else [],
                    },
                ensure_ascii=False,
                indent=2,
            ),
        )
    index_path = build_research_site(project_dir=project_dir, reports_dir=reports_dir, site_dir=site_dir)
    text = index_path.read_text(encoding="utf-8")
    assert index_path.exists()
    assert "每日简报列表" in text
    assert "近日报告" in text
    assert "2026-07-09" in text
    assert "2026-07-08" in text
    assert "F / SOFI / DRIP" in text
    assert "SOFI / LABD" in text
    assert "策略复盘" in text
