# Decision Log — OpenAlpha Engineering Decisions

> **Format**: Each entry = Decision, Date, Background, Reason, Alternatives, Impact.
> **Convention**: Mark uncertain items with `⚠️ Reason requires confirmation`.
> **Purpose**: AI assistants read this to understand *why*, not just *what*.

---

## 1. 9-Stage Selection Pipeline

**Decision**: Model the AI selection process as a 9-stage linear pipeline with enforced invariant (`output ≤ input` per stage).

**Date**: 2026-07 (formalized with FunnelTracker)

**Background**: Original selector mixed data fetching, scoring, quality filtering, and diversity selection in one monolithic function. Failures were silent — no visibility into which stage dropped which symbol.

**Reason**: A staged pipeline with immutable funnel invariants provides: (a) exact attribution of every symbol drop, (b) automated consistency validation, (c) independent testability per stage, (d) audit trail for regulatory and debugging purposes.

**Alternatives considered**:
- Keep monolithic function with logging — rejected because logs don't enforce invariants
- DAG with parallel scoring branches — rejected because linear ordering matters (quality before diversity)
- Event-sourced pipeline — rejected as over-engineered for daily batch selection

**Impact**: Every selection run produces auditable debug artifact at `artifacts/selection/funnel_debug.json`. FunnelTracker validates `input == previous.output` chain. Chain breaks are suppressed for expected paths (quality fallback, EOD relaxed mode).

---

## 2. Selector and Trading Engine Separation

**Decision**: The AI selector (`src/openalpha/selector.py`) and Trading Engine (`src/engine/trading_engine.py`) are completely decoupled. The selector writes YAML configs; the engine reads them. No runtime coupling.

**Date**: Original architecture (pre-July 2026)

**Background**: The selector is an analytical pipeline that runs daily and produces candidate lists. The trading engine is a continuous loop that monitors configs and executes trades. Coupling them would mean a selector bug could crash trading, or a trading state change could corrupt selection.

**Reason**: (a) Blast radius isolation — a selector failure never affects running positions. (b) Independent deployability — selector can be updated without touching trading engine. (c) Different runtime profiles — selector runs once daily (cpu-heavy), engine runs continuously (latency-sensitive).

**Alternatives considered**:
- Selector as engine plugin — rejected because engine restart requirement creates downtime risk
- Shared in-memory state — rejected because process isolation is a safety feature

**Impact**: Configs at `configs/TOP{1,2,3}.yaml` are the only interface. Config writer skips writes if live configs exist (safety: `Skipping TOP{N} disabled write`).

---

## 3. Dashboard Read-Only Principle

**Decision**: The combined dashboard (`src/dashboard/combined.py`) is strictly read-only. It reads artifacts, configs, and state files but never initiates trades, modifies configs, or calls broker APIs.

**Date**: 2026-07

**Background**: Dashboards that can trigger actions create safety risks — a dashboard refresh could inadvertently execute orders. The Jinja2 template renders HTML from JSON/YAML artifact files.

**Reason**: (a) Dashboard is served via static HTML or `health_check.sh`. (b) No POST endpoints, no action buttons, no broker integration. (c) Template rendering failure means a blank panel, not a trading failure.

**Alternatives considered**:
- Interactive dashboard with action buttons → rejected for safety
- Live WebSocket dashboard → rejected because it would require broker connection from web context

**Impact**: Any assistant modifying dashboard code must not add trade-affecting functionality. Dashboard safe from accidental trading triggers.

---

## 4. Telegram Notification Isolation

**Decision**: Telegram notification (`src/notifier/alerts.py`) is fire-and-forget. Notification failure never blocks selection pipeline. Message sending runs in the main thread but with rate-limiting and 8-second timeouts.

**Date**: 2026-07 (chunked delivery added 2026-07-24)

**Background**: Telegram API has a 4096-char limit. Long diagnostic-rich messages triggered `400 Bad Request: message is too long`. Previously the notification was a hard-wired single message.

