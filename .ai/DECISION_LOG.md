# Decision Log — QuantCairn Engineering Decisions

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

## 16. Public Beta Feature Release — v0.12.1

**Decision**: Ship v0.12.1 as a Public Beta Feature Release with Telegram dual-channel notification templates, public API smoke tests, and an automated release CI workflow.

**Date**: 2026-07-25

**Background**: v0.12.0 established the public beta foundation with packaging, CI, and demo mode. v0.12.1 adds user-visible features that a public beta audience needs: structured Telegram notifications for the `@QuantCairnPicks` channel and a release pipeline that builds and validates wheels automatically.

**Reason**: (a) Telegram dual-channel templates (PUBLIC_CHANNEL_TEMPLATE ≤1500 chars, ADMIN_DEBUG_TEMPLATE full diagnostics) separate end-user communication from operator debugging. (b) Public API smoke tests verify that the `quantcairn` namespace imports work end-to-end without triggering broker or network calls. (c) The release CI workflow (`release.yml`) builds sdist + wheel, validates metadata, and smoke-tests the package — essential for PyPI publication.

**Alternatives considered**:
- Ship notification and tests in separate releases — rejected because they form a single public beta feature surface
- Wait for CLI work before releasing — rejected because Telegram + CI are independently valuable

**Impact**: `src/notifier/alerts.py` +243 lines (chunked delivery, dual-channel templates). `tests/test_public_api_smoke.py` +118 lines. `.github/workflows/release.yml` +52 lines. CI test matrix stabilized (3.11/3.12/3.13) and demo validation added.

---

## 17. Public Hardening Release — v0.12.2

**Decision**: Ship v0.12.2 as a documentation alignment and repository hygiene release. Zero code changes. Scope limited to AI context file branding updates, `.gitignore` hardening, and public/private boundary documentation.

**Date**: 2026-07-26

**Background**: A pre-release audit of v0.12.1 revealed that AI context files (`.ai/CLAUDE.md`, `.ai/DECISION_LOG.md`) still identified the project as "OpenAlpha" rather than "QuantCairn." Additionally, untracked runtime artifacts (`HANDOVER.md`, `artifacts/`, `config/candidate_models/`) appeared in `git status`, creating risk of accidental commit.

**Reason**: (a) AI context files are the primary onboarding documents for AI coding assistants — branding mismatch creates confusion about project identity. (b) Runtime artifacts that appear in `git status` increase the risk of private data entering public git history. (c) Both fixes are pure hygiene with zero blast radius — no code paths are affected.

**Alternatives considered**:
- Include in v0.12.1 scope — rejected because v0.12.1 was already tagged and rewriting history would break it
- Defer to v0.13.0 — rejected because these fixes are trivial and improve the repo state for every subsequent release

**Impact**: `.ai/CLAUDE.md` title and project identity line updated. `.ai/DECISION_LOG.md` title updated, entries #16 and #17 added. `.gitignore` +3 patterns (`HANDOVER.md`, `artifacts/`, `config/candidate_models/`). Zero code changes. Zero test impact.

---

## 18. Repository Boundary Formalization — v0.12.3

**Decision**: Formalize the public/private repository boundary with an explicit AI agent reference document (`.ai/REPOSITORY_BOUNDARY.md`), fix configuration boundary inconsistencies, sanitize operational scripts of personal paths, and add historical/context banners to legacy documentation.

**Date**: 2026-07-26

**Background**: The repository boundary audit (same date) identified several issues: (a) `config.yaml` was both tracked in git AND listed in `.gitignore` — a latent contradiction. (b) `health_check.sh` hardcoded `$HOME/soxs-range-arbitrage` as a fallback path, exposing the maintainer's personal directory structure. (c) Legacy documentation (`BRAND_MIGRATION.md`, `GITHUB_MIGRATION.md`, `CURRENT_SYSTEM_STATE.md`) lacked status banners, so readers could not distinguish historical plans from current state. (d) WeChat scripts lacked any context that they are macOS-specific optional helpers. (e) AI coding assistants had no structured, machine-readable document defining what must never be committed.

**Reason**: (a) A tracked file in `.gitignore` is harmless (git ignores the gitignore for tracked files) but confusing — it signals uncertainty about whether the file should be public. `config.yaml` contains only public-safe defaults (empty broker credentials, paper mode, example capital) so the correct fix is removing it from `.gitignore`. (b) Personal paths in public scripts are a privacy concern and a portability problem. Using script-relative detection (`$SCRIPT_DIR`) preserves functionality while removing the personal reference. (c) Historical docs are valuable for project archaeology but need clear status markers so they aren't mistaken for current-state documents. (d) AI agents need explicit, structured guidance about the public/private boundary — `.gitignore` patterns alone don't convey intent or rationale.

