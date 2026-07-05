from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.ai_selector.config import AISelectorRuntimeConfig
from src.ai_selector.providers.openbb_provider import OpenBBProvider


class StubOpenBBClient:
    def __init__(
        self,
        fundamentals: dict | None = None,
        income_statement: dict | None = None,
        balance_sheet: dict | None = None,
        cash_flow: dict | None = None,
        analyst_estimates: dict | None = None,
    ) -> None:
        self._fundamentals = fundamentals or {}
        self._income_statement = income_statement or {}
        self._balance_sheet = balance_sheet or {}
        self._cash_flow = cash_flow or {}
        self._analyst_estimates = analyst_estimates or {}

    def get_fundamentals(self, ticker: str) -> dict:
        return dict(self._fundamentals)

    def get_income_statement(self, ticker: str) -> dict:
        return dict(self._income_statement)

    def get_balance_sheet(self, ticker: str) -> dict:
        return dict(self._balance_sheet)

    def get_cash_flow(self, ticker: str) -> dict:
        return dict(self._cash_flow)

    def get_analyst_estimates(self, ticker: str) -> dict:
        return dict(self._analyst_estimates)


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = []

    def setattr(self, obj, name, value):
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, original in reversed(self._originals):
            setattr(obj, name, original)


def _config() -> AISelectorRuntimeConfig:
    return AISelectorRuntimeConfig(
        enabled=True,
        top_n=3,
        universe=["NVDA"],
        top10_path=Path(tempfile.gettempdir()) / "openbb_top10.json",
        tradingagents_path="",
        tradingagents_python="python3",
        tradingagents_analysis_date=None,
        finrobot_path="",
        finrobot_python="python3",
        finrobot_config_file="",
        finrobot_output_dir="",
        openbb_enabled=True,
    )


def test_openbb_provider_without_install_does_not_crash():
    monkeypatch = SimpleMonkeyPatch()
    try:
        provider = OpenBBProvider(config=_config())
        monkeypatch.setattr(provider, "_is_available", lambda: False)

        result = provider.analyze(["NVDA"])

        assert "NVDA" in result
        assert result["NVDA"]["fallback"] is True
        assert result["NVDA"]["fundamental_score"] == 50.0
        assert result["NVDA"]["risk_score"] == 50.0
    finally:
        monkeypatch.restore()


def test_openbb_provider_analyze_returns_dict():
    provider = OpenBBProvider(
        config=_config(),
        client=StubOpenBBClient(
            fundamentals={"revenueGrowth": 0.22, "profitMargins": 0.31, "currentRatio": 1.8, "debtToEquity": 0.45, "forwardPE": 24},
            income_statement={"epsGrowth": 0.18, "operatingMargins": 0.28},
            balance_sheet={"quickRatio": 1.5},
            cash_flow={"freeCashFlowGrowth": 0.16},
            analyst_estimates={"pegRatio": 1.4},
        ),
    )

    result = provider.analyze(["NVDA"])

    assert isinstance(result, dict)
    assert "NVDA" in result
    assert result["NVDA"]["fallback"] is False
    assert "fundamental_score" in result["NVDA"]
    assert "risk_score" in result["NVDA"]
    assert result["NVDA"]["reason"]


def test_openbb_provider_missing_data_uses_neutral_scores():
    provider = OpenBBProvider(
        config=_config(),
        client=StubOpenBBClient(),
    )

    result = provider.analyze(["NVDA"])

    assert result["NVDA"]["fundamental_score"] == 50.0
    assert result["NVDA"]["valuation_score"] == 50.0
    assert result["NVDA"]["risk_score"] == 50.0


def test_openbb_provider_output_contains_required_fields():
    provider = OpenBBProvider(
        config=_config(),
        client=StubOpenBBClient(
            fundamentals={"revenueGrowth": 0.12},
        ),
    )

    result = provider.analyze(["NVDA"])
    payload = result["NVDA"]

    assert "fundamental_score" in payload
    assert "risk_score" in payload
    assert "reason" in payload


def run_test_direct():
    test_openbb_provider_without_install_does_not_crash()
    test_openbb_provider_analyze_returns_dict()
    test_openbb_provider_missing_data_uses_neutral_scores()
    test_openbb_provider_output_contains_required_fields()
