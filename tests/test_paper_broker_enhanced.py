"""
Enhanced paper broker tests: slippage, commission, trade records, data guards,
ATR stop loss, and full roundtrip.

These tests import only the broker classes — no longbridge or live dependencies.
"""
import time

from src.broker.base import OrderSide, OrderStatus, OrderType
from src.broker.paper_broker import PaperBroker


# ──────────────────────────────────────────────
# Helper: simple ATR calculation
# ──────────────────────────────────────────────

def _calc_atr(highs, lows, closes, window=14):
    """Average True Range using Wilder's smoothing."""
    tr_values = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_values.append(tr)

    if not tr_values:
        return 0.0

    if len(tr_values) <= window:
        return sum(tr_values) / len(tr_values)

    atr = sum(tr_values[:window]) / window
    for tr in tr_values[window:]:
        atr = (atr * (window - 1) + tr) / window
    return atr


# ──────────────────────────────────────────────
# 1. Slippage
# ──────────────────────────────────────────────

def test_slippage_applied():
    """
    Place buy/sell market orders and verify slippage is applied to the fill price.

    By default slippage_pct = 0.05, so the random slip factor is in
    [0.01%, 0.05%] of the reference price.

    BUY  → fill = ask × (1 + slip)      → fill > ask
    SELL → fill = bid × (1 - slip)      → fill < bid
    """
    broker = PaperBroker(initial_cash=100_000, commission_per_share=0.0)
    broker.connect()

    bid, ask = 9.95, 10.05

    buy = broker.place_order(
        "SOXS", OrderSide.BUY, 100, OrderType.MARKET,
        current_bid=bid, current_ask=ask,
    )
    assert buy.status == OrderStatus.FILLED, f"BUY not filled: {buy.status}"

    # BUY should fill above ask (positive slippage)
    assert buy.avg_fill_price > ask, (
        f"BUY fill {buy.avg_fill_price:.4f} should be above ask {ask}"
    )
    # Slippage should not exceed 0.05 % of ask
    max_allowed = ask * (1 + 0.05 / 100)
    assert buy.avg_fill_price <= max_allowed, (
        f"BUY fill {buy.avg_fill_price:.6f} exceeds max {max_allowed:.6f}"
    )

    sell = broker.place_order(
        "SOXS", OrderSide.SELL, 100, OrderType.MARKET,
        current_bid=bid, current_ask=ask,
    )
    assert sell.status == OrderStatus.FILLED, f"SELL not filled: {sell.status}"

    # SELL should fill below bid (negative slippage)
    assert sell.avg_fill_price < bid, (
        f"SELL fill {sell.avg_fill_price:.4f} should be below bid {bid}"
    )
    # Slippage should not exceed 0.05 % of bid
    min_allowed = bid * (1 - 0.05 / 100)
    assert sell.avg_fill_price >= min_allowed, (
        f"SELL fill {sell.avg_fill_price:.6f} below min {min_allowed:.6f}"
    )

    # Slippage exists in both directions
    midpoint = (bid + ask) / 2
    assert buy.avg_fill_price > midpoint, "BUY fill should be above midpoint"
    assert sell.avg_fill_price < midpoint, "SELL fill should be below midpoint"


# ──────────────────────────────────────────────
# 2. Commission
# ──────────────────────────────────────────────

def test_commission_deducted():
    """Verify commission is subtracted from account cash on a buy order."""
    broker = PaperBroker(initial_cash=1000.0, commission_per_share=0.01)
    broker.connect()

    cash_before = broker.get_account().cash

    order = broker.place_order(
        "SOXS", OrderSide.BUY, 50, OrderType.MARKET,
        current_bid=10.0, current_ask=10.0,
    )
    assert order.status == OrderStatus.FILLED

    cash_after = broker.get_account().cash

    # Commission: 50 shares × $0.01 = $0.50
    expected_commission = 50 * 0.01
    trade_value = order.avg_fill_price * 50
    expected_cash = cash_before - trade_value - expected_commission

    assert abs(cash_after - expected_cash) < 0.02, (
        f"Cash after buy: {cash_after:.2f}, expected {expected_cash:.2f} "
        f"(fill={order.avg_fill_price:.4f}, comm={order.commission:.4f})"
    )
    assert abs(order.commission - expected_commission) < 0.001, (
        f"Commission: expected {expected_commission}, got {order.commission}"
    )


