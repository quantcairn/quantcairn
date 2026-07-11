# Dynamic Range Inventory Strategy - Test Plan

## 1. Purpose

This test plan covers the proposed strategy upgrade without changing production behavior in Phase 1.

The goal is to validate the design surface before implementation.

---

## 2. Testing Principles

1. Default behavior must remain unchanged when feature flags are off.
2. Any new logic must fail closed.
3. No test may depend on future data.
4. No test may call real broker endpoints.
5. No test may submit real orders.
6. Paper-only lifecycle tests are allowed.

---

## 3. Required Test Categories

### A. Dynamic Range

- ATR-based range calculation
- range too narrow => no trade
- insufficient warm-up => fail closed
- support / resistance buffers
- volatility expansion widens range
- volatility contraction narrows range

### B. Entry Layers

- max layers enforced
- duplicate layer cannot be submitted
- pending BUY blocks duplicate order
- layer spacing must be respected
- layer quantity 0 does not call broker

### C. Exit Layers

- each layer exits once
- pending SELL blocks duplicate order
- SELL quantity cannot exceed broker position
- orphan reduce-only exit remains allowed
- exit state persists across restart

### D. Inventory Awareness

- inventory ratio reduces new BUY size
- inventory above 80% blocks new entries
- leveraged ETFs use lower caps
- cash reserve is preserved
- margin buying power is not used for ordinary BUY sizing

### E. Trend Guard

- strong trend blocks new BUY
- trend guard still allows SELL
- SOXS-specific bearish-index protection
- cooldown after trend break
- invalid range triggers block

### F. Cost Filter

- expected profit too low blocks trade
- spread too wide blocks trade
- costs exceed edge blocks trade
- only positive net-edge trades are allowed

### G. Risk / Portfolio Safety

- reduce-only still blocks BUY at API level
- fallback live remains blocked
- PortfolioManager still enforces exposure caps
- broker API is not called when a guard fails

### H. Persistence and Recovery

- state file written atomically
- restart restores layers
- broker vs local mismatch fails closed
- stale / invalid state does not open new positions

### I. Baseline Compatibility

- feature flags off => existing behavior unchanged
- existing LongBridge safety tests still pass
- existing dashboard-only tests still pass

---

## 4. Suggested Test Files

### New tests to add later

- `tests/test_dynamic_range.py`
- `tests/test_entry_layers.py`
- `tests/test_exit_layers.py`
- `tests/test_inventory_aware_sizing.py`
- `tests/test_trend_guard.py`
- `tests/test_trade_cost_filter.py`
- `tests/test_strategy_state_store.py`
- `tests/test_walk_forward_backtester.py`

### Existing tests that should continue to pass

- `tests/test_longbridge_broker.py`
- `tests/test_longbridge_sandbox_connection.py`
- `tests/test_longbridge_sandbox_lifecycle.py`
- `tests/test_trading_engine_pending.py`
- `tests/test_trading_engine_fallback_paper.py`
- `tests/test_trading_engine_portfolio_guard.py`
- `tests/test_order_rejection.py`
- `tests/test_orphan_reduce_only.py`
- `tests/test_live_guard.py`
- `tests/test_portfolio_risk.py`
- `tests/test_position_sizing.py`
- `tests/test_risk_allocator.py`

---

## 5. Detailed Test Matrix

| Area | Case | Expected Result |
|---|---|---|
| Dynamic range | insufficient bars | fail closed |
| Dynamic range | range width too small | no trade |
| Dynamic range | support/resistance valid | range usable |
| Entry layers | layer 1 filled | state saved |
| Entry layers | layer 1 duplicate | blocked |
| Entry layers | pending BUY exists | blocked |
| Entry layers | max layers reached | blocked |
| Inventory | ratio 0% - 30% | full base size |
| Inventory | ratio 60% - 80% | reduced size |
| Inventory | ratio > 80% | no new BUY |
| Trend guard | strong trend | BUY blocked |
| Trend guard | strong trend with position | SELL allowed |
| Cost filter | edge below cost | blocked |
| Cost filter | spread too wide | blocked |
| Persistence | restart recovery | state restored |
| Safety | reduce-only true | BUY blocked |
| Safety | fallback live | BUY blocked |
| Compatibility | flags off | old behavior preserved |

---

## 6. Mock / Fixture Design

Recommended test fixtures:

- synthetic OHLCV sequences
- synthetic trend regimes
- synthetic broker account snapshots
- synthetic pending-order state
- synthetic state-store JSON

These fixtures keep the tests deterministic and fast.

---

## 7. Negative Testing Requirements

The design must explicitly test that the following do **not** happen:

- no buy sizing from margin buying power
- no repeated BUY on the same layer
- no repeated SELL on the same layer
- no live order submission from a paper-only test
- no accidental enablement when a flag is missing
- no future-bar leakage into indicators

---

## 8. CI / Regression Gate

Before any later implementation is merged:

1. Run the full existing regression set.
2. Run the new strategy tests.
3. Verify LongBridge safety regressions still pass.
4. Verify dashboard-only tests still pass.
5. Verify baseline behavior unchanged with all flags disabled.

---

## 9. Phase 1 Exit Criteria

Phase 1 is complete when:

- design docs are written
- config drafts are defined
- persistence schema is defined
- backtest plan is defined
- test plan is defined
- no production behavior has changed
