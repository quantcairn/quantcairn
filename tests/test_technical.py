from src.strategy.technical import Candle, analyze_market, analyze_symbol, select_trade


def _uptrend_candles():
    candles = []
    price = 100.0
    for i in range(80):
        open_price = price - 0.3
        close_price = price + 0.7
        high = close_price + 0.5
        low = open_price - 0.5
        candles.append(Candle(timestamp=i, open=open_price, high=high, low=low, close=close_price, volume=1000))
        price += 1.0
    return candles


def _doji_pullback_candles():
    candles = _uptrend_candles()
    last = candles[-1]
    candles[-1] = Candle(
        timestamp=last.timestamp,
        open=last.close - 0.05,
        high=last.close + 0.6,
        low=last.close - 0.7,
        close=last.close - 0.02,
        volume=1200,
    )
    return candles


def _downtrend_candles():
    candles = []
    price = 180.0
    for i in range(80):
        open_price = price + 0.4
        close_price = price - 0.8
        high = open_price + 0.6
        low = close_price - 0.6
        candles.append(Candle(timestamp=i, open=open_price, high=high, low=low, close=close_price, volume=1000))
        price -= 1.0
    return candles


def test_analyze_market_detects_trend():
    market = analyze_market("SPY.US", _uptrend_candles())
    assert market.action in {"buy", "hold"}
    assert market.score >= 1


def test_analyze_symbol_buys_uptrend_doji_reversal():
    signal = analyze_symbol("AAPL.US", _doji_pullback_candles(), market_bias=1)
    assert signal.action == "buy"
    assert signal.score >= 2
    assert signal.doji is True


def test_analyze_symbol_sells_downtrend():
    signal = analyze_symbol("TSLA.US", _downtrend_candles(), market_bias=-1)
    assert signal.action == "sell"
    assert signal.score <= -3


def test_select_trade_picks_best_signal():
    class FakeQuoteContext:
        def history_candlesticks_by_offset(self, symbol, period, adjust_type, forward, count, time=None, trade_sessions=None):
            if symbol == "SPY.US":
                return _uptrend_candles()
            if symbol == "AAPL.US":
                return _doji_pullback_candles()
            return _downtrend_candles()

    report = select_trade(["AAPL.US", "TSLA.US"], FakeQuoteContext(), market_proxy="SPY.US", lookback=80)
    assert report["best"].symbol == "AAPL.US"
    assert report["best"].action == "buy"
    assert report["actionable"] is True
