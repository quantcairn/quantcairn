import pytest

from src.data.fetcher import PriceFetcher


class DummyHist:
    empty = False

    def __init__(self):
        self._row = {"Close": 10.0, "Volume": 100, "High": 10.1, "Low": 9.9}

    @property
    def iloc(self):
        class _I:
            def __init__(self, row):
                self._row = row

            def __getitem__(self, idx):
                return self._row

        return _I(self._row)


class DummyTicker:
    def __init__(self, ticker):
        self.ticker = ticker

    def history(self, period, interval):
        return DummyHist()

    @property
    def fast_info(self):
        return {
            'regularMarketPreviousClose': 10.0,
            'bid': 9.8,
            'ask': 10.2,
            'lastPrice': 10.0,
            'lastVolume': 100,
        }

    @property
    def info(self):
        return {}


def test_get_quote_from_history(monkeypatch):
    monkeypatch.setattr('yfinance.Ticker', DummyTicker)
    pf = PriceFetcher('FOO')
    q = pf.get_quote()
    assert q is not None
    assert abs(q.price - 10.0) < 1e-6
    assert q.volume == 100


if __name__ == '__main__':
    # allow running this test directly without pytest
    test_get_quote_from_history(__import__('types').SimpleNamespace(setattr=lambda *a, **k: None))