**Reason**: (a) Notification is best-effort — selection results are always available in artifacts even if Telegram fails. (b) Chunking at paragraph boundaries preserves logical structure. (c) Deduplication via notification_ledger.jsonl prevents spam.

**Alternatives considered**:
- Truncate to 4096 chars — rejected because it loses critical information (stage counts, reasons)
- Send as file attachment — rejected because Telegram `sendDocument` requires different bot permissions
- Discord webhook only — rejected because Telegram is the primary notification channel

**Impact**: Long messages auto-split. Max 3 API calls per selection (rate-limited at 1/sec). HTML mode with plain-text fallback per chunk.

---

## 5. Quality Fallback Mechanism

**Decision**: When DATA_QUALITY rejects all candidates, the system enters "Quality Fallback" mode. Preview Candidates (research-only) are produced from the pre-quality pool. Formal Candidates are empty.

**Date**: 2026-07-24 (mode-aware refinement added same day)

**Background**: During non-market hours, real-time bid/ask spread data is unavailable. All 12 candidates were rejected by strict quality checks, leaving FORMAL_TOP empty even though scoring produced valid results.

**Reason**: (a) Strict quality checks are meaningless without live data. (b) Running relaxed checks and labeling results `RESEARCH_ONLY` maintains the distinction between verified and unverified candidates. (c) `quality_fallback_active` is only `True` in FULL mode when live checks fail — this preserves the semantic meaning of "fallback" as "something went wrong."

**Alternatives considered**:
- Always run strict checks (previous behavior) — rejected because it produces empty results 90% of the time (only ~6.5 hours/day have live quotes)
- Always run relaxed — rejected because it eliminates the live-spread safety check
- Skip DATA_QUALITY entirely in non-FULL mode — rejected because volume filter still matters

**Impact**: EOD/AFTER_MARKET/DEGRADED modes now produce Formal Candidates (type: RESEARCH_ONLY). FULL mode still enforces strict checks. Pipeline success rate: 8.57% → 100% in non-FULL modes.

---

## 6. Paper Trading Before Live Trading

**Decision**: All trading systems default to paper/sandbox mode. Live trading requires explicit multi-layer approval: (a) `config.local.yaml: allow_live_order=true`, (b) `trading_environment_guard.py` validation, (c) `live_guard.py` pre-flight checks, (d) runtime config flag.

**Date**: Original architecture, hardened 2026-07

**Background**: LongBridge broker supports `sandbox` and `prod` environments. The system was initially developed for live trading but switched to paper-first after safety review.

**Reason**: (a) No live order can be placed without `allow_live_order=true` at multiple layers. (b) `trading_environment_guard.py:42` forces `allow_live_order=False` during initialization regardless of config. (c) Sandbox mode requires `account_type=paper/demo` and `environment=sandbox`. (d) `LiveGuard` validates all preconditions before any trading session.

**Alternatives considered**:
- Single config flag — rejected because single-point-of-failure in safety
- Runtime toggle — rejected because it could be changed mid-session

**Impact**: Three independent gates must all agree before any live order is possible. Paper trading is the default.

---

## 7. SOXS Inverse ETF Special Handling

**Decision**: `SOXS` is permanently `reduce_only=true` (`INVERSE_REDUCE_ONLY = {"SOXS"}`). A class of `LIQUID_SPECIAL_ETFS` (16 symbols) receives relaxed spread checks.

**Date**: Original architecture

**Background**: SOXS is a 3x inverse semiconductor ETF with extreme volatility decay. Opening new SOXS positions carries compound risk: market direction + leverage decay + volatility expansion. The system was originally built around SOXS range arbitrage (the repo name `soxs-range-arbitrage` reflects this legacy).

**Reason**: (a) SOXS can only close existing positions, never open new ones. (b) `LIQUID_SPECIAL_ETFS` (SOXL, TQQQ, SQQQ, etc.) have market-maker depth that exceeds retail quote displays — spread checks use relaxed rules. (c) Volatility limit for special ETFs is 35% vs 15% for common stocks.

**Alternatives considered**:
- Remove SOXS entirely — rejected because closing existing positions is still needed
- Apply same rules to all inverse ETFs — rejected because SOXS has uniquely extreme behavior

