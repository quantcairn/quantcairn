"""
Tests for portfolio risk metrics: correlation and drawdown calculations.

All functions are self-contained — no external dependencies.
"""
import math


# ──────────────────────────────────────────────
# Shared risk-metric implementations
# ──────────────────────────────────────────────

def pearson_correlation(x, y):
    """Pearson product-moment correlation coefficient between two series."""
    n = len(x)
    if n != len(y) or n < 2:
        return 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(a * b for a, b in zip(x, y))
    sum_x2 = sum(a * a for a in x)
    sum_y2 = sum(b * b for b in y)

    numerator = n * sum_xy - sum_x * sum_y
    denominator = math.sqrt(
        (n * sum_x2 - sum_x * sum_x) * (n * sum_y2 - sum_y * sum_y)
    )
    if denominator == 0:
        return 0.0
    return numerator / denominator


def max_drawdown(prices):
    """Maximum drawdown (as a fraction, 0–1) from a price series."""
    if not prices or len(prices) < 2:
        return 0.0
    peak = prices[0]
    max_dd = 0.0
    for price in prices:
        if price > peak:
            peak = price
        dd = (peak - price) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ──────────────────────────────────────────────
# Correlation tests
# ──────────────────────────────────────────────

def test_correlation_perfect_positive():
    """Perfectly positively correlated series → r = 1.0."""
    x = [1, 2, 3, 4, 5]
    y = [2, 4, 6, 8, 10]
    corr = pearson_correlation(x, y)
    assert abs(corr - 1.0) < 1e-6, f"Expected 1.0, got {corr:.6f}"


def test_correlation_perfect_negative():
    """Perfectly negatively correlated series → r = -1.0."""
    x = [1, 2, 3, 4, 5]
    y = [10, 8, 6, 4, 2]
    corr = pearson_correlation(x, y)
    assert abs(corr + 1.0) < 1e-6, f"Expected -1.0, got {corr:.6f}"


def test_correlation_zero():
    """Uncorrelated series → r ≈ 0.0."""
    x = [1, 2, 3, 4, 5]
    y = [100, 100, 100, 100, 100]
    corr = pearson_correlation(x, y)
    # Constant y → denominator = 0 → handled as 0.0
    assert corr == 0.0, f"Expected 0.0 (constant denominator), got {corr:.6f}"


# ──────────────────────────────────────────────
# Drawdown tests
# ──────────────────────────────────────────────

def test_max_drawdown_known():
    """
    Known drawdown: peak=110, trough=90 → (110-90)/110 ≈ 18.18 %.

    Prices: 100, 110 (peak), 105, 95, 90 (trough), 105, 120 (new peak)
    """
    prices = [100, 110, 105, 95, 90, 105, 120]
    dd = max_drawdown(prices)
    expected = (110 - 90) / 110
    assert abs(dd - expected) < 1e-4, f"Expected {expected:.4f}, got {dd:.4f}"


def test_max_drawdown_monotonic_up():
    """Monotonically increasing series → 0 drawdown."""
    prices = [100, 110, 120, 130, 140]
    dd = max_drawdown(prices)
    assert dd == 0.0, f"Expected 0.0, got {dd:.6f}"


def test_max_drawdown_monotonic_down():
    """Monotonically decreasing series → drawdown = (first-last)/first."""
    prices = [100, 90, 80, 70, 60]
    dd = max_drawdown(prices)
    expected = (100 - 60) / 100
    assert abs(dd - expected) < 1e-4, f"Expected {expected:.4f}, got {dd:.4f}"


# ──────────────────────────────────────────────
# Direct runner
# ──────────────────────────────────────────────

def run_test_direct():
    test_correlation_perfect_positive()
    test_correlation_perfect_negative()
    test_correlation_zero()
    test_max_drawdown_known()
    test_max_drawdown_monotonic_up()
    test_max_drawdown_monotonic_down()


if __name__ == "__main__":
    run_test_direct()
