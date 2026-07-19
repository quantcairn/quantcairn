from pathlib import Path

from src.data.fetcher import PriceFetcher
import os
import src.data.fetcher as fetcher_mod


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


def test_price_fetcher_normalizes_us_suffix_for_provider_calls(monkeypatch=None):
    captured: list[str] = []

    class RecordingTicker(DummyTicker):
        def __init__(self, ticker):
            captured.append(ticker)
            super().__init__(ticker)

    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit('.', 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    monkeypatch.setattr('yfinance.Ticker', RecordingTicker)
    pf = PriceFetcher('SOFI.US')
    assert captured[0] == 'SOFI'
    assert pf._provider_ticker == 'SOFI'


def test_get_quote_from_history(monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit('.', 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    monkeypatch.setattr('yfinance.Ticker', DummyTicker)
    original_fetch_chart_quote = fetcher_mod.PriceFetcher._fetch_chart_quote
    fetcher_mod.PriceFetcher._fetch_chart_quote = lambda self: {}
    pf = PriceFetcher('FOO')
    try:
        q = pf.get_quote()
        assert q is not None
        assert abs(q.price - 10.0) < 1e-6
        assert q.volume == 100
    finally:
        fetcher_mod.PriceFetcher._fetch_chart_quote = original_fetch_chart_quote


def test_get_quote_handles_none_chart_payload_without_attribute_error(monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit('.', 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    monkeypatch.setattr('yfinance.Ticker', DummyTicker)
    original_fetch_chart_quote = fetcher_mod.PriceFetcher._fetch_chart_quote
    original_get_safe_fast_info = fetcher_mod.PriceFetcher._get_safe_fast_info
    original_fetch_history = fetcher_mod.PriceFetcher._fetch_history
    fetcher_mod.PriceFetcher._fetch_chart_quote = lambda self: None
    fetcher_mod.PriceFetcher._get_safe_fast_info = lambda self: {}
    fetcher_mod.PriceFetcher._fetch_history = lambda self, period, interval, prepost=True: None
    pf = PriceFetcher('FOO')
    try:
        quote = pf.get_quote()
    finally:
        fetcher_mod.PriceFetcher._fetch_chart_quote = original_fetch_chart_quote
        fetcher_mod.PriceFetcher._get_safe_fast_info = original_get_safe_fast_info
        fetcher_mod.PriceFetcher._fetch_history = original_fetch_history

    assert quote is None or getattr(quote, "price", 0) >= 0


def test_fetch_chart_quote_marks_empty_response_for_none_json():
    original_session = fetcher_mod.requests.Session

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return None

    class DummySession:
        def __init__(self):
            self.trust_env = False

        def get(self, *args, **kwargs):
            return DummyResponse()

    try:
        fetcher_mod.requests.Session = DummySession
        pf = PriceFetcher("MSFT")
        quote = pf._fetch_chart_quote()
    finally:
        fetcher_mod.requests.Session = original_session

    assert quote == {}
    assert pf._last_quote_fetch_status == "EMPTY_RESPONSE"
    assert pf._last_quote_error_code == "EMPTY_JSON"


def test_fetch_chart_quote_marks_empty_response_for_empty_dict():
    original_session = fetcher_mod.requests.Session

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class DummySession:
        def __init__(self):
            self.trust_env = False

        def get(self, *args, **kwargs):
            return DummyResponse()

    try:
        fetcher_mod.requests.Session = DummySession
        pf = PriceFetcher("MSFT")
        quote = pf._fetch_chart_quote()
    finally:
        fetcher_mod.requests.Session = original_session

    assert quote == {}
    assert pf._last_quote_fetch_status == "EMPTY_RESPONSE"
    assert pf._last_quote_error_code == "MISSING_CHART"


def test_fetch_chart_history_marks_empty_response_for_none_json():
    original_session = fetcher_mod.requests.Session

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return None

    class DummySession:
        def __init__(self):
            self.trust_env = False

        def get(self, *args, **kwargs):
            return DummyResponse()

    try:
        fetcher_mod.requests.Session = DummySession
        pf = PriceFetcher("MSFT")
        candles = pf._fetch_chart_history("1mo", "1d")
    finally:
        fetcher_mod.requests.Session = original_session

    assert candles == []
    assert pf._last_history_fetch_status == "EMPTY_RESPONSE"
    assert pf._last_history_error_code == "EMPTY_JSON"


def test_direct_yahoo_sessions_are_closed_on_success_and_error(monkeypatch):
    sessions = []

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "chart": {
                    "result": [{
                        "meta": {"regularMarketPrice": 10.0},
                        "timestamp": [1700000000],
                        "indicators": {"quote": [{
                            "open": [10.0], "high": [10.1], "low": [9.9],
                            "close": [10.0], "volume": [1000],
                        }]},
                    }]
                }
            }

    class TrackingSession:
        def __init__(self):
            self.trust_env = False
            self.closed = False
            sessions.append(self)

        def get(self, *args, **kwargs):
            return DummyResponse()

        def close(self):
            self.closed = True

    monkeypatch.setattr(fetcher_mod.requests, "Session", TrackingSession)
    fetcher = PriceFetcher("SOFI.US")

    assert fetcher._fetch_chart_quote()["status"] == "COMPLETE"
    assert fetcher._fetch_chart_history("1mo", "1d")
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)


def test_direct_yahoo_session_is_closed_after_request_failure(monkeypatch):
    sessions = []

    class FailingSession:
        def __init__(self):
            self.trust_env = False
            self.closed = False
            sessions.append(self)

        def get(self, *args, **kwargs):
            raise OSError("network unavailable")

        def close(self):
            self.closed = True

    monkeypatch.setattr(fetcher_mod.requests, "Session", FailingSession)
    monkeypatch.setattr(fetcher_mod.time, "sleep", lambda _seconds: None)
    fetcher = PriceFetcher("SOFI.US")

    assert fetcher._fetch_chart_quote() == {}
    assert sessions[0].closed is True


def test_price_fetcher_uses_absolute_yfinance_cache_dir(monkeypatch, tmp_path: Path):
    cache_dir = tmp_path / "state" / "yfinance_cache"
    recorded: dict[str, str] = {}

    def _record_cache_location(location):
        recorded["location"] = location

    monkeypatch.setattr('yfinance.Ticker', DummyTicker)
    monkeypatch.setattr(fetcher_mod, "_YFINANCE_CACHE_INITIALIZED", False)
    monkeypatch.setattr(fetcher_mod, "_YFINANCE_CACHE_ERROR", None)
    monkeypatch.setattr(fetcher_mod, "DEFAULT_YFINANCE_CACHE_DIR", cache_dir)
    import yfinance.cache as yf_cache

    original_set_cache_location = yf_cache.set_cache_location
    yf_cache.set_cache_location = _record_cache_location
    try:
        pf = PriceFetcher("SOFI.US")
    finally:
        yf_cache.set_cache_location = original_set_cache_location

    assert cache_dir.is_dir()
    assert recorded["location"] == str(cache_dir.resolve())
    assert pf._cache_status == "COMPLETE"


def test_get_ohlcv_handles_empty_dataframe_without_attribute_error(monkeypatch=None):
    if monkeypatch is None:
        class SimpleMonkeyPatch:
            def setattr(self, target, value):
                module_name, attr_name = target.rsplit('.', 1)
                module = __import__(module_name, fromlist=[attr_name])
                setattr(module, attr_name, value)

        monkeypatch = SimpleMonkeyPatch()

    class EmptyHist:
        empty = True

    monkeypatch.setattr('yfinance.Ticker', DummyTicker)
    original_fetch_chart_history = fetcher_mod.PriceFetcher._fetch_chart_history
    original_fetch_history = fetcher_mod.PriceFetcher._fetch_history
    fetcher_mod.PriceFetcher._fetch_chart_history = lambda self, period, interval: []
    fetcher_mod.PriceFetcher._fetch_history = lambda self, period, interval, prepost=True: EmptyHist()
    pf = PriceFetcher("FOO")
    try:
        candles = pf.get_ohlcv(period="1mo", interval="1d")
    finally:
        fetcher_mod.PriceFetcher._fetch_chart_history = original_fetch_chart_history
        fetcher_mod.PriceFetcher._fetch_history = original_fetch_history

    assert candles == []
    assert pf._last_history_fetch_status == "EMPTY_RESPONSE"
    assert pf._last_history_error_code == "NO_HISTORY"


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


def test_synthetic_market_fallback(monkeypatch=None):
    env_keys = {
        "SOXS_SYNTHETIC_MARKET": os.environ.get("SOXS_SYNTHETIC_MARKET"),
        "SOXS_SYNTHETIC_START_PRICE": os.environ.get("SOXS_SYNTHETIC_START_PRICE"),
        "SOXS_SYNTHETIC_AMPLITUDE_PCT": os.environ.get("SOXS_SYNTHETIC_AMPLITUDE_PCT"),
        "SOXS_SYNTHETIC_PERIOD_SECONDS": os.environ.get("SOXS_SYNTHETIC_PERIOD_SECONDS"),
    }
    originals = {
        "_fetch_chart_quote": fetcher_mod.PriceFetcher._fetch_chart_quote,
        "_get_safe_fast_info": fetcher_mod.PriceFetcher._get_safe_fast_info,
        "_fetch_history": fetcher_mod.PriceFetcher._fetch_history,
    }

    try:
        os.environ["SOXS_SYNTHETIC_MARKET"] = "1"
        os.environ["SOXS_SYNTHETIC_START_PRICE"] = "123.45"
        os.environ["SOXS_SYNTHETIC_AMPLITUDE_PCT"] = "10"
        os.environ["SOXS_SYNTHETIC_PERIOD_SECONDS"] = "30"

        fetcher_mod.PriceFetcher._fetch_chart_quote = lambda self: {}
        fetcher_mod.PriceFetcher._get_safe_fast_info = lambda self: {}
        fetcher_mod.PriceFetcher._fetch_history = lambda self, period, interval, prepost=True: None

        pf = PriceFetcher("TOP1")
        quote = pf.get_quote()
        assert quote is not None
        assert quote.price > 0
    finally:
        fetcher_mod.PriceFetcher._fetch_chart_quote = originals["_fetch_chart_quote"]
        fetcher_mod.PriceFetcher._get_safe_fast_info = originals["_get_safe_fast_info"]
        fetcher_mod.PriceFetcher._fetch_history = originals["_fetch_history"]
        for key, value in env_keys.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_fetch_chart_quote_prefers_day_high_low_from_meta():
    original_session = fetcher_mod.requests.Session

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "chart": {
                    "result": [{
                        "meta": {
                            "regularMarketPrice": 101.5,
                            "previousClose": 100.0,
                            "regularMarketVolume": 123456,
                            "regularMarketDayHigh": 105.25,
                            "regularMarketDayLow": 97.75,
                        },
                        "indicators": {
                            "quote": [{
                                "close": [101.5],
                                "volume": [123456],
                                "high": [101.5],
                                "low": [101.5],
                            }]
                        },
                    }]
                }
            }

    class DummySession:
        def __init__(self):
            self.trust_env = False

        def get(self, *args, **kwargs):
            return DummyResponse()

    try:
        fetcher_mod.requests.Session = DummySession
        pf = PriceFetcher("MSFT")
        quote = pf._fetch_chart_quote()
    finally:
        fetcher_mod.requests.Session = original_session

    assert quote["price"] == 101.5
    assert quote["high"] == 105.25
    assert quote["low"] == 97.75


def test_get_ohlcv_prefers_direct_chart_history():
    original_session = fetcher_mod.requests.Session
    original_fetch_history = fetcher_mod.PriceFetcher._fetch_history
    original_env = os.environ.get("AI_SELECTOR_DIRECT_HISTORY")

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "chart": {
                    "result": [{
                        "timestamp": [1700000000, 1700086400],
                        "indicators": {
                            "quote": [{
                                "open": [10.0, 10.5],
                                "high": [10.2, 10.7],
                                "low": [9.9, 10.3],
                                "close": [10.1, 10.6],
                                "volume": [1000, 1200],
                            }]
                        },
                    }]
                }
            }

    class DummySession:
        def __init__(self):
            self.trust_env = False

        def get(self, *args, **kwargs):
            return DummyResponse()

    try:
        os.environ["AI_SELECTOR_DIRECT_HISTORY"] = "1"
        fetcher_mod.requests.Session = DummySession
        fetcher_mod.PriceFetcher._fetch_history = lambda self, period, interval, prepost=True: (_ for _ in ()).throw(
            AssertionError("yfinance history should not be used when direct chart history succeeds")
        )

        pf = PriceFetcher("MSFT")
        candles = pf.get_ohlcv(period="1mo", interval="1d")
    finally:
        fetcher_mod.requests.Session = original_session
        fetcher_mod.PriceFetcher._fetch_history = original_fetch_history
        if original_env is None:
            os.environ.pop("AI_SELECTOR_DIRECT_HISTORY", None)
        else:
            os.environ["AI_SELECTOR_DIRECT_HISTORY"] = original_env

    assert len(candles) == 2
    assert candles[-1].close == 10.6
    assert candles[-1].volume == 1200


def run_test_direct():
    test_get_quote_from_history()
    test_validate_live_mode_requires_longbridge_credentials()
    test_synthetic_market_fallback()
    test_fetch_chart_quote_prefers_day_high_low_from_meta()
    test_get_ohlcv_prefers_direct_chart_history()


if __name__ == '__main__':
    run_test_direct()