**Impact**: `INVERSE_REDUCE_ONLY` set is checked at config write time. `LIQUID_SPECIAL_ETFS` bypasses spread_unavailable rejection in quality filter.

---

## 8. Universe Manager Design

**Decision**: Replace hardcoded 9-symbol sample with a managed 35-symbol universe loaded from `UniverseManager` snapshots and persisted as JSON.

**Date**: 2026-07 (48 profiles, expanded to 35 active with full coverage)

**Background**: The original selector used `self.universe._load_local_snapshot()` which returned a fixed 9-symbol list. Adding symbols required code changes. The managed universe system supports enable/disable per symbol via CLI.

**Reason**: (a) Operators can adjust the universe without touching selector code. (b) Symbol profiles carry metadata (sector, asset_type, risk_score, volatility_score) used by downstream filters. (c) 4-level filter pipeline (liquidity → price → risk → composition) validates all enabled symbols.

**Alternatives considered**:
- SP500 full universe — rejected because 500 symbols is too many for the daily quality check budget (8 seconds)
- Dynamic ETF screener — rejected because screener results are non-deterministic
- Hardcoded list — rejected because it requires code deployment to add/remove symbols

**Impact**: Universe manager at `src/universe/manager.py`. Snapshot at `artifacts/universe/universe_snapshot.json`. CLI management via `scripts/manage_universe.py`.

---

## 9. Preflight Market Check

**Decision**: Run a market state assessment BEFORE building the universe. Preflight determines `run_mode` (FULL/DEGRADED/AFTER_MARKET/EOD_ONLY) which gates quality filter strictness.

**Date**: 2026-07-24

**Background**: The selector ran the same quality checks regardless of whether the market was open. During premarket/after-hours/weekends, live quotes are unavailable, causing universal rejection at DATA_QUALITY stage.

**Reason**: (a) Market state detection prevents mode-inappropriate checks. (b) Run mode propagates through the entire pipeline — scoring fallback behavior, quality filter strictness, and FORMAL_TOP output type all depend on it. (c) Advisory-only — never blocks selection from running, only adjusts behavior.

**Alternatives considered**:
- Skip preflight and use try/except for live data — rejected because it conflates "data unavailable" with "API error"
- Time-based heuristic (check clock) — rejected because it doesn't account for holidays, early closes
- Always run relaxed — rejected for same reason as #5

**Impact**: `src/openalpha/preflight.py` runs before pipeline. `_run_mode` propagates to quality filter, composition, and TOP output. Market state artifact at `artifacts/selection/preflight.json`.

---

## 10. Human-Approval Learning Governance

**Decision**: All machine learning weight proposals default to `PENDING_HUMAN_APPROVAL`. Auto-activation is impossible — `ACTIVE` state requires `approved_by_human=True` with non-empty reason.

**Date**: 2026-07

**Background**: The Weight Advisor (v2) analyzes trade outcomes and suggests factor weight adjustments. Without governance, the model could auto-tune itself into degenerate states (e.g., overfitting to recent volatility, ignoring trend during range-bound markets).

**Reason**: (a) Model weight changes affect all future selections — a bad weight update damages the entire pipeline. (b) State machine (`DRAFT → BACKTESTED → WALK_FORWARD_VALIDATED → REVIEW_REQUIRED → APPROVED → ACTIVE`) ensures evidence-based progression. (c) Human-in-the-loop is the final gate — no automated approval.

**Alternatives considered**:
- Fully automated weight updates — rejected because ML models can silently degrade
- Manual-only weights — rejected because data-driven adjustment is the whole point
- A/B testing with gradual rollout — rejected as over-engineered for daily batch selection

**Impact**: All proposals start at `PENDING_HUMAN_APPROVAL`. `LearningGovernance.can_transition(ACTIVE) → False` unless `approved_by_human=True`. Audit trail at `artifacts/learning/governance_audit.jsonl`. Proposal index at `artifacts/learning/proposal_index.json`.

---