**Alternatives considered**:
- Remove `config.yaml` from tracking instead of from `.gitignore` — rejected because the file is genuinely public-safe and serves as the base configuration for new users
- Delete historical docs — rejected because project history has value; markers are sufficient
- Delete WeChat scripts — rejected; they are functional utilities that may be useful to macOS users
- Skip boundary documentation — rejected because the audit demonstrated AI agents benefit from explicit rules

**Impact**: `.ai/REPOSITORY_BOUNDARY.md` created (+1 file, ~200 lines). `.gitignore`: removed `config.yaml` line. `health_check.sh`: `$HOME/soxs-range-arbitrage` → `$SCRIPT_DIR` fallback. `monitor.sh`, `scripts/run_top_engine.sh`: added operational context banners. `docs/BRAND_MIGRATION.md`, `docs/GITHUB_MIGRATION.md`, `docs/CURRENT_SYSTEM_STATE.md`: added historical/snapshot status banners. `scripts/wechat_notify.py`, `scripts/wechat_webhook.py`: added platform-specific context banners. `.ai/DECISION_LOG.md`: entry #18 added. Zero production code changes. Zero test impact.

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
| 16 | Public Beta Feature Release v0.12.1 | 2026-07-25 | `alerts.py`, `release.yml` |
| 17 | Public Hardening Release v0.12.2 | 2026-07-26 | `.ai/CLAUDE.md`, `.gitignore` |
| 18 | Repository Boundary Formalization v0.12.3 | 2026-07-26 | `.ai/REPOSITORY_BOUNDARY.md` |
| 19 | Gap Risk One-Strike Fix | 2026-08-04 | `scorer.py` |
| 20 | Redundant Market Data Fetch Removal | 2026-08-05 | `selector.py` |
| 21 | Research Platform v0.13.0 | 2026-08-05 | `selection_ledger.py`, `selection_backfill.py`, `learning_dataset.py`, `research_analytics.py`, `combined.py` |
| 22 | Walk-Forward Validation (Phase 3B) | 2026-08-05 | `walk_forward.py` |
| 23 | Research History Registry (Phase 4A) | 2026-08-05 | `research_registry.py` |

---

## 19. Gap Risk One-Strike Fix

**Decision**: Remove the `max_gap_pct > 5.0%` leg from the gap risk rejection check, keeping only `gap_rate > 0.20` for structural frequent-gap detection. The `max_gap_pct` check was a one-strike rule that permanently disqualified any symbol that had ever had a single overnight gap exceeding 5% in its entire 252-day history window.

**Date**: 2026-08-04

**Background**: An audit of 15 consecutive days of NO_SELECTION (1,900+ pipeline runs, zero formal candidates) revealed that the dominant rejection was "frequent gap risk: 25". Investigation showed ZERO symbols actually triggered on `gap_rate > 0.20`. All 25 rejections were caused by `max_gap_pct > 5.0%` — a single earnings gap (normal quarterly events) permanently disqualified symbols like AAPL (one 8.58% gap, otherwise 0.39% median daily gap).

**Reason**: (a) A single earnings gap is normal market behavior, not a structural disqualifier for range trading. (b) The gap_rate check already catches genuinely frequent gappers (>20% of sessions with >5% gaps). (c) The max_gap_pct check was redundant when gap_rate fires (if >20% of days gap >5%, max_gap will always be >5%). (d) After removal, SCORING_ELIGIBLE went from 0 to 31 output — restoring the pipeline.