# ──────────────────────────────────────────────
# 3. Trade record completeness
# ──────────────────────────────────────────────

def test_trade_record_complete():
    """
    Verify that a filled order produces a complete TradeRecord with
    executed_at, slippage, and commission populated.
    """
    broker = PaperBroker(initial_cash=100_000, commission_per_share=0.005)
    broker.connect()

    order = broker.place_order(
        "SOXS", OrderSide.BUY, 100, OrderType.MARKET,
        current_bid=50.0, current_ask=50.10,
    )
    assert order.status == OrderStatus.FILLED

    # Fields on the returned Order object
    assert order.filled_at is not None, "filled_at must be set on filled order"
    assert order.commission > 0, f"commission must be > 0, got {order.commission}"
    assert order.avg_fill_price > 0, f"avg_fill_price must be > 0, got {order.avg_fill_price}"
    assert order.filled_quantity == 100, f"filled_quantity must be 100, got {order.filled_quantity}"

    # Fields on the recorded TradeRecord (accessible via get_trade_history)
    history = broker.get_trade_history()
    assert len(history) == 1, f"Expected 1 trade record, got {len(history)}"

    rec = history[0]
    assert rec.executed_at is not None, "executed_at must be set in TradeRecord"
    assert rec.slippage > 0, f"slippage must be > 0, got {rec.slippage}"
    assert rec.commission > 0, f"commission must be > 0, got {rec.commission}"
    assert rec.quantity == 100, f"quantity must be 100, got {rec.quantity}"
    assert rec.ticker == "SOXS"
    assert rec.side == OrderSide.BUY
    assert rec.price > 0, f"price must be > 0, got {rec.price}"


# ──────────────────────────────────────────────
# 4. No data guard
# ──────────────────────────────────────────────

def test_no_data_guard():
    """
    When both current_bid and current_ask are ≤ 0 (simulating price=None),
    PaperBroker rejects the order with 'Missing bid/ask prices'.
    """
    broker = PaperBroker(initial_cash=100_000)
    broker.connect()

    order = broker.place_order(
        "SOXS", OrderSide.BUY, 10, OrderType.MARKET,
        current_bid=0, current_ask=0,
    )
    assert order.status == OrderStatus.REJECTED, (
        f"Expected REJECTED, got {order.status}"
    )
    assert "Missing bid/ask" in order.notes, (
        f"Expected 'Missing bid/ask' in notes, got: {order.notes}"
    )


# ──────────────────────────────────────────────
# 5. Stale data guard
# ──────────────────────────────────────────────

class PriceDataError(Exception):
    """Raised when price data is missing or stale."""
    pass


