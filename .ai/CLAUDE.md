# QuantCairn — AI Context for Assistant Agents

> **For**: Claude Code, Codex, Aider, Cursor, Windsurf, and other AI coding assistants.
> **Goal**: Understand this repo with minimal context tokens. ~120 lines.  Link deeper docs for details.

## Project Identity

**QuantCairn** (formerly "OpenAlpha", originally "AI Selector") — an AI-powered quantitative research platform that runs a 9-stage stock selection pipeline for range-bound swing trading on US equities. The system selects TOP K candidates daily, writes config files consumed by a trading engine, and notifies results via Telegram.

- **Language**: Python 3.14 (venv: `.venv/bin/python`)
- **Market**: US equities (NYSE/NASDAQ), LongBridge broker API
- **Trading style**: Range-bound swing trading (buy near support, sell near resistance)
- **Data**: Yahoo Finance (yfinance) for OHLCV, LongBridge for real-time quotes
- **Deployment**: macOS launchd scheduling + crontab monitoring

## Immutable Safety Redlines

**These must never be changed by any AI assistant under any circumstances:**

| Constraint | Value | Location |
|---|---|---|
| `allow_live_order` | **`false`** (NEVER true) | `config.local.yaml`, `trading_environment_guard.py` |
| `reduce_only` | **`true`** (NEVER false) | `trading_engine.py`, `live_guard.py` |
| Paper Gate | **NEVER bypass** | `paper_broker.py` |
| Live Gate | **NEVER bypass** | `longbridge_broker.py`, `trading_environment_guard.py` |
| Broker module | **NEVER modify** | `src/broker/` |
| TradingEngine | **NEVER modify** | `src/engine/trading_engine.py` |
| RiskManager | **NEVER modify** | `src/risk/manager.py` |
| PortfolioManager | **NEVER modify** | `src/portfolio/` |
| Order creation | **NEVER create orders** | Any `src/order/` |

**Violation of any of these = critical safety breach. The assistant must refuse and warn.**

## Module Map

```
src/
├── openalpha/       # ★ Core: AI selection pipeline (selector, scoring, funnel, diagnostics)
├── scoring/         # Scorer class: 30/20/20/15/10 weighted multi-factor model
├── universe/        # Symbol universe: 35 active US equities + ETFs
├── engine/          # Trading engine (DO NOT MODIFY)
├── broker/          # LongBridge & paper brokers (DO NOT MODIFY)
├── risk/            # Risk management (DO NOT MODIFY)
├── safety/          # LiveGuard, environment guard
├── portfolio/       # Position tracking (DO NOT MODIFY)
├── notifier/        # Telegram, console, macOS notification dispatch
├── backtest/        # Backtesting framework
├── strategy/        # Strategy definitions
├── dashboard/       # Combined dashboard (Jinja2 HTML)
├── candidate_validation/  # Candidate validation pipeline
├── shadow/          # Shadow trading observation
├── outcome/         # Outcome collection, governance, weight advisor
├── regime/          # Market regime detection
├── data/            # PriceFetcher (yfinance wrapper)
├── config/          # Runtime values, env loading
├── utils/           # Market calendar, helpers
├── order/           # Order management (DO NOT MODIFY)
└── reports/         # Report generation
```

## 9-Stage Selection Pipeline

```
UNIVERSE (35 symbols)
  → UNIVERSE_FILTER (cap at 50)
  → MARKET_DATA (OHLCV availability, independent of scoring)
  → SCORING_ELIGIBLE (Scorer produces multi-factor score)
  → BASE_RANKING (score_candidate() polish)
  → FORMAL_ELIGIBILITY (formal scoring gate)
  → DATA_QUALITY (spread/volume/volatility checks — mode-aware)
  → COMPOSITION_FILTER (diversified sector/correlation selection)
  → FORMAL_TOP (final tradable candidates)
```

**Key invariant**: Every stage must satisfy `output_count <= input_count`. FunnelTracker enforces this.

## Run Modes

| Mode | When | Quality Checks | Candidate Type |
|---|---|---|---|
| **FULL** | Market open + live quotes | Strict (bid/ask, spread, volatility) | `LIVE_TRADABLE` |
| **AFTER_MARKET** | After hours | Relaxed (skip spread) | `RESEARCH_ONLY` |
| **EOD_ONLY** | Closed/holiday | Relaxed (use hints/fallback) | `RESEARCH_ONLY` |
| **DEGRADED** | Premarket | Relaxed | `RESEARCH_ONLY` |

Preflight (`src/openalpha/preflight.py`) detects mode before pipeline runs.

## Scoring Model

**Real data path** (`score_frame`):
```
Base = 0.30×Volatility + 0.20×Volume + 0.20×Trend + 0.15×Repeatability + 0.10×Drawdown
Final = Base + 0.05×CorrelationBonus
```

**Fallback path** (`_fallback_scored_item`): Uses `FALLBACK_PROFILES` (all 35 symbols have entries). Synthetic volatility from band width. Universe filter skips ATR checks for fallback data.

## Key Files (most frequently modified)