**Alternatives considered**:
- Raise max_gap_pct threshold to 10% — rejected because it still catches earnings gaps (AAPL's 8.58% would pass but many earnings gaps exceed 10%)
- Add earnings-aware exception — rejected as over-engineered; the gap_rate check is sufficient
- Keep unchanged — rejected because 15 days of zero output proves the rule is broken

**Impact**: `scorer.py:924` changed from `if gap_rate > 0.20 or max_gap_pct > self.GAP_LIMIT_PCT` to `if gap_rate > 0.20`. +6 regression tests. max_gap_pct still computed and reported in metrics for downstream diagnostics.

---

## 20. Redundant Market Data Fetch Removal

**Decision**: Remove the `check_data_availability()` OHLCV pre-fetch from the MARKET_DATA pipeline stage. This function fetched full OHLCV data for all 50 symbols just to count rows, then discarded the result. SCORING_ELIGIBLE then fetched the same data again via `_load_history()`. MARKET_DATA becomes a pass-through stage.

**Date**: 2026-08-05

**Background**: After the gap risk fix restored SCORING_ELIGIBLE output to 31, a new bottleneck appeared: DATA_QUALITY was rejecting all 12 candidates with `fast_preliminary_bypass` (budget timeout). Instrumentation revealed ~132 OHLCV HTTP round-trips consuming ~289s of fetch time, exhausting the 15s total budget before quality checks could run.

**Reason**: (a) Double-fetching was pure waste — the same data fetched twice per symbol per run. (b) The scoring pipeline already handles data-unavailable symbols gracefully via fallback profiles and `scoring_rejections`. (c) The "safe bottom" logic (never empty the pool to zero) was already in place to prevent scoring starvation.

**Alternatives considered**:
- Add in-memory OHLCV cache — rejected as more complex; removing the redundant fetch is simpler and sufficient
- Raise budget defaults — rejected as masking the root cause without fixing it
- Keep the pre-check — rejected because it's redundant with scoring-level handling

**Impact**: MARKET_DATA is now pass-through (50→50). ~50 HTTP round-trips eliminated per run. Wall-clock reduced ~41% (252s → 149s). DATA_QUALITY budget exhaustion resolved. 5 formal candidates produced instead of 0. +4 regression tests.

---

## 21. Research Platform v0.13.0

**Decision**: Ship a complete selection-to-outcome measurement pipeline (Phases 1–3A) as v0.13.0-research-platform. The pipeline captures every formal candidate at selection time, backfills forward returns/risk metrics, joins into an ML-ready dataset, produces analytics reports, and exposes them via the dashboard API. All phases are read-only consumers — zero modifications to selection, scoring, ranking, or trading logic.

**Date**: 2026-08-05

**Background**: The system had 302 historical selection bundles with zero outcome tracking in RESEARCH mode. The outcome collector (`src/outcome/collector.py`) had a production-ready v3 schema but zero populated data because it depends on FillEvents from broker audit logs. No infrastructure existed to answer "are our selections any good?"

**Reason**: (a) Measuring selection quality is a prerequisite for any future scoring model improvements — you cannot optimize what you don't measure. (b) The ledger/backfill/dataset/analytics pipeline is intentionally decoupled from trading — it runs offline, never blocks selection, and never affects live behavior. (c) The dashboard integration is read-only JSON consumption, following existing patterns exactly.

**Alternatives considered**:
- Use broker audit logs for outcomes — rejected because RESEARCH mode produces zero trades
- Integrate measurement into the real-time selection path — rejected because it would add latency and risk
- Wait for paper trading to be active — rejected because measurement is valuable even without trades

**Impact**: +5 source modules (+1,733 lines), +5 test modules (+2,897 lines), +83 lines in dashboard. 239 tests pass. Zero protected module modifications. Storage under `artifacts/learning/` (selection_ledger, selection_outcomes, dataset, analytics).

---

## 22. Walk-Forward Validation Framework (Phase 3B)

**Decision**: Add a research-only walk-forward evaluation framework that analyzes selection performance stability across rolling time periods. Reads the Phase 2B dataset, splits into configurable train/validation/forward windows, computes per-period metrics, feature stability, sector stability, and generates robustness flags (observations only — never blocks trading or triggers automatic parameter changes).

**Date**: 2026-08-05

**Background**: With phases 1–3A complete, the system can measure selection performance and serve analytics via the dashboard. But there is no framework for detecting whether that performance is STABLE across different market regimes. Aggregate metrics can hide period-dependent variation — a strategy that appears strong overall might only work in specific conditions.

**Reason**: (a) Walk-forward is the standard methodology for validating time-series strategies without look-ahead bias: train on past data, validate on intermediate data, test on future data. (b) Rolling windows reveal performance stability and degradation patterns. (c) Feature drift detection warns when the characteristics of selected candidates change over time — an early signal of regime shift. (d) Configurable window sizes (train/validation/forward months, step size) support different research questions. (e) Robustness flags (LOW_SAMPLE_SIZE, PERIOD_DEGRADATION, FEATURE_DRIFT) are observations that can inform human review — they never feed back into production automatically.

**Alternatives considered**:
- Single aggregate performance report — rejected because it hides period-dependent performance variation
- Live monitoring in the selector — rejected because walk-forward is an offline analysis; embedding it would add latency and coupling risk
- Backtesting framework integration — rejected because the existing backtester operates on strategy variants, not selection outcomes

**Impact**: +654 lines in `walk_forward.py`, +532 lines in tests. 22 tests, zero production module modifications. Single import dependency: `learning_dataset`. Output stored in `artifacts/learning/walk_forward/`. No imports from selector, scorer, engine, broker, risk, or safety modules.

---

## 23. Research History Registry (Phase 4A)

**Decision**: Add a research-only registry layer that records every selection run, tracks dataset growth over time, classifies market regimes from bundle benchmark data, evaluates research quality (coverage, age, feature availability), and produces an ML readiness assessment. All outputs are observation reports — never modify production configuration or trigger automatic training.

**Date**: 2026-08-05

**Background**: Phases 1–3 established the selection → outcome → dataset → analytics → walk-forward pipeline. But the system has no infrastructure for tracking long-term accumulation — how many runs have been recorded, how many outcomes backfilled, what market regimes are represented, or whether the dataset is large and diverse enough for ML research. The existing 100 selection bundles + 2,302 funnel reports represent significant historical data with no aggregated registry.

**Reason**: (a) Long-term research requires tracking data volume and quality trends. (b) ML readiness assessment prevents premature training on insufficient data. (c) Market regime tagging enables regime-aware analysis. (d) The fire-and-forget hook adds minimal risk (identical pattern to Phase 1 ledger hook). (e) All analysis functions read existing artifact directories — no new data collection infrastructure needed.

**Alternatives considered**:
- Combine with the Phase 1 selection ledger — rejected because the ledger captures per-candidate snapshots, not run-level metadata or aggregate growth stats
- Build a database-backed registry — rejected as over-engineered for the current data volume; JSON files in `artifacts/learning/research_history/` are sufficient and follow existing conventions
- Skip regime tagging — rejected because regime awareness is critical for evaluating whether selection strategy performance is regime-dependent
- Make ML readiness trigger automatic training — rejected as a safety violation; the report is an observation, never an action

**Impact**: +497 lines in `research_registry.py`, +527 lines in tests, +35 lines in selector.py (fire-and-forget hook). 22 tests. Zero production module modifications. Output stored in `artifacts/learning/research_history/` (5 JSON files: `run_index.json`, `dataset_tracker.json`, `regime_tags.json`, `research_quality_report.json`, `ml_readiness.json`). ML readiness thresholds are configurable via function parameters — never hardcoded. No imports from selector, scorer, engine, broker, risk, or safety modules.

## Decision Index (continued)

| # | Decision | Date | Key File |
|---|---|---|---|
| 1–18 | (see above) | | |
| 19 | Gap risk one-strike fix | 2026-08-04 | `scorer.py` |
| 20 | Redundant market data fetch removal | 2026-08-05 | `selector.py` |
| 21 | Research Platform v0.13.0 | 2026-08-05 | `selection_ledger.py`, `selection_backfill.py`, `learning_dataset.py`, `research_analytics.py`, `combined.py` |
| 22 | Walk-Forward Validation (Phase 3B) | 2026-08-05 | `walk_forward.py` |
| 23 | Research History Registry (Phase 4A) | 2026-08-05 | `research_registry.py` |

## 2026-07-28 — TOP Config Empty-Selection Sync Safety

- Existing live TOP runtime configs are not overwritten by selector output.
- Approved exception: when the formal selector result is empty (`selected_symbols == []`), stale live TOP runtime configs may be overwritten only into the existing disabled schema.
- Before that disable transition, the previous live TOP config is preserved under `state/top_config_disable_backups/` for audit/history only.
- `top_sync_status` is `OK` only when the selection bundle state matches the consumed runtime TOP config files; mismatches are published as `NOT_OK`.

## 2026-07-28 — Final Selection Funnel Reporting

- `FORMAL_TOP` remains the selector-stage research/formal candidate output and is no longer treated as the implicit terminal reporting stage.
- `POST_FILTER` records existing final post-processing removals, using current rejection and formal eligibility reason helpers without changing eligibility decisions.
- `FINAL_SELECTED` records the exact executable selected-symbol list consumed by selection bundle/TOP config publication and mirrored by `final_selected_symbols`.
- The stages are additive; existing fields such as `formal_candidates`, `research_top_candidates`, `tradable_top_candidates`, `final_selected_symbols`, and `selection_funnel.stages` remain present.

## 2026-07-28 — PaperBroker Persistence Rollback Exception

- Temporary one-time safety exception approved for this task only to modify `src/broker/paper_broker.py`.
- PaperBroker order execution now snapshots in-memory broker state and restores it if portfolio-state persistence fails, preventing partial cash/position/trade-history mutation.
- The scope is limited to transactional rollback on persistence failure; fill logic, pricing, commissions, slippage, strategy behavior, and risk management remain unchanged.

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
| 16 | Public Beta Feature Release v0.12.1 | 2026-07-25 | `alerts.py`, `release.yml` |
| 17 | Public Hardening Release v0.12.2 | 2026-07-26 | `.ai/CLAUDE.md`, `.gitignore` |
