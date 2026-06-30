"""Position sizing helpers shared by the engine and backtest path."""


def determine_buy_quantity(
    current_price: float,
    available_cash: float,
    configured_size: int,
    max_position: int,
    execution_price: float | None = None,
    commission_per_share: float = 0.005,
) -> int:
    """Return the number of shares to buy for a long entry.

    If ``configured_size`` is positive, it is treated as a fixed share count.
    Otherwise, the system uses the existing auto-sizing rule: deploy 80% of
    available cash and round down to a whole number of shares.
    """
    reference_price = execution_price if execution_price and execution_price > 0 else current_price
    if current_price <= 0 or reference_price <= 0:
        return 0

    per_share_cost = reference_price + max(0.0, commission_per_share)
    affordable_shares = int(available_cash / per_share_cost) if available_cash > 0 and per_share_cost > 0 else 0
    if affordable_shares <= 0:
        return 0

    if configured_size > 0:
        shares = configured_size
    else:
        shares = max(1, int((available_cash * 0.80) / current_price))

    return min(shares, affordable_shares, max_position)
