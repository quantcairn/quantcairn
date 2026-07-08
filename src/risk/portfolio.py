"""
Portfolio-level risk management: correlation limits, total exposure, drawdown.

Wraps individual engine risk managers with a cross-portfolio view.
A single PortfolioRisk instance is shared across all TradingEngine instances.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..risk.instrument_profile import LEVERAGED_ETF_REGISTRY, is_leveraged_etf

logger = logging.getLogger(__name__)

MAX_POSITION_PCT_DEFAULT = 1.0  # 100% — no cap by default


class PortfolioRisk:
    """
    Aggregate portfolio risk controller.

    Accepts a list of engine configs (or config-like dicts) to derive the
    set of symbols under management.  Methods are thread-safe in the sense
    that *read* calls (exposure, drawdown) are idempotent; callers sharing
    a single instance must coordinate writes (update_equity, record_trade).

    Typical usage::

        portfolio_risk = PortfolioRisk([config1, config2, config3])
        for engine in all_engines:
            engine.set_portfolio_risk(portfolio_risk)

    Each engine calls ``portfolio_risk.check_correlation_limit(t1, t2)``
    in its buy-signal handler to apply cross-engine limits.
    """

    def __init__(self, engine_configs: list[Any]):
        # engine_configs is a list of AppConfig (or any object with .ticker)
        self._configs: list[Any] = list(engine_configs or [])

        # Externally-synced state (caller updates these)
        self._position_values: dict[str, float] = {}   # ticker → market value ($)
        self._current_equity: float = 0.0
        self._peak_equity: float = 0.0

    # ---- public helpers ----

    @property
    def tickers(self) -> list[str]:
        """Return the list of tickers managed by member engines."""
        tickers: list[str] = []
        for cfg in self._configs:
            t = str(getattr(cfg, "ticker", "") or "").strip().upper()
            if t:
                tickers.append(t)
        return tickers

    # ---- correlation limit ----

    def check_correlation_limit(self, ticker1: str, ticker2: str) -> float:
        """
        Return a *reduction factor* (1.0 = no reduction, 0.70 = 70 % cap)
        when both tickers are same-sector leveraged ETFs.

        If either ticker is not a leveraged ETF, or they belong to different
        sectors, the default factor of 1.0 is returned.
        """
        t1 = str(ticker1 or "").strip().upper()
        t2 = str(ticker2 or "").strip().upper()
        if not t1 or not t2:
            return 1.0

        p1 = LEVERAGED_ETF_REGISTRY.get(t1)
        p2 = LEVERAGED_ETF_REGISTRY.get(t2)

        # Both must be registered leveraged ETFs
        if not p1 or not p2:
            return 1.0

        sector1 = str(p1.get("sector", "") or "").strip()
        sector2 = str(p2.get("sector", "") or "").strip()

        # Different sectors → correlation less of a concern
        if not sector1 or not sector2 or sector1 != sector2:
            return 1.0

        logger.info(
            "Correlation limit: %s and %s are both %s leveraged ETFs "
            "→ reducing max position to 70 %%",
            t1, t2, sector1,
        )
        return 0.70

    # ---- total exposure ----

    def set_position_value(self, ticker: str, market_value: float) -> None:
        """Update the market value of a single position (called per engine)."""
        ticker = str(ticker or "").strip().upper()
        if ticker:
            self._position_values[ticker] = max(0.0, float(market_value or 0.0))

    def get_total_exposure(self) -> float:
        """Sum of all engine position market values."""
        return sum(self._position_values.values())

    # ---- drawdown ----

    def update_equity(self, current_equity: float) -> None:
        """Sync total portfolio equity.  Called periodically by the combined loop."""
        self._current_equity = max(0.0, float(current_equity or 0.0))
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity

    def get_max_drawdown(self) -> dict:
        """
        Return the current drawdown (peak → trough) stats.

        Returns
        -------
        dict with keys::

            peak_equity      — all-time high equity
            current_equity   — latest equity
            drawdown_dollars — dollars lost from peak
            drawdown_pct     — percentage lost from peak (0 if no peak)
        """
        peak = self._peak_equity
        current = self._current_equity
        dd_dollars = peak - current if peak > 0 else 0.0
        dd_pct = (dd_dollars / peak * 100.0) if peak > 0 else 0.0
        return {
            "peak_equity": round(peak, 2),
            "current_equity": round(current, 2),
            "drawdown_dollars": round(max(0.0, dd_dollars), 2),
            "drawdown_pct": round(max(0.0, dd_pct), 2),
        }

    # ---- aggregate summary ----

    def summary(self) -> dict:
        """Return a dict suitable for dashboard / risk display."""
        dd = self.get_max_drawdown()
        return {
            "tickers": self.tickers,
            "position_values": dict(self._position_values),
            "total_exposure": round(self.get_total_exposure(), 2),
            **dd,
        }