## 11. MARKET_DATA Independence from SCORING

**Decision**: MARKET_DATA stage validates OHLCV data availability independently of whether scoring succeeds. A symbol with 252 rows of OHLCV passes MARKET_DATA even if `score_frame()` later rejects it for range-too-tight or volatility-too-low.

**Date**: 2026-07-24

**Background**: Previously, MARKET_DATA output was computed from scoring results — if scoring returned nothing for a symbol, it was marked as "market_data_sufficiency_failed." This conflated a data problem (no OHLCV) with a scoring problem (bad range).

**Reason**: (a) Data availability and scoring fitness are different concerns — combining them makes debugging impossible. (b) A symbol with perfect data but bad trading characteristics should fail at SCORING_ELIGIBLE, not MARKET_DATA. (c) Separation enables accurate zero-output detection — "FIRST ZERO-OUTPUT STAGE: DATA_QUALITY" tells a different story than "FIRST ZERO-OUTPUT STAGE: MARKET_DATA."

**Alternatives considered**:
- Keep combined — rejected because diagnostics show "reason unknown" for 32/35 symbols
- Merge SCORING_ELIGIBLE into MARKET_DATA — rejected because it eliminates the granularity we just gained

**Impact**: `check_data_availability()` (in `data_diagnostics.py`) runs before scoring. Scoring only processes data-available symbols. Chain: `MARKET_DATA → SCORING_ELIGIBLE` with independent outputs.

---

## 12. Fallback Profile Universal Coverage

**Decision**: Every active universe symbol (35) must have a `FALLBACK_PROFILES` entry with `range_low`, `range_high`, and `volume`. Fallback-derived volatility must never trigger ATR rejection.

**Date**: 2026-07-24

**Background**: Only 10 of 35 symbols had fallback profiles. When Yahoo Finance was unavailable (TLS issues, rate limiting, after-hours), 25 symbols had zero recovery path. Additionally, 11 symbols with fallback profiles were still rejected because the synthetic band width produced implied ATR > 8% (common_stock cap).

**Reason**: (a) Yahoo Finance is a weak dependency — it rate-limits, has TLS issues behind proxies, and returns empty data for some tickers. (b) Fallback profiles provide a deterministic recovery path. (c) `skip_atr_validation=True` for fallback data because synthetic volatility is not real market data and should not gate universe admission.

**Alternatives considered**:
- Multiple data providers (Alpha Vantage, Polygon, IEX) — rejected because they require API keys and have their own rate limits
- Cache-only mode — rejected because cache expires and symbols still need profiles
- Accept empty results — rejected because zero candidates is worse than fallback-scored candidates

**Impact**: All 35 symbols have profiles. `universe_filter.py: evaluate_universe_candidate(skip_atr_validation=True)` used for fallback candidates. Price, volume, and market cap checks still apply. FALLBACK_PROFILES, FALLBACK_RANGE_PCT, FALLBACK_SECTOR, and FALLBACK_MARKET_CAP are kept in sync.

---

## 13. Funnel Invariant Enforcement

**Decision**: Every pipeline stage must satisfy `output_count <= input_count`. FunnelTracker.validate() reports any violation. The FORMAL_TOP stage must never output more candidates than DATA_QUALITY produced.

**Date**: 2026-07-24

**Background**: A backfill loop in the selector padded `topk` from the raw scored list when quality filters left fewer than 5 candidates. This caused FORMAL_TOP `output_count=3` when DATA_QUALITY `output_count=2`, violating the funnel invariant.

**Reason**: (a) A funnel by definition narrows — output > input means candidates were injected from outside the pipeline. (b) Injected candidates never passed DATA_QUALITY — they have unverified spread, volume, and volatility characteristics. (c) "Fill to TOP3" is a UI preference, not a safety requirement — better to have 1 verified candidate than 3 unverified ones.

**Alternatives considered**:
- Allow injection with "quality_backfill" flag — rejected because it creates false confidence in output
- Lower the TOP_K requirement when candidates are scarce — accepted as better than padding

