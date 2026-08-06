# Introducing QuantCairn v0.17.0

## Building an AI-Powered Quantitative Research Intelligence Platform

---

## Introduction

QuantCairn is an **AI-powered quantitative research platform** for US equity selection, validation, simulation, and research intelligence.

It solves a problem common to quantitative research: **signal generation is easy, but validation and reproducibility are hard**. QuantCairn provides a complete research stack — from selection through outcome measurement, paper trading simulation, and research intelligence — so every decision is traceable and every analysis is reproducible.

The platform operates under a strict research-first architecture: the selection pipeline produces candidates, the research layer evaluates them, and the paper trading layer simulates what would have happened. Live trading is architecturally separate and disabled by default.

---

## Why QuantCairn

Traditional quantitative systems face several challenges:

- **Signal generators without validation** — a model can pick stocks, but how do you know the picks were good?
- **Backtesting without forward evaluation** — historical backtests can be overfit; true walk-forward validation is rare
- **Black-box scoring** — when a stock is rejected, the reason is often opaque
- **No paper trading bridge** — going from research to simulated execution requires separate infrastructure

QuantCairn addresses each of these:

| Challenge | QuantCairn Approach |
|---|---|
| Signal without validation | Every selection is stored in an immutable ledger; forward outcomes are backfilled automatically |
| No forward evaluation | Walk-forward analysis across configurable rolling time windows |
| Black-box scoring | 9-stage pipeline with per-symbol rejection reasons at every stage |
| No paper trading bridge | Complete Phase 5 stack: trade creation → position tracking → analytics |

---

## Architecture

QuantCairn's research stack spans 6 phases across 8+ research modules:

```
Selection Pipeline (9 stages)
        │
        ▼
Selection Ledger           ← Phase 1: immutable per-run snapshots
        │
        ▼
Historical Outcome Backfill ← Phase 2: forward returns, MFE/MAE, range eval
        │
        ▼
Learning Dataset           ← Phase 2B: ML-ready JSONL (10 features, 7 labels)
        │
        ▼
Research Analytics         ← Phase 2C: performance, feature correlation, sectors
        │
        ▼
Walk Forward Validation    ← Phase 3: rolling time-window stability analysis
        │
        ▼
Research Registry          ← Phase 4: run history, dataset growth, ML readiness
        │
        ▼
Paper Trading Research     ← Phase 5: simulated positions, path-based risk
        │
        ▼
Benchmark · Regime · Report ← Phase 6: unified intelligence layer
        │
        ▼
Research Center Dashboard  ← Phase 6D: all research data in one view
```

**Key invariant**: Every pipeline stage enforces `output_count <= input_count`. Production trading modules (broker, engine, risk, safety) are never imported by research modules.

---

## Platform Evolution

QuantCairn has been built in phases, each adding a layer of research capability:

### Phase 1 — Selection Ledger (v0.13.0)
Immutable JSON snapshots of every formal candidate per selection run. Written fire-and-forget after pipeline completion — never blocks selection.

### Phase 2 — Historical Evaluation (v0.13.0)
True path-based MFE/MAE from OHLCV bars (not two-point approximation). Forward return computation with trading-day-aware offsets. Range trading success/failure evaluation. ML-ready dataset joining selections with outcomes.

### Phase 3 — Validation Platform (v0.14.0)
Configurable rolling walk-forward analysis. Per-period performance metrics, feature stability detection, sector stability analysis. Robustness flags for low samples and performance degradation.

### Phase 4 — Research Observability (v0.15.0)
Selection run history registry. Dataset growth tracking. Market regime tagging (bull/bear/sideways from bundle benchmarks). Research quality reports. ML readiness assessment (observation only — never triggers training). Dashboard API integration.

### Phase 5 — Paper Research Platform (v0.16.0)
Bridge from selection to simulated paper trading. Position snapshots with current prices, unrealized returns, path-based risk metrics. Exit signals are **observations only** — the tracker never modifies trade state. Aggregate performance analytics by sector and factor.

### Phase 6 — Research Intelligence (v0.17.0)
**Research Benchmark** — Unified composite A-F grade synthesizing all four research pipelines.

**Regime Analysis** — Per-regime breakdown (bull/bear/sideways) comparing selection and paper performance. Regime robustness scoring.

**Research Report** — Structured JSON report with executive summary, risk detection (LOW_SAMPLE_SIZE, REGIME_DEPENDENCY, PERFORMANCE_DEGRADATION), and research recommendations.

**Research Center Dashboard** — Single-page view of the entire research stack. Read-only, zero recomputation during requests.

---

## Research Safety

QuantCairn's research layer is architecturally isolated from production trading:

**Research modules never:**
- Import from broker, engine, risk, or safety modules
- Create, modify, or cancel orders
- Change risk parameters or position limits
- Automatically optimize strategy weights
- Trigger ML training

**Research modules only:**
- Read pre-computed JSON artifacts from `artifacts/learning/`
- Write observations to their own isolated directories
- Produce reports and analytics for human review

**The selector hook** (fire-and-forget pattern, `try/except: pass`) records selection metadata without affecting pipeline output. If the hook fails, the selection continues unchanged.

---

## Dashboard

QuantCairn includes research visualization dashboards accessible at port 8090:

| Dashboard | URL | What It Shows |
|---|---|---|
| **Research Center** | `/research-center` | Benchmark grade, regime analysis, walk-forward stability, paper research metrics, risk flags, recommendations |
| **Paper Research** | `/paper-research` | Portfolio overview, current positions table, performance analytics, factor correlation, sector breakdown |
| **Main Dashboard** | `/` | Selection status, engine health, position summary, mode consistency |

All dashboards are read-only HTML pages with client-side JavaScript. They fetch pre-computed JSON from API endpoints — no computation, no yfinance calls, no network requests during page load. Missing data shows `available: false` gracefully.

---

## Getting Started

```bash
git clone git@github.com:quantcairn/quantcairn.git && cd quantcairn
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python scripts/run_demo_selector.py
```

The demo runs with deterministic synthetic data — 5 symbols, 252 trading days each, zero API keys required. You'll see the full 9-stage selection pipeline produce research candidates.

For paper trading simulation:

```bash
QUANTCAIRN_EXECUTION_MODE=PAPER .venv/bin/python scripts/run_ai_selector.py --universe-source managed
```

For the research dashboard:

```bash
.venv/bin/python scripts/run_dashboard.py
# Then open http://localhost:8090/research-center
```

---

## Current Status

**v0.17.0 — Research Intelligence Platform** — is the current release. All Phase 1-6 capabilities are implemented and tested.

**Test coverage**: 300+ tests across 17 test suites. Zero protected module modifications since v0.13.0.

---

## Future Roadmap

### Phase 7 — ML Research Sandbox

A controlled environment for:

- Feature importance analysis
- Model architecture comparison
- Validation experiments on historical data

**Not**: automatic trading, live model deployment, or production weight changes. The sandbox is a research tool — it evaluates, it does not execute.

---

## Links

- [GitHub Repository](https://github.com/quantcairn/quantcairn)
- [Decision Log](https://github.com/quantcairn/quantcairn/blob/main/.ai/DECISION_LOG.md)
- [Safety Constraints](https://github.com/quantcairn/quantcairn/blob/main/.ai/safety.md)
- [Architecture Reference](https://github.com/quantcairn/quantcairn/blob/main/.ai/architecture.md)
- [License (Apache 2.0)](https://github.com/quantcairn/quantcairn/blob/main/LICENSE)

---

*QuantCairn is for research and educational purposes only. It does not provide financial advice or guarantee investment results.*
