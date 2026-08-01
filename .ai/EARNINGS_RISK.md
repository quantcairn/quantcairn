# QuantCairn Earnings Risk Contract

> Status: Draft contract for v0.12.9 planning.
> Scope: Read-only earnings event risk semantics.
> Safety: This contract must not change selector behavior, trading gates, broker behavior, or execution logic.

## 1. Purpose

QuantCairn currently carries several earnings-related fields across scoring, trade filtering, reporting, and provider layers, but their meaning is fragmented:

- `earnings_score` is currently used as a provider / factor score.
- `earnings_within_7_days` and `earnings_soon` are currently used as proximity hints in ranking.
- `earnings_within_days` is currently used as a trade filter input.
- There is no single, canonical, read-only earnings event contract.

This document freezes a single, backward-compatible contract for a future `earnings_info` field so the system can express earnings event risk consistently without changing existing safety boundaries.

This contract is for **event awareness**, not execution authority.

---

## 2. Ownership

### Canonical owner

`earnings_info` is a **SelectionBundle-derived, read-only candidate metadata field**.

It is not a broker input, not a trading-engine input, and not an order-generation input.

### Recommended source of truth

A dedicated earnings event provider or enrichment layer should produce raw earnings event data, then the selection pipeline should normalize it into the bundle as a derived field.

Recommended ownership chain:

1. Provider / event enrichment layer discovers raw earnings data
2. Selector normalizes the data into candidate metadata
3. SelectionBundle serializes the derived read-only view
4. Dashboard / notifier consume it for display only

### Not the owner

The following must never own or mutate earnings risk semantics:

- broker
- trading engine
- order creation
- live gate
- paper gate
- config writer
- scheduler

---

## 3. Current Logic Audit

### Existing earnings-related fields in the codebase

| Field | Current meaning | Typical producer | Current consumers |
|---|---|---|---|
| `earnings_score` | Analysis/factor score, not calendar risk | Provider layer (for example `finrobot_provider.py`; other providers may pass it through) | `scoring`, `data_quality`, `selection_report`, `integration`, ranking/score aggregation |
| `earnings_within_7_days` | Boolean proximity hint | Candidate/market metadata layer | `candidate_ranking` |
| `earnings_soon` | Boolean proximity hint / alias | Candidate/market metadata layer | `candidate_ranking` |
| `earnings_within_days` | Numeric proximity hint | Candidate / market context / trade-filter context | `trade_filter` |
| `earnings_risk_score` | Not found as a canonical field in current codebase | N/A | N/A |

### Semantics conflict today

- `earnings_score` is a generic factor score, but it can be misread as event risk.
- `earnings_within_7_days` / `earnings_soon` are boolean aliases of the same idea and should not become the canonical contract.
- `earnings_within_days` is a useful numeric driver, but it is not a complete contract because it does not express source, timestamp, confidence, or time zone.

Conclusion: the current schema is functional but not normalized.

---

## 4. Data Contract

### Canonical field

```json
"earnings_info": {
  "symbol": "AAPL",
  "earnings_date": "2026-08-07",
  "earnings_time": "BMO",
  "market_timezone": "America/New_York",
  "trading_days_to_earnings": 5,
  "earnings_risk_level": "MEDIUM",
  "source": "provider_name_or_event_feed",
  "updated_at": "2026-08-01T08:00:00Z",
  "confidence": 0.86
}
```

### Required fields

These fields are required for the canonical contract when data is available:

- `symbol`
- `earnings_date`
- `trading_days_to_earnings`
- `earnings_risk_level`
- `source`
- `updated_at`

### Recommended fields

- `earnings_time`
- `market_timezone`
- `confidence`

### Future extension fields

These may be added later without breaking the contract:

- `source_detail`
- `event_type`
- `pre_market_flag`
- `post_market_flag`
- `confirmed_by_provider`
- `earnings_surprise_direction`
- `guidance_flag`
- `next_earnings_date`

### Contract rules

- Missing data must be representable.
- Partial data must be representable.
- `earnings_info` is optional.
- `earnings_info` must never be required for the pipeline to run.
- `earnings_info` must never be used to fabricate broker/trading state.

---

## 5. Risk Levels

### Recommended enum

- `VERY_HIGH`
- `HIGH`
- `MEDIUM`
- `LOW`
- `UNKNOWN`

### Recommended thresholds

| Level | Trading days to earnings | Meaning |
|---|---:|---|
| `VERY_HIGH` | today / next trading day | Event is immediate; near-term gap risk is elevated |
| `HIGH` | 2 trading days or fewer | Event is imminent; risk should be visible in ranking / display |
| `MEDIUM` | 3-7 trading days | Event is near enough to matter, but not immediate |
| `LOW` | more than 7 trading days | Event risk exists but is not a near-term concern |
| `UNKNOWN` | unavailable / untrusted | No reliable event data is present |

### Evaluation guidance

- Use trading days, not calendar days.
- Use the market calendar / exchange timezone, not local machine time.
- If a provider returns a date but confidence is low or the date cannot be normalized, use `UNKNOWN`.
- If the earnings date has already passed but the provider has not refreshed, keep the last known event only if it is explicitly marked stale; otherwise fall back to `UNKNOWN`.

