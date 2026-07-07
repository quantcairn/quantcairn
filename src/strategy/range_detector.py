"""
Range detection strategy: identifies range-bound conditions and generates signals.

Two modes:
1. Manual: Uses user-defined support/resistance levels
2. Auto: Automatically detects range using recent price action
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class SignalType(Enum):
    BUY = "BUY"              # Price near support → buy
    SELL = "SELL"            # Price near resistance → sell
    STOP_LOSS = "STOP_LOSS"  # Price broke support → emergency exit
    HOLD = "HOLD"            # No action
    TREND_BLOCK = "TREND_BLOCK"  # Signal blocked by trend filter


@dataclass
class Signal:
    type: SignalType
    ticker: str
    price: float
    support: float
    resistance: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)
    confidence: float = 1.0  # 0.0–1.0, lower = less certain


@dataclass
class RangeState:
    """Current state of the detected/configured range."""
    support: float
    resistance: float
    spread_dollars: float
    spread_pct: float
    midpoint: float
    is_valid: bool
    support_confidence: float = 0.0
    source: str = "unknown"
    detected_at: datetime = field(default_factory=datetime.now)


class RangeDetector:
    """
    Detects range-bound conditions and generates buy/sell signals.

    Manual mode: User sets exact support/resistance. Signals fire when
        price is within tolerance_pct of either boundary.

    Auto mode: Uses recent high/low from last N bars to define range.
        Refreshes every auto_refresh_minutes.
    """

    BUY_ZONE_MAX_POSITION_PCT = 60.0
    TREND_BLOCK_MIN_PCT_FROM_MA = 1.2

    def __init__(
        self,
        ticker: str,
        mode: str = "manual",
        support_price: Optional[float] = None,
        resistance_price: Optional[float] = None,
        tolerance_pct: float = 0.3,
        auto_lookback: int = 50,
        auto_refresh_minutes: int = 15,
        trend_ma_period: int = 20,
        trend_enabled: bool = True,
        trend_min_strength: float = 0.5,
        min_profit_per_trade: float = 1.0,
        min_range_width_pct: float = 0.8,
        quick_stop_pct: float = 3.0,
    ):
        self.ticker = ticker
        self.mode = mode
        self._manual_support = support_price
        self._manual_resistance = resistance_price
        self.tolerance_pct = tolerance_pct
        self.auto_lookback = auto_lookback
        self.auto_refresh_minutes = auto_refresh_minutes

        self._auto_support: Optional[float] = None
        self._auto_resistance: Optional[float] = None
        self._last_auto_refresh: Optional[datetime] = None
        self._range_source: str = "manual" if mode == "manual" else "uninitialized"

        # Price history for auto-detection + trend calculation
        self._price_history: list[float] = []

        # Volume-profile data: (price, volume) tuples for weighted S/R
        self._volume_profile: list[tuple[float, float]] = []  # (price_midpoint, volume)
        self._bar_extremes: list[tuple[float, float]] = []  # (high, low)

        # Trend filter state
        self.trend_enabled = trend_enabled
        self.trend_ma_period = trend_ma_period
        self.trend_min_strength = trend_min_strength
        self._trend_ma: Optional[float] = None
        self._trend_direction: str = "neutral"  # "up", "down", "neutral"

        # Profit / risk filter
        self.min_profit_per_trade = min_profit_per_trade
        self.min_range_width_pct = min_range_width_pct
        self.quick_stop_pct = quick_stop_pct
        self._entry_price: Optional[float] = None  # Last entry for quick stop

        # Quality metrics
        self._support_confidence: float = 0.0  # 0-1: how reliable is support?
        self._support_touch_count: int = 0     # times price bounced off support
        self._resistance_touch_count: int = 0  # times price hit resistance

        if self.mode == "manual" and self._manual_support is not None and self._manual_resistance is not None:
            self._support_confidence = 1.0

    @property
    def support(self) -> Optional[float]:
        """Current support level."""
        if self.mode == "manual":
            return self._manual_support
        return self._auto_support

    @property
    def resistance(self) -> Optional[float]:
        """Current resistance level."""
        if self.mode == "manual":
            return self._manual_resistance
        return self._auto_resistance

    def get_range_state(self) -> RangeState:
        """Get current range state with metadata."""
        s, r = self.support, self.resistance
        if s is None or r is None or s <= 0:
            return RangeState(
                support=0, resistance=0, spread_dollars=0,
                spread_pct=0, midpoint=0, is_valid=False, source=self._range_source,
            )

        spread = r - s
        spread_pct = (spread / s) * 100 if s > 0 else 0
        return RangeState(
            support=s,
            resistance=r,
            spread_dollars=round(spread, 2),
            spread_pct=round(spread_pct, 2),
            midpoint=round((s + r) / 2, 2),
            is_valid=spread > 0,
            support_confidence=self._support_confidence,
            source=self._range_source,
        )

    def apply_auto_range(self, support: float, resistance: float, confidence: float = 0.3, source: str = "auto") -> bool:
        """Apply a validated auto range from any source."""
        if not self._is_range_tradeable(support, resistance):
            return False
        self._auto_support = round(float(support), 2)
        self._auto_resistance = round(float(resistance), 2)
        self._support_confidence = max(0.0, min(1.0, float(confidence)))
        self._range_source = source
        self._last_auto_refresh = datetime.now()
        return True

    def feed_price(self, price: float) -> None:
        """Feed a new price point for auto-detection and trend tracking."""
        self._price_history.append(price)
        max_len = max(self.auto_lookback * 2, self.trend_ma_period * 2)
        if len(self._price_history) > max_len:
            self._price_history = self._price_history[-max_len:]
        self._update_trend()

    def feed_volume_bar(self, high: float, low: float, close: float, volume: int) -> None:
        """Feed a single OHLCV bar for volume-profile range detection."""
        mid = (high + low) / 2
        self._volume_profile.append((mid, float(volume or 0)))
        self._bar_extremes.append((float(high or 0.0), float(low or 0.0)))
        max_bars = self.auto_lookback * 2
        if len(self._volume_profile) > max_bars:
            self._volume_profile = self._volume_profile[-max_bars:]
        if len(self._bar_extremes) > max_bars:
            self._bar_extremes = self._bar_extremes[-max_bars:]

    def _calc_volume_weighted_range(self) -> tuple[float, float, float, float]:
        """
        Calculate support/resistance using Volume Profile.

        Algorithm:
        1. Divide the price range into ~20 buckets
        2. Sum volume in each bucket → find where money traded
        3. Point of Control (POC) = bucket with most volume
        4. Value Area = 68% of total volume around POC
        5. Support = Value Area Low (where buyers concentrated)
        6. Resistance = Value Area High (where sellers concentrated)

        Returns (support, resistance, support_strength, resistance_strength)
        Falls back to percentile method if insufficient data.
        """
        vp = self._volume_profile
        if not vp or len(vp) < 10:
            # Fallback to percentile method
            return self._percentile_range()

        prices_only = [p for p, _ in vp]
        vols = [v for _, v in vp]
        if not prices_only:
            return self._percentile_range()

        price_min, price_max = min(prices_only), max(prices_only)
        price_range = price_max - price_min
        if price_range <= 0:
            return self._percentile_range()

        # ── Build volume profile buckets ──
        BUCKETS = 25
        bucket_size = price_range / BUCKETS
        buckets = [0.0] * BUCKETS
        bucket_centers = [price_min + (i + 0.5) * bucket_size for i in range(BUCKETS)]

        for (mid, vol) in vp:
            idx = min(BUCKETS - 1, int((mid - price_min) / bucket_size))
            if idx >= 0:
                buckets[idx] += vol

        total_vol = sum(buckets)
        if total_vol <= 0:
            return self._percentile_range()

        # ── Find Point of Control (POC) ──
        poc_idx = max(range(BUCKETS), key=lambda i: buckets[i])
        poc = bucket_centers[poc_idx]

        # ── Value Area (68% of volume around POC) ──
        target_vol = total_vol * 0.68
        accumulated = buckets[poc_idx]
        low_idx = poc_idx
        high_idx = poc_idx

        while accumulated < target_vol and (low_idx > 0 or high_idx < BUCKETS - 1):
            # Expand toward the side with more volume
            vol_below = buckets[low_idx - 1] if low_idx > 0 else 0
            vol_above = buckets[high_idx + 1] if high_idx < BUCKETS - 1 else 0

            if vol_below >= vol_above and low_idx > 0:
                low_idx -= 1
                accumulated += buckets[low_idx]
            elif high_idx < BUCKETS - 1:
                high_idx += 1
                accumulated += buckets[high_idx]
            elif low_idx > 0:
                low_idx -= 1
                accumulated += buckets[low_idx]
            else:
                break

        support_price = bucket_centers[low_idx] - bucket_size / 2
        resistance_price = bucket_centers[high_idx] + bucket_size / 2

        # ── Confidence: how concentrated is volume at boundaries ──
        # Higher = volume clusters more tightly → more reliable range
        support_vol_pct = buckets[low_idx] / max(buckets) if max(buckets) > 0 else 0
        resistance_vol_pct = buckets[high_idx] / max(buckets) if max(buckets) > 0 else 0

        # ── Touch count — how many times price hit each boundary ──
        touch_tolerance = bucket_size * 1.5
        support_touches = sum(
            1 for p, _ in vp if abs(p - support_price) <= touch_tolerance
        )
        resistance_touches = sum(
            1 for p, _ in vp if abs(p - resistance_price) <= touch_tolerance
        )

        logger.debug(
            f"[Volume Profile] VA: ${support_price:.2f}−${resistance_price:.2f} "
            f"POC=${poc:.2f} touches={support_touches}/{resistance_touches}"
        )

        return (
            round(support_price, 2),
            round(resistance_price, 2),
            round(support_vol_pct, 2),
            round(resistance_vol_pct, 2),
        )

    def _percentile_range(self) -> tuple[float, float, float, float]:
        """Fallback: simple percentile-based range."""
        if not self._price_history or len(self._price_history) < 10:
            return (0.0, 0.0, 0.0, 0.0)
        prices = self._price_history[-self.auto_lookback:]
        sorted_prices = sorted(prices)
        n = len(sorted_prices)
        support = sorted_prices[max(0, int(n * 0.05))]
        resistance = sorted_prices[min(n - 1, int(n * 0.95))]
        return (round(support, 2), round(resistance, 2), 0.3, 0.3)

    def _extrema_range(self) -> tuple[float, float, float, float]:
        """Use raw candle highs/lows to avoid over-compressed startup ranges."""
        if not self._bar_extremes or len(self._bar_extremes) < 5:
            return (0.0, 0.0, 0.0, 0.0)
        return self._extrema_range_from_pairs(self._bar_extremes[-self.auto_lookback:])

    def _extrema_range_from_pairs(
        self,
        bar_pairs: list[tuple[float, float]],
    ) -> tuple[float, float, float, float]:
        """Build a range from explicit high/low pairs."""
        if not bar_pairs or len(bar_pairs) < 5:
            return (0.0, 0.0, 0.0, 0.0)
        highs = [high for high, _ in bar_pairs if high > 0]
        lows = [low for _, low in bar_pairs if low > 0]
        if not highs or not lows:
            return (0.0, 0.0, 0.0, 0.0)
        support = min(lows)
        resistance = max(highs)
        return (round(support, 2), round(resistance, 2), 0.25, 0.25)

    def _effective_min_profit_per_trade(self, reference_price: float) -> float:
        """
        Relax the absolute spread floor for lower-priced stocks while keeping
        the configured ceiling for higher-priced names.
        """
        try:
            price = float(reference_price)
        except (TypeError, ValueError):
            price = 0.0
        configured = max(0.0, float(self.min_profit_per_trade or 0.0))
        if price <= 0:
            return configured
        dynamic_floor = max(0.35, min(configured or 1.0, price * 0.04))
        return round(dynamic_floor, 2)

    def _is_range_tradeable(self, support: float, resistance: float) -> bool:
        """Reject ranges that are too narrow to cover slippage and fees."""
        if support <= 0 or resistance <= support:
            return False
        spread = resistance - support
        spread_pct = (spread / support * 100) if support > 0 else 0.0
        min_profit = self._effective_min_profit_per_trade(support)
        if spread < min_profit:
            return False
        if spread_pct < self.min_range_width_pct:
            return False
        return True

    def _update_trend(self) -> None:
        """Calculate SMA and determine trend direction."""
        period = self.trend_ma_period
        if len(self._price_history) < period:
            return

        recent = self._price_history[-period:]
        self._trend_ma = sum(recent) / len(recent)

        # Determine direction: compare current price to MA
        current = self._price_history[-1]
        pct_from_ma = (current - self._trend_ma) / self._trend_ma * 100

        if pct_from_ma > self.trend_min_strength:
            self._trend_direction = "up"
        elif pct_from_ma < -self.trend_min_strength:
            self._trend_direction = "down"
        else:
            self._trend_direction = "neutral"

    @property
    def trend_ma(self) -> Optional[float]:
        """Current moving average value."""
        return self._trend_ma

    @property
    def trend_direction(self) -> str:
        """Current trend direction: 'up', 'down', or 'neutral'."""
        return self._trend_direction

    def get_trend_info(self) -> dict:
        """Get trend state for display."""
        if self._trend_ma is None or not self._price_history:
            return {"direction": "neutral", "ma": 0, "pct_from_ma": 0, "active": False}
        current = self._price_history[-1]
        pct = (current - self._trend_ma) / self._trend_ma * 100 if self._trend_ma > 0 else 0
        return {
            "direction": self._trend_direction,
            "ma": round(self._trend_ma, 4),
            "pct_from_ma": round(pct, 2),
            "active": self.trend_enabled,
        }

    def update_auto_range(self, ohlcv_data=None) -> bool:
        """
        Recalculate auto range using Volume Profile.

        Uses volume-weighted price levels to find where real money
        changed hands — much more reliable than simple price extremes.
        Falls back to percentile method if no volume data available.
        """
        # ── Option A: Volume Profile ──
        if self._volume_profile and len(self._volume_profile) >= 10:
            supp, res, supp_conf, res_conf = self._calc_volume_weighted_range()
            if self._is_range_tradeable(supp, res):
                self.apply_auto_range(supp, res, confidence=supp_conf, source="volume_profile")

                spread = res - supp
                spread_pct = (spread / supp * 100) if supp > 0 else 0
                logger.info(
                    f"[Volume Range] Supp=${supp:.2f} Res=${res:.2f} "
                    f"Spread={spread:.2f} ({spread_pct:.1f}%) "
                    f"Conf={supp_conf:.1f}/{res_conf:.1f}"
                )
                return True

        # ── Option B: Percentile Fallback ──
        if self._price_history and len(self._price_history) >= 10:
            supp, res, _, _ = self._percentile_range()
            if self._is_range_tradeable(supp, res):
                self.apply_auto_range(supp, res, confidence=0.3, source="percentile")
                spread = res - supp
                spread_pct = (spread / supp * 100) if supp > 0 else 0
                logger.info(
                    f"[Pct Range] Supp=${supp:.2f} Res=${res:.2f} "
                    f"Spread={spread:.2f} ({spread_pct:.1f}%)"
                )
                return True

        # ── Option C: raw candle envelope fallback ──
        supp, res, supp_conf, _ = self._extrema_range()
        if self._is_range_tradeable(supp, res):
            self.apply_auto_range(supp, res, confidence=supp_conf, source="bar_extrema")
            spread = res - supp
            spread_pct = (spread / supp * 100) if supp > 0 else 0
            logger.info(
                f"[Extrema Range] Supp=${supp:.2f} Res=${res:.2f} "
                f"Spread={spread:.2f} ({spread_pct:.1f}%)"
            )
            return True

        return False

    def seed_from_ohlcv(self, candles: list) -> bool:
        """
        Seed the auto range from historical OHLCV data.
        Uses high/low prices (not just close) for a wider, more realistic range.
        Call this once at startup to immediately have a valid range.

        Args:
            candles: list of OHLCV objects with .high, .low, .close attributes
        Returns:
            True if range was seeded successfully
        """
        if not candles or len(candles) < 5:
            return False

        # Use recent candles for initial range
        recent = candles[-self.auto_lookback:] if len(candles) > self.auto_lookback else candles

        closes = [c.close for c in recent]

        # Seed price history
        for c in closes:
            self._price_history.append(c)

        # Seed volume profile from OHLCV bars
        for c in recent:
            mid = (c.high + c.low) / 2
            self._volume_profile.append((mid, float(c.volume or 0)))
            self._bar_extremes.append((float(c.high or 0.0), float(c.low or 0.0)))

        full_extrema_pairs = [
            (float(c.high or 0.0), float(c.low or 0.0))
            for c in candles
        ]
        candidates = []
        if self._volume_profile and len(self._volume_profile) >= 10:
            candidates.append(("seeded_history",) + self._calc_volume_weighted_range())
        candidates.append(("bar_extrema_seed",) + self._extrema_range())
        if len(full_extrema_pairs) > len(self._bar_extremes):
            candidates.append(("multi_day_extrema_seed",) + self._extrema_range_from_pairs(full_extrema_pairs))
        candidates.append(("percentile",) + self._percentile_range())

        selected = None
        failure = None
        for source, supp, res, supp_conf, res_conf in candidates:
            if self._is_range_tradeable(supp, res):
                selected = (source, supp, res, supp_conf, res_conf)
                break
            failure = (supp, res)

        if selected is None:
            supp, res = failure or (0.0, 0.0)
            spread = max(0.0, res - supp)
            spread_pct = (spread / supp * 100) if supp > 0 else 0.0
            min_profit = self._effective_min_profit_per_trade(supp)
            logger.warning(
                "Could not seed auto range for %s: spread=$%.2f (%.1f%%), min=$%.2f / %.1f%%",
                self.ticker,
                spread,
                spread_pct,
                min_profit,
                self.min_range_width_pct,
            )
            return False

        source, supp, res, supp_conf, res_conf = selected
        self.apply_auto_range(supp, res, confidence=supp_conf, source=source)

        spread = res - supp
        spread_pct = (spread / supp * 100) if supp > 0 else 0

        method = (
            "Volume Profile"
            if source == "seeded_history"
            else "Multi-day Extrema"
            if source == "multi_day_extrema_seed"
            else "Extrema"
            if source == "bar_extrema_seed"
            else "Percentile"
        )
        logger.info(
            f"[{method}] Seeded: ${supp:.2f}−${res:.2f} "
            f"({spread:.1f}% spread, conf={supp_conf:.1f})"
        )
        return True

    def record_entry(self, entry_price: float) -> None:
        """Record an entry price for quick stop tracking."""
        self._entry_price = entry_price

    def clear_entry(self) -> None:
        """Clear entry tracking after exit."""
        self._entry_price = None

    def needs_auto_refresh(self) -> bool:
        """Check if auto range needs refreshing."""
        if self.mode != "auto":
            return False
        if self._last_auto_refresh is None:
            return True
        elapsed = (datetime.now() - self._last_auto_refresh).total_seconds()
        return elapsed >= (self.auto_refresh_minutes * 60)

    def evaluate(self, current_price: float, has_position: bool = False) -> Signal:
        """
        Evaluate current price against the range and generate a signal.

        Args:
            current_price: Latest price
            has_position: Whether we currently hold a position

        Returns:
            Signal with action to take
        """
        support = self.support
        resistance = self.resistance

        # --- Validation ---
        if support is None or resistance is None:
            return Signal(
                type=SignalType.HOLD, ticker=self.ticker,
                price=current_price, support=0, resistance=0,
                reason="Range not configured (set support/resistance in config.yaml)",
                confidence=0,
            )

        if support <= 0 or resistance <= support:
            return Signal(
                type=SignalType.HOLD, ticker=self.ticker,
                price=current_price, support=support, resistance=resistance,
                reason="Invalid range (support must be < resistance)",
                confidence=0,
            )

        # --- Trend filter ---
        trend_info = self.get_trend_info()
        trend_blocked = False
        pct_ma = trend_info["pct_from_ma"]
        trend_block_threshold = max(self.trend_min_strength, self.TREND_BLOCK_MIN_PCT_FROM_MA)
        if self.trend_enabled and self._trend_direction == "down" and not has_position:
            # Only block when the stock is clearly below its moving average.
            trend_blocked = pct_ma <= -trend_block_threshold

        # --- Calculate distances ---
        dist_to_support = (current_price - support) / support * 100  # % above support
        dist_to_resistance = (resistance - current_price) / resistance * 100  # % below resistance
        tol = self.tolerance_pct

        # --- Signal logic ---
        if has_position:
            # Quick stop: exit if price dropped too fast (reversal protection)
            if self._entry_price and current_price < self._entry_price * (1 - self.quick_stop_pct / 100):
                drop_pct = (self._entry_price - current_price) / self._entry_price * 100
                return Signal(
                    type=SignalType.STOP_LOSS,
                    ticker=self.ticker,
                    price=current_price,
                    support=support,
                    resistance=resistance,
                    reason=f"QUICK STOP: Price dropped {drop_pct:.1f}% from entry ${self._entry_price:.2f} (limit: {self.quick_stop_pct}%)",
                    confidence=1.0,
                )

            # We hold a position → look to sell
            if dist_to_resistance <= tol:
                return Signal(
                    type=SignalType.SELL,
                    ticker=self.ticker,
                    price=current_price,
                    support=support,
                    resistance=resistance,
                    reason=f"Price ${current_price:.2f} near resistance ${resistance:.2f} "
                           f"({dist_to_resistance:.1f}% below), holding position",
                    confidence=min(1.0, 1.0 - dist_to_resistance / tol),
                )
            # Check stop loss
            if current_price < support * (1 - 0.02):  # Hard 2% below support
                return Signal(
                    type=SignalType.STOP_LOSS,
                    ticker=self.ticker,
                    price=current_price,
                    support=support,
                    resistance=resistance,
                    reason=f"STOP LOSS: Price ${current_price:.2f} broke below support ${support:.2f}",
                    confidence=1.0,
                )
        else:
            # No position → look to buy
            position_in_range = ((current_price - support) / (resistance - support) * 100) if resistance != support else 50
            in_buy_zone = position_in_range <= self.BUY_ZONE_MAX_POSITION_PCT
            if 0 <= dist_to_support <= tol or in_buy_zone:
                if trend_blocked:
                    return Signal(
                        type=SignalType.TREND_BLOCK,
                        ticker=self.ticker,
                        price=current_price,
                        support=support,
                        resistance=resistance,
                        reason=f"BUY blocked: downtrend (price {pct_ma:+.1f}% vs MA{self.trend_ma_period}=${self._trend_ma:.2f})",
                        confidence=0.0,
                    )

                # Minimum profit check: ensure the spread covers costs
                spread_dollars = resistance - support
                spread_pct = (spread_dollars / support * 100) if support > 0 else 0.0
                min_profit = self._effective_min_profit_per_trade(support)
                est_profit = (resistance - current_price) / current_price * 100  # % return
                commission_pct = 0.12  # ~0.12% round-trip commission on 2 trades

                if spread_dollars < min_profit or spread_pct < self.min_range_width_pct:
                    return Signal(
                        type=SignalType.HOLD,
                        ticker=self.ticker,
                        price=current_price,
                        support=support,
                        resistance=resistance,
                        reason=(
                            f"Range too narrow (spread ${spread_dollars:.2f}, {spread_pct:.1f}%) "
                            f"< min ${min_profit:.2f} / {self.min_range_width_pct:.1f}%"
                        ),
                        confidence=0.0,
                    )

                # Support confidence check: only buy at "real" supports (volume-tested)
                if self.mode != "manual" and self._support_confidence < 0.10:
                    return Signal(
                        type=SignalType.HOLD,
                        ticker=self.ticker,
                        price=current_price,
                        support=support,
                        resistance=resistance,
                        reason=f"Weak support (conf={self._support_confidence:.1f}), waiting for volume confirmation",
                        confidence=0.1,
                    )

                if est_profit < commission_pct * 2 + 0.1:  # need at least 0.34% above costs
                    return Signal(
                        type=SignalType.HOLD,
                        ticker=self.ticker,
                        price=current_price,
                        support=support,
                        resistance=resistance,
                        reason=f"Spread too narrow for profit (est return {est_profit:.1f}% < costs {commission_pct*2:.1f}%)",
                        confidence=0.0,
                    )

                return Signal(
                    type=SignalType.BUY,
                    ticker=self.ticker,
                    price=current_price,
                    support=support,
                    resistance=resistance,
                    reason=(
                        f"Price ${current_price:.2f} "
                        f"{'near support' if dist_to_support <= tol else 'in lower range'} "
                        f"(pos {position_in_range:.0f}%), est return {est_profit:.1f}%"
                    ),
                    confidence=max(0.0, min(1.0, 1.0 - dist_to_support / tol)),
                )

        # --- Default: HOLD ---
        position_in_range = ((current_price - support) / (resistance - support) * 100) if resistance != support else 50
        trend_note = ""
        if trend_blocked:
            trend_note = f" [TREND FILTER: downtrend, price {pct_ma:+.1f}% vs MA{self.trend_ma_period}]"
        return Signal(
            type=SignalType.HOLD,
            ticker=self.ticker,
            price=current_price,
            support=support,
            resistance=resistance,
            reason=f"In range at {position_in_range:.0f}%, "
                   f"dist to support={dist_to_support:.1f}%, dist to resistance={dist_to_resistance:.1f}%"
                   + trend_note,
            confidence=0.5,
        )
