# QuantCairn

[![test](https://github.com/quantcairn/quantcairn/actions/workflows/test.yml/badge.svg)](https://github.com/quantcairn/quantcairn/actions/workflows/test.yml)

AI-powered quantitative research platform for US equity selection.

[![release](https://img.shields.io/badge/release-v0.17.0-blue)](https://github.com/quantcairn/quantcairn/releases)

QuantCairn builds a comprehensive research stack: selection → validation → paper trading simulation → research intelligence. Every selection is traceable, every outcome is measurable, and every analysis is reproducible.

The platform is designed for:

- quantitative research
- paper trading simulation
- strategy evaluation
- model transparency
- research-driven iteration


## Research Pipeline

QuantCairn's research stack spans 6 phases across 8+ research modules:

Selection
↓
Selection Ledger *(Phase 1)*
↓
Historical Outcome Backfill *(Phase 2)*
↓
Learning Dataset
↓
Research Analytics
↓
Walk Forward Validation *(Phase 3)*
↓
Research Registry *(Phase 4)*
↓
Paper Trading Research *(Phase 5)*
↓
Benchmark · Regime · Report *(Phase 6)*
↓
Research Center Dashboard


## Core Capabilities

### AI Selection Engine

- multi-factor scoring
- volatility analysis
- liquidity evaluation
- trend analysis
- drawdown safety
- gap risk analysis
- portfolio diversification


### Research Infrastructure

- immutable selection ledger
- forward outcome evaluation
- MFE / MAE analysis
- range-trading success measurement
- feature-performance analysis
- sector performance analytics


### Transparency

Every selection can be traced:

- why it was selected
- why candidates were rejected
- how selections performed afterwards


## Current Scope

QuantCairn currently supports:

- US equities and ETFs
- research mode
- paper trading workflows
- historical evaluation pipelines


QuantCairn is intended for research and simulation purposes only.
It does not provide financial advice or guarantee investment results.

> *Formerly developed under the internal project name OpenAlpha.*

---

## Quick Start (30 seconds, no API keys)

```bash
git clone git@github.com:quantcairn/quantcairn.git && cd quantcairn
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python scripts/run_demo_selector.py
```

The demo uses **deterministic synthetic data** — 5 symbols, 252 trading days each, no network required. You'll see the full research pipeline run and produce research candidates.

---

## Features

| Category | Capabilities |
|---|---|
| **Selection Pipeline** | Multi-stage research pipeline: Universe → Scoring → Quality → Diversity → TOP K. Funnel invariant enforced per stage. |
| **Scoring Model** | Multi-factor (30/20/20/15/10): volatility, volume, trend, repeatability, drawdown. 37 fallback profiles. |
| **Quality Filtering** | Mode-aware: strict spread checks during market hours, relaxed with EOD data otherwise. |
| **Paper Trading** | End-to-end verified: selector → config → simulated fills → portfolio persistence → dashboard. |
| **Diagnostics** | Per-symbol elimination tracing. Funnel audit trail. Preflight market state detection. |
| **Notifications** | Telegram (`@QuantCairnPicks`), console, macOS, webhook. Long messages auto-chunked. |
| **Dashboard** | Read-only HTML (port 8090). No trade buttons, no broker calls. |
| **Demo Mode** | Zero-dependency evaluation with seeded random walk data. |
| **Execution Modes** | RESEARCH (candidates only) / PAPER (relaxed, simulated). Live execution is architecturally disabled in the public release. |

---

## Architecture

```
Market Data ──→ Preflight ──→ Research Pipeline ──→ FunnelTracker (audit)
  (Yahoo/                                 │
  LongBridge)              ┌───────────────┼───────────────┐
                           ▼               ▼               ▼
                      Notifier        Config Writer     Dashboard
                     (Telegram)      (TOP{1,2,3}.yaml)   (port 8090)
                                           │
                                     Trading Engine
                                    (paper / sandbox)
```

**Key invariant**: Every pipeline stage enforces `output_count <= input_count`.

---

## Paper Trading

QuantCairn includes a complete paper trading simulation environment. Run the selector in PAPER mode to produce trade-eligible candidates with confidence scores:

```bash
QUANTCAIRN_EXECUTION_MODE=PAPER .venv/bin/python scripts/run_ai_selector.py --universe-source managed
```

The paper broker simulates fills with realistic slippage and commissions. Positions are persisted to `state/paper/{account}/portfolio_state.json`. The dashboard displays open positions, unrealized P&L, and account equity.

**Live trading is architecturally disabled** — `allow_live_order` is forced to `false` at three independent layers regardless of configuration.

---

## Dashboard

QuantCairn includes research visualization dashboards (port 8090):

| Page | URL | Purpose |
|---|---|---|
| Main Dashboard | `/` | Selection status, engine health, position summary |
| Research Center | `/research-center` | Benchmark, regime analysis, validation, report |
| Paper Research | `/paper-research` | Paper trades, position tracking, performance analytics |
| Research Report | `/research` | Daily research digest |

All dashboards are read-only — zero recomputation during requests, zero network calls. Pre-computed JSON artifacts power every page.

---

## Video DevLog

### Episode 001 — Building QuantCairn v0.17.0

[![QuantCairn v0.17.0 — Research Intelligence Platform](https://img.youtube.com/vi/8MLKDTAsiT8/0.jpg)](https://youtu.be/8MLKDTAsiT8)

I built an AI-powered quantitative research platform with AI.

This episode introduces:

- QuantCairn architecture and 6-phase research stack
- Research-first safety design
- Research Center Dashboard
- Paper Research workflow
- AI-assisted development approach

📺 **[Watch on YouTube](https://youtu.be/8MLKDTAsiT8)**

---

## Safety

QuantCairn is a **research tool, not a trading bot**. Key safety invariants:

- `allow_live_order` is **forced to `false`** — cannot be overridden
- Trading engine defaults to **reduce-only** mode — no new positions opened
- **Selector and Engine are decoupled** — the selector writes YAML configs; the engine reads them
- **Paper trading is the default** — live trading requires explicit multi-layer approval
- **Learning governance** requires explicit human approval before any model weight change

See [`.ai/safety.md`](.ai/safety.md) for the full safety specification.

---

## Commands

```bash
# Daily research selection
.venv/bin/python scripts/run_ai_selector.py --universe-source managed

# Paper trading simulation
QUANTCAIRN_EXECUTION_MODE=PAPER .venv/bin/python scripts/run_ai_selector.py --universe-source managed

# System health check (read-only)
.venv/bin/python scripts/status.py

# Developer environment check
.venv/bin/python scripts/check_dev_environment.py

# Run tests
.venv/bin/python -m pytest tests/ -q
```

---

## Project Structure

```
quantcairn/
├── src/
│   ├── openalpha/          Core: selection pipeline, diagnostics, preflight
│   ├── scoring/            Multi-factor scoring model
│   ├── universe/           Managed symbol universe (US equities and ETFs)
│   ├── broker/             Paper broker, portfolio state, LongBridge integration
│   ├── engine/             Trading engine (selector-independent)
│   ├── risk/, safety/      Risk management, LiveGuard, environment guard
│   ├── notifier/           Telegram, webhook, macOS notifications
│   ├── dashboard/          Read-only combined dashboard (Jinja2 HTML)
│   ├── outcome/            Trade outcome collection, governance, weight advisor
│   ├── backtest/, regime/  Backtesting framework, market regime detection
│   └── strategy/, shadow/  Strategy definitions, shadow trading observation
├── scripts/                CLI tools, wrappers, diagnostics
├── tests/                  pytest: 1075+ tests, 59+ core integration tests
├── .ai/                    AI assistant context layer (CLAUDE.md, safety, architecture, decisions)
├── quantcairn/             Public Python API namespace
├── examples/               Minimal API usage examples
├── docs/                   Product overview, API reference, migration plans
├── configs/                Generated TOP{1,2,3}.yaml configs
└── state/                  Runtime state (portfolios, notifications, selection markers)
```

---

## Documentation

| Document | For |
|---|---|
| [Product Overview](docs/PRODUCT_OVERVIEW.md) | What QuantCairn is, what it can do, what it won't |
| [Decision Log](.ai/DECISION_LOG.md) | Why 15 major engineering decisions were made |
| [Architecture Reference](.ai/architecture.md) | Module map, data flow, pipeline stage details |
| [Safety Constraints](.ai/safety.md) | Immutable rules — what must never change |
| [API Reference](docs/API.md) | Public Python API surface |
| [Contributing](CONTRIBUTING.md) | Development workflow and PR guidelines |
| [Roadmap](ROADMAP.md) | Completed and planned work |
| [Changelog](CHANGELOG.md) | Release history |

---

## Release History

| Version | Name | Highlights |
|---|---|---|
| **v0.17.0** | Research Intelligence | Benchmark framework, regime analysis, research report, research center dashboard |
| **v0.16.0** | Paper Research | Paper trading bridge, position tracker, paper analytics, paper dashboard |
| **v0.15.0** | Research Observability | Research history registry, observability dashboard, dataset quality tracking |
| **v0.14.0** | Validation Platform | Walk-forward validation, research analytics API |
| **v0.13.0** | Research Platform | Selection ledger, outcome backfill, learning dataset, analytics engine |

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

---

## Roadmap

**Completed (v0.17.0)**:
- [x] Selection pipeline, demo mode, diagnostics, Telegram, dashboard, CI, packaging, open-source foundation, selection ledger with backfill, walk-forward validation, research analytics, research registry, observability APIs, paper research bridge, position tracking, paper analytics, research benchmark, regime analysis, research report, research center dashboard

**Next (Phase 7)**:
- [ ] ML research sandbox — feature analysis experiments, model comparison, validation only. No auto-training, no production model replacement.

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## Disclaimer

**This project is for research and educational purposes only.** QuantCairn is not financial advice, investment advice, or a trading recommendation. The system runs in paper/sandbox mode by default. Live trading is architecturally prevented. Past pipeline performance does not guarantee future results.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
