# QuantCairn

[![test](https://github.com/quantcairn/quantcairn/actions/workflows/test.yml/badge.svg)](https://github.com/quantcairn/quantcairn/actions/workflows/test.yml)

**AI-driven quantitative research platform for US equity selection.**

QuantCairn runs a 9-stage analytical pipeline that screens a managed universe of 35 stocks and ETFs, scores them with a multi-factor model, and produces daily candidate selections. Designed for research, paper trading, and transparency.

> *Formerly developed under the internal project name OpenAlpha.*

---

## Quick Start (30 seconds, no API keys)

```bash
git clone git@github.com:quantcairn/quantcairn.git && cd quantcairn
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python scripts/run_demo_selector.py
```

The demo uses **deterministic synthetic data** — 5 symbols, 252 trading days each, no network required. You'll see the full 9-stage pipeline run and produce research candidates.

---

## Features

| Category | Capabilities |
|---|---|
| **Selection Pipeline** | 9-stage pipeline: Universe → Scoring → Quality → Diversity → TOP K. Funnel invariant enforced per stage. |
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
Market Data ──→ Preflight ──→ 9-Stage Pipeline ──→ FunnelTracker (audit)
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
│   ├── universe/           Managed symbol universe (35 symbols)
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

## Roadmap

**Completed (v0.12.0)**:
- [x] 9-stage pipeline, mode-aware quality filtering, paper trading, demo mode, diagnostics, Telegram, dashboard, CI, packaging, AI context layer, open-source foundation

**Next (v0.13.0)**:
- [ ] Scoring rejection reason propagation, outcome collector auto-trigger, dashboard mode consistency, multi-provider data fallback

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## Disclaimer

**This project is for research and educational purposes only.** QuantCairn is not financial advice, investment advice, or a trading recommendation. The system runs in paper/sandbox mode by default. Live trading is architecturally prevented. Past pipeline performance does not guarantee future results.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
