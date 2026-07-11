# Dynamic Range Inventory Strategy - Backtest Plan

## 1. Purpose

Build a reproducible backtest framework for the proposed Dynamic Range Inventory Strategy.

This phase does **not** change production trading behavior.
It only defines how to compare the current strategy against proposed variants.

---

## 2. Backtest Versions

### Baseline

Current repository behavior as-is.

### Version A

- Dynamic range
- Single-entry BUY
- Single-exit SELL
- No inventory-aware sizing
- No trend guard

### Version B

- Dynamic range
- Layered entry
- Layered exit
- Still no inventory-aware sizing
- Still no trend guard

### Version C

- Version B
- Inventory-aware sizing
- Trend guard
- Time stop
- Trade cost filter

---

## 3. Comparison Rules

All versions must use:

- Same historical period
- Same initial capital
- Same universe
- Same commission model
- Same slippage model
- Same spread assumptions
- Same paper-like fill logic

Do not compare versions using different data slices.

---

## 4. Required Market Regimes

Backtest must include at least:

- SOXS
- A normal low-price stock
- A non-leveraged ETF
- Range-bound period
- Strong uptrend period
- Strong downtrend period
- High-volatility period

This is needed to reduce overfitting to SOXS-only behavior.

---

## 5. Data Requirements

### Required inputs

- OHLCV bars
- splits / dividends adjustment if available
- bid / ask spread estimate or proxy
- commission and platform fee assumptions
- symbol metadata
- benchmark or sector trend proxy

### No future data

The backtest engine must never use:

- future bars
- future highs / lows
- future trend labels
- future optimal range values

---

## 6. Walk-Forward Structure

Use time-sliced evaluation:

1. Train
2. Validation
3. Out-of-sample
4. Roll forward

Example layout:

```text
2024-Q1 train → 2024-Q2 validation → 2024-Q3 test
2024-Q2 train → 2024-Q3 validation → 2024-Q4 test
```

### Why

This helps identify whether parameters are stable or only fit one period.

---

## 7. Evaluation Metrics

Primary metrics:

- Total return
- Annualized return
- Max drawdown
- Sharpe
- Sortino
- Calmar
- Win rate
- Profit factor
- Average win
- Average loss
- Exposure
- Turnover
- Trade count
- Average holding time
- Longest losing streak
- Return / drawdown ratio

### Interpretation

- A higher raw return is not enough.
- A strategy with slightly lower return but much lower drawdown and turnover is preferable.

---

## 8. Objective Function

The optimization objective should not be “max return only”.

Recommended score:

```text
score =
  annualized_return
  - drawdown_penalty
  + sharpe_weight
  + calmar_weight
  - turnover_penalty
  - instability_penalty
```

### Constraint

No parameter set may be selected if it only wins because it:

- increases leverage
- loosens stop loss
- removes cooldown
- bypasses existing guards

---

## 9. Parameter Stability Plan

Do not report only the single best point.

Instead:

- Search bounded ranges
- Use fixed random seeds
- Save every run
- Report the stable parameter zone
- Mark overfit candidates

### Example report outputs

- Best single configuration
- Top 10 stable configurations
- Parameter sensitivity heatmap
- Overfit risk notes

---

## 10. Backtest Engine Responsibilities

The backtest engine should model:

- layered entries
- layered exits
- pending-order blocking
- cash-only sizing
- reduce-only behavior
- trend guard
- time stop
- broker fill assumptions

It should not:

- use production broker APIs
- submit real orders
- depend on live market state

---

## 11. Suggested Backtest Artifacts

For each run, persist:

- config snapshot
- symbol universe
- parameter set
- date range
- transaction log
- equity curve
- drawdown curve
- per-symbol trade list
- per-layer state transitions
- risk events

Suggested output directory:

```text
reports/backtest/
```

---

## 12. Acceptance Criteria

Version C is only considered better if it improves **risk-adjusted** results without weakening safety.

Minimum acceptance:

- Sharpe not worse than baseline
- Calmar not worse than baseline
- Max drawdown improved or at least not meaningfully worse
- Trade count and turnover remain controlled
- No hidden leverage expansion
- No violation of reduce-only / portfolio / buying-power rules

---

## 13. Reproducibility Rules

Backtest must be deterministic:

- fixed random seed
- fixed universe snapshot
- fixed parameter grid
- fixed cost assumptions

If a run cannot be reproduced, it is not acceptable for decision-making.

---

## 14. Phase 1 Deliverables

For Phase 1, only produce:

- Backtest engine design
- Parameter grid design
- Walk-forward design
- Metrics definition
- Stability report format

No production code changes.
