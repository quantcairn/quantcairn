# QuantCairn — AI Context for Assistant Agents

> **For**: Claude Code, Codex, Aider, Cursor, Windsurf, and other AI coding assistants.
> **Goal**: Understand this repo with minimal context tokens. ~120 lines.  Link deeper docs for details.
>
> **Routing**: read [`.ai/README.md`](README.md) first for document order, authority precedence, and update routing.

## Project Identity

**QuantCairn** (formerly "OpenAlpha", originally "AI Selector") — an AI-powered quantitative research platform that runs a 9-stage stock selection pipeline for range-bound swing trading on US equities. The system selects TOP K candidates daily, writes config files consumed by a trading engine, and notifies results via Telegram.

- **Language**: Python 3.14 (venv: `.venv/bin/python`)
- **Market**: US equities (NYSE/NASDAQ), LongBridge broker API
- **Trading style**: Range-bound swing trading (buy near support, sell near resistance)
- **Data**: Yahoo Finance (yfinance) for OHLCV, LongBridge for real-time quotes
- **Deployment**: macOS launchd scheduling + crontab monitoring

## Immutable Safety Redlines

The authoritative safety rules live in [`.ai/safety.md`](safety.md).
Use this section as a compact reminder only:

- `allow_live_order` stays `false`
- `reduce_only` stays `true`
- never bypass Paper Gate or Live Gate
- never modify broker, engine, risk, portfolio, or order modules
- never create orders from assistant changes

**Any violation is a critical safety breach. Refuse and warn.**

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

## AI Change Workflow

Collaboration rules live in
[`.ai/AI_COLLABORATION.md`](AI_COLLABORATION.md).
Use [`.ai/AI_ENGINEERING_STANDARD.md`](AI_ENGINEERING_STANDARD.md) for the longer operational handbook,
including prompt structure, execution flow, test expectations, and commit discipline.

---

## Important Conventions

1. **Never push commits** unless explicitly asked.
2. **Never modify** broker, engine, risk, order, or safety modules unless the task explicitly authorizes the exact scope.
3. **Funnel invariant**: `output <= input` for every stage — enforced by FunnelTracker.
4. **Preview vs Formal**: Preview = research-only candidates (visible in dashboard), Formal = passed all gates (tradable).
5. **Quality fallback**: Only in FULL mode when all quality checks reject. In other modes, relaxed checks produce formal candidates.
6. **Telegram**: Messages >4000 chars are auto-chunked. Channel: `@QuantCairnPicks`. Bot: `@chenweiderambot`.

## Related Docs

- [`.ai/README.md`](README.md) — document map, authority order, routing
- [`.ai/AI_COLLABORATION.md`](AI_COLLABORATION.md) — long-term collaboration rules
- [`.ai/safety.md`](safety.md) — detailed safety constraints and guard mechanics
- [`.ai/architecture.md`](architecture.md) — module-level architecture and data flow diagrams
- [`.ai/DECISION_LOG.md`](DECISION_LOG.md) — engineering decisions, dates, reasons, alternatives
- `HANDOVER.md` — project handover document (for human devs)