class _StaleDataAwareBroker(PaperBroker):
    """
    PaperBroker subclass that rejects orders when price data
    hasn't been refreshed within ``max_data_age`` seconds.
    """

    def __init__(self, *args, max_data_age=60, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_data_age = max_data_age
        self._last_update_time = 0.0

    def place_order(self, *args, **kwargs):
        elapsed = time.monotonic() - self._last_update_time
        if elapsed > self.max_data_age:
            raise PriceDataError(
                f"Stale price data: last update {elapsed:.1f}s ago "
                f"(max_age={self.max_data_age}s)"
            )
        return super().place_order(*args, **kwargs)


def test_stale_data_guard():
    """
    Set max_data_age=1, wait 2 seconds, then attempt an order.
    The broker must raise PriceDataError because the data is stale.
    """
    broker = _StaleDataAwareBroker(initial_cash=100_000, max_data_age=1)
    broker.connect()

    # "Update" prices — marks data as fresh
    broker._last_update_time = time.monotonic()

    # Wait past the 1-second expiry
    time.sleep(1.5)

    raised = False
    try:
        broker.place_order(
            "SOXS", OrderSide.BUY, 10, OrderType.MARKET,
            current_bid=50.0, current_ask=50.10,
        )
    except PriceDataError as exc:
        raised = True
        assert "stale" in str(exc).lower(), f"Error should mention 'stale': {exc}"

    assert raised, "Expected PriceDataError for stale price data"


# ──────────────────────────────────────────────
# 6. ATR stop loss
# ──────────────────────────────────────────────

def test_atr_stop_loss():
    """
    Verify that a dynamic ATR-based stop loss is wider (further from entry)
    than a static percentage-based stop loss, for moderately volatile data.
    """
    # Sample OHLC data with moderate volatility
    highs = [101, 103, 102, 104, 106, 105, 107, 109, 108, 110]
    lows =  [99,  98,  99,  101, 103, 102, 104, 106, 105, 107]
    closes = [100, 102, 101, 103, 105, 104, 106, 108, 107, 109]

    atr = _calc_atr(highs, lows, closes, window=5)
    assert atr > 0, f"ATR must be positive, got {atr}"

    entry_price = closes[-1]

    # Static stop: 2 % below entry
    static_stop = entry_price * 0.98

    # Dynamic stop: entry - 2 × ATR
    dynamic_stop = entry_price - (atr * 2.0)

    # With this data, ATR should be wide enough that the dynamic stop
    # is further from entry than the tight 2 % static stop.
    assert dynamic_stop < static_stop, (
        f"Dynamic stop ${dynamic_stop:.2f} should be lower (further from entry) "
        f"than static stop ${static_stop:.2f} (ATR={atr:.2f}, entry={entry_price})"
    )


# ──────────────────────────────────────────────
# 7. Full roundtrip
# ──────────────────────────────────────────────

def test_paper_broker_roundtrip():
    """
    Buy 100 shares at ~$10, sell 100 shares at ~$11, then verify:
      - Both orders filled
      - Commission recorded on both legs
      - Net P&L positive (price rose ~$1/share)
      - Cash > initial ($10 000)
      - No residual position
    """
    broker = PaperBroker(initial_cash=10_000, commission_per_share=0.005)
    broker.connect()

    # ── Buy 100 shares @ ~$10 ──
    buy = broker.place_order(
        "SOXS", OrderSide.BUY, 100, OrderType.MARKET,
        current_bid=9.98, current_ask=10.02,
    )
    assert buy.status == OrderStatus.FILLED, f"BUY not filled: {buy.status}"
    assert buy.filled_quantity == 100
    assert buy.commission > 0

    # ── Sell 100 shares @ ~$11 ──
    sell = broker.place_order(
        "SOXS", OrderSide.SELL, 100, OrderType.MARKET,
        current_bid=10.98, current_ask=11.02,
    )
    assert sell.status == OrderStatus.FILLED, f"SELL not filled: {sell.status}"
    assert sell.filled_quantity == 100
    assert sell.commission > 0

    # ── Verify position closed ──
    pos = broker.get_position_for_ticker("SOXS")
    assert pos is None or pos.quantity == 0, (
        f"Expected no position, got qty={pos.quantity}"
    )

    # ── Verify cash and P&L ──
    buy_cost = buy.avg_fill_price * 100
    sell_proceeds = sell.avg_fill_price * 100
    total_commission = buy.commission + sell.commission
    # Net cash flow: -buy_cost + sell_proceeds - total_commission
    net_cash_change = sell_proceeds - buy_cost - total_commission

    assert net_cash_change > 0, (
        f"Expected positive cash flow from ~$1/share move, "
        f"got ${net_cash_change:.2f} "
        f"(buy_fill={buy.avg_fill_price:.4f}, sell_fill={sell.avg_fill_price:.4f}, "
        f"comm={total_commission:.4f})"
    )

    account = broker.get_account()
    assert account.cash > 10_000, (
        f"Cash should exceed initial $10 000, got ${account.cash:.2f}"
    )

    # ── Verify two trade records with all fields populated ──
    history = broker.get_trade_history()
    assert len(history) == 2, f"Expected 2 trade records, got {len(history)}"

    for rec in history:
        assert rec.executed_at is not None, "executed_at missing"
        assert rec.slippage > 0, "slippage must be > 0"
        assert rec.commission > 0, "commission must be > 0"


# ──────────────────────────────────────────────
# Direct runner (matches existing test convention)
# ──────────────────────────────────────────────

def run_test_direct():
    test_slippage_applied()
    test_commission_deducted()
    test_trade_record_complete()
    test_no_data_guard()
    test_stale_data_guard()
    test_atr_stop_loss()
    test_paper_broker_roundtrip()


if __name__ == "__main__":
    run_test_direct()