**Impact**: Backfill loop removed. `topk ⊆ filtered_candidates` always. Quality fallback uses pre-quality pool transparently. FunnelTracker validates invariant on every run. 8 regression tests guard against reintroduction.

---

## 14. Curl_CFFI Disablement for Surge Proxy Compatibility

**Decision**: Set `YF_DISABLE_CURL_CFFI=1` in the wrapper to force yfinance to use `requests` + Python SSL instead of `curl_cffi`'s bundled TLS library.

**Date**: 2026-07-24

**Background**: Surge proxy (127.0.0.1:1082) performs MITM on HTTPS connections. `curl_cffi` bundles its own libcurl/TLS which cannot validate Surge's MITM certificates, producing `curl: (35) TLS connect error: error:00000000:invalid library (0):OPENSSL_internal:invalid library (0)`.

**Reason**: (a) Python's `requests` uses Homebrew OpenSSL which trusts the system keychain where Surge's CA is installed. (b) `curl_cffi` provides browser TLS impersonation to avoid Yahoo rate-limiting, but the TLS compatibility cost outweighs the impersonation benefit when behind Surge. (c) Disabling at the wrapper level ensures consistent behavior across all invocation paths.

**Alternatives considered**:
- Bypass Surge for Yahoo domains — rejected because it requires Surge rule changes and may not work with all routing modes
- Use system curl — rejected because Python's curl_cffi doesn't use the system curl
- Accept the error — rejected because 32/35 symbols fail

**Impact**: Wrapper sets `YF_DISABLE_CURL_CFFI=1`. `scripts/diag_market_data.py` detects TLS issues. Risk: higher Yahoo 429 rate-limiting without browser impersonation (mitigated by fallback profiles).

---

## 15. Configuration Layering

**Decision**: Configuration is layered: `config.yaml` (defaults) → `config.local.yaml` (overrides) → environment variables (highest priority). Secrets live only in `config.local.yaml` and environment variables.

**Date**: Original architecture

**Background**: `config.yaml` is tracked in git. `config.local.yaml` is gitignored and contains API keys, tokens, and environment-specific overrides. Environment variables are set by launchd plist and shell profile.

**Reason**: (a) Secrets must never enter git history. (b) Different environments (dev machine, CI, deployment) need different configs. (c) Environment variables are needed for launchd which doesn't source shell profiles.

**Alternatives considered**:
- Single config with env var substitution — rejected because it's fragile with launchd
- .env file only — rejected because YAML supports nested structures better

**Impact**: Notification config loads from both layers: `_load_notification_config()` merges `config.yaml` and `config.local.yaml`. Runtime values use `get_runtime_env()` which checks env vars first, then config.

---

## Decision Index

| # | Decision | Date | Key File |
|---|---|---|---|
| 1 | 9-stage pipeline with invariant | 2026-07 | `funnel_tracker.py` |
| 2 | Selector/Engine separation | Original | `selector.py`, `trading_engine.py` |
| 3 | Dashboard read-only | 2026-07 | `dashboard/combined.py` |
| 4 | Telegram isolation + chunking | 2026-07 | `notifier/alerts.py` |
| 5 | Quality fallback mechanism | 2026-07-24 | `selector.py` |
| 6 | Paper-first trading | Original | `safety/` |
| 7 | SOXS reduce-only + special ETFs | Original | `selector.py` |
| 8 | Managed universe (35 symbols) | 2026-07 | `universe/manager.py` |
| 9 | Preflight market check | 2026-07-24 | `preflight.py` |
| 10 | Human-approval governance | 2026-07 | `outcome/governance.py` |
| 11 | MARKET_DATA / SCORING separation | 2026-07-24 | `data_diagnostics.py` |
| 12 | Universal fallback profile coverage | 2026-07-24 | `scorer.py` |
| 13 | Funnel invariant enforcement | 2026-07-24 | `funnel_tracker.py` |
| 14 | Curl_CFFI disable for proxy | 2026-07-24 | `ai_selector_wrapper.py` |
| 15 | Config layering (3 levels) | Original | `config.yaml`, `config.local.yaml` |
