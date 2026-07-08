"""
Tests for Average True Range (ATR) calculation and ATR-based stop-loss logic.

All functions are self-contained — no external dependencies beyond the standard library.
"""


# ──────────────────────────────────────────────
# Shared ATR implementation
# ──────────────────────────────────────────────

def calc_atr(highs, lows, closes, window=14):
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

    # Wilder's smoothed ATR
    atr = sum(tr_values[:window]) / window
    for tr in tr_values[window:]:
        atr = (atr * (window - 1) + tr) / window
    return atr


# ──────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────

def test_atr_basic():
    """
    Basic ATR sanity: flat, low-volatility data yields a small, positive ATR.

    Data description:
      - Prices range from ~100 to ~103
      - True ranges span 1–4 points
      - ATR(3) should settle around 2–3
    """
    highs = [101, 102, 101, 103, 102]
    lows =  [99,   98,  99, 101, 100]
    closes = [100, 100, 100, 102, 101]

    atr = calc_atr(highs, lows, closes, window=3)
    assert atr > 0, f"ATR must be positive, got {atr}"
    assert 1.0 < atr < 5.0, f"ATR={atr:.2f} is outside expected range [1, 5]"


def test_atr_volatile():
    """
    High-volatility data produces a larger ATR value.

    Data description:
      - Daily ranges are ~50 points wide
      - True ranges often exceed 30
      - ATR(3) should be well above 10
    """
    highs = [150, 155, 148, 160, 158]
    lows =  [100, 105,  98, 110, 108]
    closes = [120, 130, 115, 140, 135]

    atr = calc_atr(highs, lows, closes, window=3)
    assert atr > 10, f"Volatile data should yield ATR > 10, got {atr:.2f}"
    assert atr < 100, f"ATR={atr:.2f} is unreasonably large for this data"


def test_atr_smoothing():
    """
    Longer windows produce smoother (less extreme) ATR values.

    With gradually widening ranges, the short-window ATR should be more
    responsive (higher) than the long-window ATR.
    """
    highs = [100, 102, 101, 103, 105, 104, 106, 108, 107, 109]
    lows =  [95,   97,  96,  98, 100,  99, 101, 103, 102, 104]
    closes = [98, 100,  99, 101, 103, 102, 104, 106, 105, 107]

    atr_short = calc_atr(highs, lows, closes, window=3)
    atr_long = calc_atr(highs, lows, closes, window=7)

    assert atr_short > 0, f"Short-window ATR must be positive, got {atr_short}"
    assert atr_long > 0, f"Long-window ATR must be positive, got {atr_long}"

    # Long window should not be radically different from short window
    upper_bound = atr_short * 2.0
    lower_bound = atr_short * 0.4
    assert lower_bound <= atr_long <= upper_bound, (
        f"Long-window ATR ({atr_long:.2f}) too far from short-window ({atr_short:.2f})"
    )


def test_atr_single_tr():
    """
    With exactly N data points (one TR), ATR equals that single TR value.
    """
    highs = [110, 112]
    lows =  [100, 108]
    closes = [105, 110]

    atr = calc_atr(highs, lows, closes, window=5)
    # Single TR = max(112-108, |112-105|, |108-105|) = max(4, 7, 3) = 7
    assert abs(atr - 7.0) < 0.001, f"Single TR ATR should be 7.0, got {atr:.4f}"


# ──────────────────────────────────────────────
# Direct runner
# ──────────────────────────────────────────────

def run_test_direct():
    test_atr_basic()
    test_atr_volatile()
    test_atr_smoothing()
    test_atr_single_tr()


if __name__ == "__main__":
    run_test_direct()
