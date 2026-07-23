"""Market Regime Classifier — scores BULL / SIDEWAYS / BEAR / RISK_OFF.

Rule-based multi-factor scoring from index-level indicators.
Each regime gets a 0-100 score based on indicator alignment.
Highest score wins.  Signals list explains the classification.
"""

from __future__ import annotations

from src.regime.models import RegimeState, REGIME_TYPES


def _safe_snap(state: RegimeState, key: str = "SPY") -> dict:
    d = state.indices.get(key, {})
    return d if isinstance(d, dict) else {}


def classify(state: RegimeState) -> None:
    """Populate state.regime, .confidence, .signals, and per-regime scores."""
    spy = _safe_snap(state, "SPY")
    qqq = _safe_snap(state, "QQQ")
    iwm = _safe_snap(state, "IWM")

    bs, b_sig = _bull_score(state, spy, qqq, iwm)
    ss, s_sig = _sideways_score(state, spy, qqq, iwm)
    bes, be_sig = _bear_score(state, spy, qqq, iwm)
    ros, ro_sig = _risk_off_score(state, spy, qqq, iwm)

    state.bull_score = bs
    state.sideways_score = ss
    state.bear_score = bes
    state.risk_off_score = ros

    scores = {"BULL": (bs, b_sig), "SIDEWAYS": (ss, s_sig),
              "BEAR": (bes, be_sig), "RISK_OFF": (ros, ro_sig)}
    winner = max(scores, key=lambda k: scores[k][0])

    state.regime = winner
    state.signals = scores[winner][1]

    # ── Confidence ───────────────────────────────────────────────────────
    sorted_scores = sorted([bs, ss, bes, ros], reverse=True)
    gap = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else 0.0
    data_available = bool(state.indices)
    score_confidence = min(1.0, gap / 30.0) if gap > 0 else 0.25
    data_confidence = 0.8 if len(state.indices) >= 3 else (0.5 if len(state.indices) >= 2 else 0.2)
    vix_confidence = 0.0
    if state.vix > 0:
        if state.vix <= 20:
            vix_confidence = 0.9
        elif state.vix <= 30:
            vix_confidence = 0.6
        else:
            vix_confidence = 0.3
    state.confidence = round(
        0.40 * score_confidence + 0.35 * data_confidence + 0.25 * vix_confidence, 4
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Bull scoring — strong uptrend, above moving averages, positive momentum
# ═══════════════════════════════════════════════════════════════════════════════

def _bull_score(state: RegimeState, spy: dict, qqq: dict, iwm: dict) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []

    # Price vs MA alignment
    above_20 = 0; above_50 = 0; above_200 = 0
    for label, snap in [("SPY", spy), ("QQQ", qqq), ("IWM", iwm)]:
        if not snap:
            continue
        if snap.get("price_vs_ma20", 1.0) > 1.0:
            above_20 += 1
        if snap.get("price_vs_ma50", 1.0) > 1.0:
            above_50 += 1
        if snap.get("price_vs_ma200", 1.0) > 1.0:
            above_200 += 1

    if above_20 >= 2:
        score += 20.0
    if above_50 >= 2:
        score += 20.0
    if above_200 >= 2:
        score += 15.0

    if above_20 > 0:
        signals.append(f"bull_above_ma20:{above_20}/3")
    else:
        signals.append("bull_no_ma20_support")

    # Market return
    if state.market_return_20d >= 3.0:
        score += 15.0
        signals.append("strong_20d_return")
    elif state.market_return_20d > 0:
        score += 8.0

    if state.market_return_5d > 0:
        score += min(10.0, state.market_return_5d)

    # VIX
    if state.vix > 0:
        if state.vix <= 18:
            score += 10.0
            signals.append("low_vix_bullish")
        elif state.vix <= 25:
            score += 5.0
        else:
            signals.append("elevated_vix")

    # Momentum — check SPY RSI
    if spy.get("rsi", 50.0) >= 55:
        score += 10.0

    return round(min(100.0, score), 2), signals


# ═══════════════════════════════════════════════════════════════════════════════
# Sideways scoring — range-bound, near MAs, low volatility
# ═══════════════════════════════════════════════════════════════════════════════

def _sideways_score(state: RegimeState, spy: dict, qqq: dict, iwm: dict) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []

    near_ma20 = 0
    for label, snap in [("SPY", spy), ("QQQ", qqq), ("IWM", iwm)]:
        if not snap:
            continue
        pv20 = snap.get("price_vs_ma20", 1.0)
        if 0.97 <= pv20 <= 1.03:
            near_ma20 += 1
            score += 10.0

    if near_ma20 >= 2:
        signals.append(f"near_ma20:{near_ma20}/3")
    else:
        signals.append("far_from_ma20")

    # Tight range returns
    if abs(state.market_return_20d) <= 3.0:
        score += 15.0
        signals.append("tight_20d_range")
    if abs(state.market_return_5d) <= 1.5:
        score += 10.0

    # Low volatility (ATR)
    atr_vals = [snap.get("atr_pct", 10.0) for snap in [spy, qqq, iwm] if snap]
    avg_atr = round(sum(atr_vals) / max(len(atr_vals), 1), 2) if atr_vals else 10.0
    if avg_atr <= 1.5:
        score += 10.0
    elif avg_atr <= 2.5:
        score += 5.0
    else:
        signals.append("high_atr")

    # VIX
    if state.vix > 0 and state.vix <= 25:
        score += 10.0
        signals.append("normal_vix")
    elif state.vix > 25:
        signals.append("elevated_vix_noise")

    # RSI 40-60
    spy_rsi_s = spy.get("rsi", 50.0)
    if spy_rsi_s and 40 <= spy_rsi_s <= 60:
        score += 10.0

    # Volume stable
    vol_stable = sum(1 for s in [spy, qqq, iwm] if s and 0.7 <= s.get("volume_sma20_ratio", 1.0) <= 1.3)
    if vol_stable >= 2:
        score += 5.0

    return round(min(100.0, score), 2), signals


# ═══════════════════════════════════════════════════════════════════════════════
# Bear scoring — below MAs, negative momentum, broad weakness
# ═══════════════════════════════════════════════════════════════════════════════

def _bear_score(state: RegimeState, spy: dict, qqq: dict, iwm: dict) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []

    below_20 = 0; below_50 = 0; below_200 = 0
    for label, snap in [("SPY", spy), ("QQQ", qqq), ("IWM", iwm)]:
        if not snap:
            continue
        if snap.get("price_vs_ma20", 1.0) < 1.0:
            below_20 += 1
        if snap.get("price_vs_ma50", 1.0) < 1.0:
            below_50 += 1
        if snap.get("price_vs_ma200", 1.0) < 1.0:
            below_200 += 1

    if below_20 >= 2:
        score += 20.0
    if below_50 >= 2:
        score += 20.0
    if below_200 >= 2:
        score += 20.0

    if below_200 >= 1:
        signals.append(f"below_ma200:{below_200}/3")

    # Market return
    if state.market_return_20d <= -3.0:
        score += 15.0
        signals.append("deep_20d_loss")
    elif state.market_return_20d < 0:
        score += 8.0

    if state.market_return_5d < 0:
        score += min(10.0, abs(state.market_return_5d))

    # Small caps weaker (IWM relative)
    sp20 = spy.get("return_20d_pct", 0.0) if spy else 0.0
    iw20 = iwm.get("return_20d_pct", 0.0) if iwm else 0.0
    if iw20 < sp20:
        score += 5.0
        signals.append("small_caps_underperforming")

    # Elevated VIX
    if state.vix > 25:
        score += 10.0
        signals.append("vix_elevated_bearish")

    # RSI
    if spy.get("rsi", 50.0) <= 40:
        score += 5.0

    return round(min(100.0, score), 2), signals


# ═══════════════════════════════════════════════════════════════════════════════
# Risk-off — high VIX, broad declines, high volatility, flight to safety
# ═══════════════════════════════════════════════════════════════════════════════

def _risk_off_score(state: RegimeState, spy: dict, qqq: dict, iwm: dict) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []

    # VIX spike
    if state.vix >= 30:
        score += 30.0
        signals.append("vix_30_plus_risk_off")
    elif state.vix >= 25:
        score += 15.0
        signals.append("vix_elevated")
    if state.vix_change_pct > 10.0:
        score += 15.0
        signals.append("vix_spiking")

    # Broad declines
    below_200_count = 0
    for snap in [spy, qqq, iwm]:
        if snap and snap.get("price_vs_ma200", 1.0) < 1.0:
            below_200_count += 1
    if below_200_count >= 2:
        score += 20.0
        signals.append(f"broad_below_ma200:{below_200_count}/3")

    # Sharp recent selloff
    if state.market_return_5d <= -3.0:
        score += 15.0
        signals.append("sharp_5d_selloff")
    if state.market_return_20d <= -5.0:
        score += 10.0

    # Volatility spike
    atr_vals = [snap.get("atr_pct", 0.0) for snap in [spy, qqq, iwm] if snap]
    avg_atr = round(sum(atr_vals) / max(len(atr_vals), 1), 2) if atr_vals else 0.0
    if avg_atr >= 3.0:
        score += 10.0
        signals.append(f"high_atr:{avg_atr}%")

    return round(min(100.0, score), 2), signals
