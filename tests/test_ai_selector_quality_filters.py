from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from src.ai_selector import selector as selector_module
from src.ai_selector.selector import AIStrategySelector, apply_quality_filters


class SimpleMonkeyPatch:
    def __init__(self):
        self._originals = []

    def setattr(self, obj, name, value):
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, original in reversed(self._originals):
            setattr(obj, name, original)


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
        class FakeFetcher:
            def __init__(self, ticker, poll_interval=0):
                self.ticker = ticker

            def get_quote(self):
                quote_map = {
                    "PASS": SimpleNamespace(price=20.0, bid=19.98, ask=20.02),
                    "LOWVOL": SimpleNamespace(price=20.0, bid=19.98, ask=20.02),
                    "WIDESPREAD": SimpleNamespace(price=20.0, bid=19.0, ask=20.2),
                    "MOMO": SimpleNamespace(price=20.0, bid=19.98, ask=20.02),
                    "SOFI": SimpleNamespace(price=18.0, bid=17.9, ask=18.1),
                }
                return quote_map[self.ticker]

            def get_ohlcv(self, period="1mo", interval="1d"):
                history_map = {
                    "PASS": [SimpleNamespace(close=v, volume=1_000_000) for v in [18, 18.5, 19, 19.4, 19.7, 19.8, 19.9, 20.0, 20.0, 20.0]],
                    "LOWVOL": [SimpleNamespace(close=v, volume=100_000) for v in [18, 18.5, 19, 19.4, 19.7, 19.8, 19.9, 20.0, 20.0, 20.0]],
                    "WIDESPREAD": [SimpleNamespace(close=v, volume=1_000_000) for v in [18, 18.5, 19, 19.4, 19.7, 19.8, 19.9, 20.0, 20.0, 20.0]],
                    "MOMO": [SimpleNamespace(close=v, volume=1_000_000) for v in [14, 15, 16, 17, 18, 19, 20, 21, 23, 25]],
                    "SOFI": [SimpleNamespace(close=v, volume=100_000) for v in [18, 18, 18, 18]],
                }
                return history_map[self.ticker]

        monkeypatch.setattr(selector_module, "PriceFetcher", FakeFetcher)
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


def test_selector_runs_quality_filters_before_final_top5_and_writes_log():
    monkeypatch = SimpleMonkeyPatch()
    try:
        class FakeFetcher:
            def __init__(self, ticker, poll_interval=0):
                self.ticker = ticker

            def get_quote(self):
                return {
                    "AAA": SimpleNamespace(price=20.0, bid=19.99, ask=20.01),
                    "BBB": SimpleNamespace(price=25.0, bid=24.99, ask=25.01),
                }[self.ticker]

            def get_ohlcv(self, period="1mo", interval="1d"):
                series = {
                    "AAA": [SimpleNamespace(close=v, volume=100_000) for v in [18, 18.5, 19, 19.2, 19.4, 19.6, 19.8, 19.9, 20.0, 20.0]],
                    "BBB": [SimpleNamespace(close=v, volume=2_000_000) for v in [22, 22.5, 23, 23.2, 23.4, 23.6, 24.0, 24.3, 24.6, 25.0]],
                }
                return series[self.ticker]

        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            monkeypatch.setattr(selector_module, "PriceFetcher", FakeFetcher)
            monkeypatch.setattr(selector_module, "LOG_DIR", log_dir)

            selector = AIStrategySelector()
            selector.universe._load_local_snapshot = lambda: ["AAA", "BBB"]
            selector.news.collect_for_symbols = lambda symbols: {symbol: [] for symbol in symbols}
            selector._score_with_live_flag = lambda symbols, news_map, live_enabled: [
                _candidate("AAA", 95.0),
                _candidate("BBB", 85.0),
            ]

            result = selector.run_selection(write_configs=False)

            assert [item["ticker"] for item in result["top5"]] == ["BBB"]
            log_path = log_dir / f"selection_{selector_module.datetime.now().strftime('%Y-%m-%d')}.log"
            assert log_path.exists()
            lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert lines[0]["summary"]["removed_by_volume_filter"] == 1
            assert lines[0]["summary"]["final_selected_symbols"] == ["BBB"]
    finally:
        monkeypatch.restore()


def run_test_direct():
    test_apply_quality_filters_removes_volume_spread_and_volatility_failures()
    test_selector_runs_quality_filters_before_final_top5_and_writes_log()


if __name__ == "__main__":
    run_test_direct()
