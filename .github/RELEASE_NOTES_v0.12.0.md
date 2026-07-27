# QuantCairn v0.12.0 Public Beta

QuantCairn is an AI-powered quantitative research platform for US equity selection. This is the first public beta release.

## Highlights

- **9-stage stock selection pipeline** with funnel invariant enforcement (every stage: output ≤ input)
- **Multi-factor scoring engine** — volatility, volume, trend, repeatability, drawdown (30/20/20/15/10 weights)
- **Managed universe** — 35 symbols across index ETFs, mega caps, semiconductors, sector ETFs, and more
- **37 fallback profiles** — every symbol has a recovery path when data sources are unavailable
- **Paper trading validation** — end-to-end simulated execution with P&L tracking and portfolio persistence
- **Mode-aware quality filtering** — strict spread checks during market hours, relaxed EOD validation otherwise
- **Three execution modes** — LIVE (safety-disabled), PAPER (simulated), RESEARCH (candidates only)
- **Telegram notifications** — auto-chunked reports to channel, deduplication ledger
- **Research dashboard** — read-only HTML dashboard (port 8090), no trade buttons, no broker calls
- **Demo mode** — deterministic synthetic data, 5 symbols, 252 trading days, zero API keys required
- **Safety-first architecture** — `allow_live_order=false` enforced at every layer, `reduce_only=true`

## Current Status

QuantCairn focuses on quantitative research and paper validation. Live trading is architecturally disabled — it requires explicit configuration changes across three independent safety gates. The selector and trading engine are completely decoupled (file-based YAML interface). The dashboard is strictly read-only.

**This project is not financial advice and does not guarantee investment results.**

## What's Included

- **Source code**: `src/openalpha/` (selection pipeline), `src/scoring/`, `src/broker/`, `src/engine/`
- **Scripts**: `scripts/run_ai_selector.py`, `scripts/run_demo_selector.py`, `scripts/status.py`
- **Python package**: `pyproject.toml`, `quantcairn/` namespace (21 public API symbols)
- **Tests**: 1075+ pytest tests, GitHub Actions CI
- **AI documentation layer**: `.ai/` (CLAUDE.md, safety.md, architecture.md, DECISION_LOG.md)

## Quick Start

```bash
git clone git@github.com:quantcairn/quantcairn.git && cd quantcairn
python3 -m venv .venv && .venv/bin/pip install -e .

# 30-second demo (no API keys required)
.venv/bin/python scripts/run_demo_selector.py

# Full selection pipeline
.venv/bin/python scripts/run_ai_selector.py --universe-source managed
```

## Documentation

| Document | Description |
|---|---|
| [Product Overview](docs/PRODUCT_OVERVIEW.md) | What QuantCairn is, what it can do, what it won't |
| [Current System State](docs/CURRENT_SYSTEM_STATE.md) | Maintainer-oriented system audit |
| [Architecture Reference](.ai/architecture.md) | Module map, data flow, pipeline stage details |
| [Safety Constraints](.ai/safety.md) | Immutable safety rules |
| [Decision Log](.ai/DECISION_LOG.md) | 15 engineering decisions with rationale |
| [API Reference](docs/API.md) | Public Python API surface |
| [Contributing](CONTRIBUTING.md) | Development workflow and PR guidelines |
| [Roadmap](ROADMAP.md) | Completed and planned work |
| [Changelog](CHANGELOG.md) | Release history |

## License

Apache 2.0 — see [LICENSE](LICENSE)
