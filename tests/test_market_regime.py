from __future__ import annotations

from src.market_regime.detector import compute_market_features, detect_market_regime


class FakeSeries(list):
    def tolist(self):
        return list(self)


class FakeFrame:
    def __init__(self, rows):
        self.rows = list(rows)
        self.columns = list(self.rows[0].keys()) if self.rows else []
        self.attrs = {}
        self._data = {column: FakeSeries([row[column] for row in self.rows]) for column in self.columns}

    def __getitem__(self, key):
        return self._data[key]


def _frame(rows):
    return FakeFrame(rows)


def _flat_range_df():
    rows = []
    for idx in range(60):
        close = 100.0 + ((idx % 4) - 1.5) * 0.1
        rows.append(
            {
                "open": close + 0.02,
                "high": close + 0.25,
                "low": close - 0.25,
                "close": close,
                "volume": 1_000_000 + (idx % 3) * 2_000,
            }
        )
    return _frame(rows)


def _trend_df():
    rows = []
    for idx in range(60):
        close = 50.0 + idx * 1.2 + (idx % 5) * 0.8
        rows.append(
            {
                "open": close - 0.3,
                "high": close + 1.4,
                "low": close - 1.4,
                "close": close,
                "volume": 900_000 + idx * 25_000,
            }
        )
    return _frame(rows)


def _event_df():
    rows = []
    for idx in range(60):
        close = 100.0 + ((idx % 4) - 1.5) * 0.1
        rows.append(
            {
                "open": close + 0.02,
                "high": close + 0.25,
                "low": close - 0.25,
                "close": close,
                "volume": 1_000_000 + (idx % 3) * 2_000,
            }
        )
    rows[59]["open"] = rows[58]["close"] * 1.08
    rows[59]["high"] = rows[59]["open"] * 1.01
    rows[59]["low"] = rows[59]["open"] * 0.99
    rows[59]["close"] = rows[59]["open"] * 1.02
    rows[59]["volume"] = sum(row["volume"] for row in rows) / len(rows) * 4.5
    frame = _frame(rows)
    frame.attrs["news_score"] = 90
    return frame


def test_detect_market_regime_range():
    df = _flat_range_df()
    assert detect_market_regime(df) == "RANGE"


def test_detect_market_regime_trend():
    df = _trend_df()
    assert detect_market_regime(df) == "TREND"


def test_detect_market_regime_event_from_volume_and_news():
    df = _event_df()
    assert detect_market_regime(df) == "EVENT"


def test_compute_market_features_contains_expected_values():
    df = _flat_range_df()
    features = compute_market_features(df)
    assert features.close > 0
    assert features.atr >= 0
    assert 0 <= features.rsi <= 100
