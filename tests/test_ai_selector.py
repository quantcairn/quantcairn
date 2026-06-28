from datetime import datetime, timezone

from src.ai_selector.pipeline import AIStockSelector, write_report, write_top_configs
from src.news_agent.collector import NewsCollector, NewsItem
from src.scoring.technical import Candle


def _uptrend():
    candles = []
    price = 100.0
    for idx in range(240):
        open_price = price - 0.4
        close_price = price + 0.8
        candles.append(Candle(timestamp=idx, open=open_price, high=close_price + 0.6, low=open_price - 0.5, close=close_price, volume=2_000_000))
        price += 0.6
    return candles


def _downtrend():
    candles = []
    price = 220.0
    for idx in range(240):
        open_price = price + 0.6
        close_price = price - 0.9
        candles.append(Candle(timestamp=idx, open=open_price, high=open_price + 0.8, low=close_price - 0.8, close=close_price, volume=1_300_000))
        price -= 0.55
    return candles


class FakeQuoteContext:
    def security_list(self, market, category=None):
        return [{"symbol": "AAPL.US"}, {"symbol": "TSLA.US"}, {"symbol": "MSFT.US"}]

    def static_info(self, symbols):
        return [
            {"symbol": "AAPL.US", "last_done": 190.0, "market_cap": 2_000_000_000_000},
            {"symbol": "TSLA.US", "last_done": 240.0, "market_cap": 700_000_000_000},
            {"symbol": "MSFT.US", "last_done": 420.0, "market_cap": 2_500_000_000_000},
            {"symbol": "SPY.US", "last_done": 530.0, "market_cap": 0},
        ]

    def history_candlesticks_by_offset(self, symbol, period, adjust_type, forward, count, time=None, trade_sessions=None):
        if symbol == "SPY.US":
            return _uptrend()
        if symbol == "AAPL.US":
            return _uptrend()
        if symbol == "MSFT.US":
            return _uptrend()
        return _downtrend()


class FakeNewsCollector(NewsCollector):
    def __init__(self):
        pass

    def collect(self, symbols, quote_ctx=None):
        now = datetime.now(timezone.utc)
        return {
            "AAPL.US": [
                NewsItem(symbol="AAPL.US", source="news", title="Apple beats expectations with strong guidance", summary="", published_at=now),
                NewsItem(symbol="AAPL.US", source="reddit", title="Bullish Apple earnings thread", summary="", published_at=now),
            ],
            "TSLA.US": [
                NewsItem(symbol="TSLA.US", source="news", title="Tesla faces downgrade after weak delivery warning", summary="", published_at=now),
            ],
            "MSFT.US": [
                NewsItem(symbol="MSFT.US", source="sec", title="Microsoft reports record profit and buyback", summary="", published_at=now),
            ],
        }

    def score(self, symbol, items):
        from src.news_agent.collector import score_news_items

        return score_news_items(symbol, items)


def test_ai_selector_ranks_and_writes_configs(tmp_path):
    selector = AIStockSelector(FakeQuoteContext(), news_collector=FakeNewsCollector(), candidate_limit=10, market_proxy="SPY.US")
    report = selector.run(symbols=["AAPL.US", "TSLA.US", "MSFT.US"])

    assert report.top10
    assert len(report.top3) == 3
    assert all(row.symbol.endswith(".US") for row in report.top3)

    json_path, md_path = write_report(report, tmp_path / "outputs")
    assert json_path.exists()
    assert md_path.exists()

    written = write_top_configs(report, tmp_path / "configs")
    assert len(written) == 3
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "ticker:" in text
        assert "range:" in text
        assert "risk:" in text
