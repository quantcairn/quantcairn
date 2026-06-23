import pandas as pd
import pytest

import datetime

from src.data.fetcher import PriceFetcher


class DummyTicker:
    def __init__(self, ticker):
        self.ticker = ticker

    def history(self, period, interval):
        idx = pd.date_range(end=pd.Timestamp.now(), periods=1, freq='1min')
        df = pd.DataFrame({'Close': [10.0], 'Volume': [100], 'High': [10.1], 'Low': [9.9]}, index=idx)
        return df

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
    assert q.price == pytest.approx(10.0)
    assert q.volume == 100