---

## 6. Pipeline Integration

### Recommended placement

The best fit is **data enrichment plus soft scoring awareness**:

1. **Data enrichment**: attach `earnings_info` before or alongside candidate scoring
2. **Candidate ranking penalty**: use event proximity as a soft risk input, not as a hard execution gate by default
3. **Trade filter**: optionally read the field for diagnostics or configurable policy checks, but never treat it as a broker/execution decision
4. **SelectionBundle metadata**: serialize the derived view for downstream consumers
5. **Dashboard display**: show event date / countdown / risk level
6. **Telegram notification**: surface the event risk for humans, not machines

### Why not Universe Filter

Universe filtering is too early and too coarse. Earnings risk is event-specific and date-sensitive; dropping a symbol from the universe on this basis would hide research candidates and conflate event risk with symbol eligibility.

### Why not broker / trading engine

Those layers must never depend on a soft event-awareness field.

### Recommended usage by stage

| Stage | May read `earnings_info` | May mutate `earnings_info` | Notes |
|---|---|---|---|
| Data enrichment | Yes | Yes | Raw provider normalization belongs here |
| Scoring | Yes | No | Soft penalty / factor awareness only |
| Ranking | Yes | No | Optional penalty or sort hint |
| Trade filter | Yes | No | Only if policy explicitly chooses to act on it |
| SelectionBundle | Yes | No | Must serialize read-only derived view |
| Dashboard | Yes | No | Display only |
| Telegram | Yes | No | Display only |
| Broker | No | No | Forbidden |
| Trading engine | No | No | Forbidden |

---

## 7. Allowed Consumers

The following modules may read `earnings_info` as a read-only field:

- `src/openalpha/candidate_ranking.py`
- `src/openalpha/trade_filter.py`
- `src/openalpha/selection_bundle.py`
- `src/openalpha/selection_report.py`
- `src/dashboard/combined.py`
- `src/notifier/alerts.py`
- reporting / diagnostics modules

If future scoring logic chooses to use the field, it must remain a soft, transparent input.

---

## 8. Forbidden Consumers

The following must not read `earnings_info` as a trading authority or state source:

- `src/broker/*`
- `src/engine/trading_engine.py`
- `src/order/*`
- `src/safety/live_guard.py`
- `src/safety/trading_environment_guard.py`
- `src/openalpha/config_writer.py`
- any module that writes TOP configs
- any module that creates, submits, or amends orders

Also forbidden:

- using `earnings_info` to bypass or weaken risk gates
- using `earnings_info` to auto-promote `PAPER_ELIGIBLE`
- using `earnings_info` to auto-promote `LIVE_ELIGIBLE`
- using `earnings_info` as a hidden side channel for execution decisions

---

## 9. Backward Compatibility

### Existing bundles

Older bundles do not have `earnings_info`. They must continue to load successfully.

Behavior when missing:

- `earnings_info = null` or absent
- display `UNKNOWN` / `N/A`
- do not infer a new risk level from `earnings_score` alone

### Legacy fields

The following legacy fields remain accepted as fallback inputs for compatibility only:

- `earnings_within_days`
- `earnings_within_7_days`
- `earnings_soon`
- `earnings_score`

Compatibility rule:

- legacy fields may be used to derive `earnings_info`
- legacy fields must not override a canonical `earnings_info` value
- if legacy and canonical fields disagree, keep the canonical derived field and expose the mismatch in diagnostics

### Existing reports / dashboard

- Missing `earnings_info` must not crash rendering.
- Dashboard and Telegram should continue to render older reports.
- If a report has only `earnings_score`, show it as a factor score, not as earnings calendar risk.

---

## 10. Testing Strategy

### Contract tests

- normalize provider payloads into canonical `earnings_info`
- preserve missing / partial / malformed input safely
- derive risk levels from trading days and market calendar
- handle stale dates and low-confidence dates

### Compatibility tests

- old bundle without `earnings_info` still loads
- old report without earnings fields still renders
- legacy fields can be read as fallback without crashing
- canonical `earnings_info` takes precedence over legacy aliases

### Safety tests

- no broker / trading engine / config writer dependency
- no automatic trade eligibility promotion
- no change to PAPER/LIVE gates
- no change to selector output counts

### Regression tests

- dashboard shows earnings event info when present
- dashboard shows `UNKNOWN` when absent
- Telegram rendering includes earnings risk only as informational text
- `candidate_ranking` and `trade_filter` still behave deterministically when the field is absent

---

## 11. Future Extension

The contract is intentionally minimal and can be extended later with:

- earnings surprise / beat-miss direction
- guidance revision flags
- pre-market / post-market event timing
- event confidence / source provenance details
- multi-source consensus / disagreement handling
- post-earnings cooldown window
- watchlist / near-miss event labels

Future work should keep the same rule:

**earnings risk is advisory metadata, not execution authority.**

---

## 12. Summary

`earnings_info` should become QuantCairn’s canonical read-only earnings event contract.

It should:

- unify current scattered earnings semantics
- remain optional and backward compatible
- support scoring / ranking / display / diagnostics
- never change broker or execution safety boundaries
- never silently become a trading gate
