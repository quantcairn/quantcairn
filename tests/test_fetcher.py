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

    def history(self, period, interval, prepost=False):
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


def test_get_quote_from_history(monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit('.', 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    monkeypatch.setattr('yfinance.Ticker', DummyTicker)
    pf = PriceFetcher('FOO')
    q = pf.get_quote()
    assert q is not None
    assert abs(q.price - 10.0) < 1e-6
    assert q.volume == 100


def test_validate_live_mode_requires_longbridge_credentials():
    from src.config.loader import AppConfig, BrokerConfig, LongBridgeConfig, RangeConfig, validate_config

    config = AppConfig(
        mode='live',
        range=RangeConfig(mode='auto'),
        broker=BrokerConfig(longbridge=LongBridgeConfig(enabled=True)),
    )
    issues = validate_config(config)
    assert any('requires longbridge app_key' in issue for issue in issues)
    assert any('requires longbridge app_secret' in issue for issue in issues)
    assert any('requires longbridge access_token' in issue for issue in issues)


def run_test_direct():
    test_get_quote_from_history()
    test_validate_live_mode_requires_longbridge_credentials()


if __name__ == '__main__':
    run_test_direct()