| File | Lines | Purpose |
|---|---|---|
| `src/openalpha/selector.py` | ~1028 | Main pipeline orchestration |
| `src/scoring/scorer.py` | ~1091 | Multi-factor scoring model |
| `src/openalpha/funnel_tracker.py` | ~647 | Pipeline tracking, validation, diagnostics |
| `src/openalpha/data_diagnostics.py` | ~359 | Per-symbol data quality tracing |
| `src/openalpha/preflight.py` | ~258 | Market state detection |
| `src/notifier/alerts.py` | ~1578 | Telegram/webhook/console notifications |
| `scripts/run_ai_selector.py` | ~2600 | CLI entry point |
| `scripts/ai_selector_wrapper.py` | ~131 | launchd scheduler wrapper |

## Key Commands

```bash
# Run AI selection
.venv/bin/python scripts/run_ai_selector.py --universe-source managed

# Run with curl_cffi disabled (Surge proxy compatibility)
YF_DISABLE_CURL_CFFI=1 .venv/bin/python scripts/run_ai_selector.py --universe-source managed

# Run diagnostics
.venv/bin/python scripts/diag_market_data.py

# Run tests
.venv/bin/python -m pytest tests/ -q

# Force a selection run regardless of time
FORCE_AI_RUN=1 .venv/bin/python scripts/ai_selector_wrapper.py
```

## Testing

- **Framework**: pytest
- **Test count**: 59+ core integration tests, ~1075 total
- **Key test files**:
  - `tests/test_quality_mode_awareness.py` — mode-aware quality filtering
  - `tests/test_market_data_separation.py` — MARKET_DATA/SCORING separation
  - `tests/test_funnel_invariant.py` — pipeline invariant enforcement
  - `tests/test_ai_selector_quality_filters.py` — quality filter behavior
  - `tests/test_telegram_notification.py` — Telegram message chunking
- **Pre-existing failures**: 5 env-leak tests (`config.local.yaml`, unrelated to selector)

## Environment

- **venv**: `.venv/` (Python 3.14.4, OpenSSL 3.6.3 via Homebrew)
- **Proxy**: Surge (127.0.0.1:1082), requires `YF_DISABLE_CURL_CFFI=1` for Yahoo Finance
- **Config**: `config.yaml` (defaults) + `config.local.yaml` (secrets/overrides)
- **Scheduling**: macOS launchd (`com.soxs.ai_selector.plist`, every 60s) + crontab (monitor.sh)
- **State**: `state/` directory (selection markers, notification ledger, yfinance cache)

## AI Change Protocol

**Every AI assistant MUST follow this protocol before modifying any code:**

### Mandatory Workflow

1. **Explain affected modules** — List every file/module that will be touched and its role
2. **Explain risks** — Identify what could break, safety implications, and blast radius
3. **Provide implementation plan** — Step-by-step plan before writing code
4. **Modify code only after approval** — Wait for explicit human sign-off unless the change is documentation-only or a trivial bug fix
5. **Run related tests** — At minimum the test files covering the changed modules. Report failures honestly — never hide them
6. **Update CHANGELOG when behavior changes** — Selection pipeline output, scoring weights, quality filter rules, or API surfaces
7. **Update DECISION_LOG when architectural decisions are introduced** — New constraints, changed invariants, or design trade-offs

### Trivial Change Exemptions

The full protocol may be skipped only for:
- Typo fixes in comments or docs
- Adding log/print statements for debugging
- Updating test assertions to match already-changed behavior
- Edits to `.ai/` documentation files themselves

### Before Any Code Change

- Read `.ai/CLAUDE.md` (this file) — understand project structure and safety redlines
- Read `.ai/safety.md` if the change touches `src/broker/`, `src/engine/`, `src/risk/`, `src/portfolio/`, `src/order/`, or `src/safety/`
- Read `.ai/architecture.md` if the change affects pipeline stages or module dependencies
- Read `.ai/DECISION_LOG.md` if the change introduces a new architectural decision

### After Any Code Change

- Run `git status` and verify only intended files are modified
- Run `pytest tests/ -q` and confirm no new failures
- If a behavior change: update `CHANGELOG.md` or `DECISION_LOG.md`
- Wait for explicit instruction before `git push`

---

## Important Conventions

1. **Never push commits** unless explicitly asked
2. **Never modify** Broker, TradingEngine, RiskManager, PortfolioManager, LiveGuard, Paper Gate, or Live Gate
3. **Funnel invariant**: `output <= input` for every stage — enforced by FunnelTracker
4. **Preview vs Formal**: Preview = research-only candidates (visible in dashboard), Formal = passed all gates (tradable)
5. **Quality fallback**: Only in FULL mode when ALL quality checks reject. In other modes, relaxed checks produce formal candidates.
6. **Telegram**: Messages >4000 chars are auto-chunked. Channel: `@QuantCairnPicks`. Bot: `@chenweiderambot`.

## Related Docs

- `.ai/safety.md` — Detailed safety constraints and guard mechanics
- `.ai/architecture.md` — Module-level architecture and data flow diagrams
- `.ai/DECISION_LOG.md` — Engineering decisions, dates, reasons, alternatives
- `HANDOVER.md` — Project handover document (for human devs)
