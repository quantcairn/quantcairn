# Repository Boundary — AI Agent Reference

> **Purpose**: Define what is public, what is private, and what must never be committed — so every AI coding assistant (Claude, Codex, Cursor, Aider, etc.) applies the boundary correctly.
> **Created**: v0.12.3 (2026-07-26)
> **Related**: [`.ai/safety.md`](safety.md), [`.gitignore`](../.gitignore)

---

## 1. Repository Model

### Current: Single Repository with `.gitignore` Boundary

```
Working Copy: soxs-range-arbitrage/
                    │
    ┌───────────────┼───────────────┐
    ▼                               ▼
Tracked (Public)              Gitignored (Private)
git push ──►                  NEVER pushed
github.com/quantcairn/
quantcairn
```

There is **no separate private repository**. The boundary is defined entirely by `.gitignore`. All tracked files are pushed to the public `github.com/quantcairn/quantcairn` repository.

---

## 2. Public Repository (Tracked in Git)

These files and directories **are pushed to GitHub** and visible to the world.

| Category | Path | Notes |
|---|---|---|
| Core source | `src/` | 23 packages; safety modules at `src/broker/`, `src/engine/`, `src/risk/`, `src/safety/` are public code but NEVER modifiable (see `safety.md`) |
| Public API | `quantcairn/` | 21 public symbols (`AIStrategySelector`, `DemoDataProvider`, `FunnelTracker`, etc.) |
| Tests | `tests/` | 133 files, 1075+ tests |
| AI context | `.ai/` | `CLAUDE.md`, `safety.md`, `architecture.md`, `DECISION_LOG.md`, `REPOSITORY_BOUNDARY.md` |
| User docs | `docs/` | `API.md`, `PRODUCT_OVERVIEW.md`, plus historical migration docs (bannered) |
| Examples | `examples/basic_demo.py` | Minimal API usage |
| CI/CD | `.github/workflows/` | `test.yml`, `release.yml` |
| Package metadata | `pyproject.toml`, `requirements.txt`, `dev-requirements.txt` | Package name: `quantcairn` |
| Project meta | `README.md`, `LICENSE` (Apache 2.0), `CHANGELOG.md`, `CONTRIBUTING.md`, `ROADMAP.md` | |
| Base config | `config.yaml` | **Public-safe defaults only** — broker credentials empty, mode=paper, no tokens |
| Config template | `config.sample.yaml` | Sample for new users to copy |
| Symbol configs | `configs/` (individual YAMLs) | AMC, DRIP, LABD, NAIL, QBTS, SMR, WULF — symbol-level parameters. Does NOT include `configs/TOP*.yaml` or `configs/SOXS.yaml` (gitignored) |
| Demo config | `configs/examples/TOP_DEMO.yaml` | Demo mode example |
| Env template | `.env.ai_selector.local.example` | Example env file — **no real secrets** |
| Scripts | `scripts/*.py`, `scripts/*.sh` | CLI tools, diagnostics, demo (some are operational — see banners) |
| Reference data | `data/sp500_sample.txt` | S&P 500 ticker list |
| Utilities | `run_tests.py`, `parse_trades.py`, `health_check.sh`, `monitor.sh` | |
| TradingView | `tradingview/soxs_range_strategy.pine` | Pine Script (historical, SOXS-specific) |
| Handoff | `HANDOFF.md` | Public project handoff document |

---

## 3. Private Runtime Boundary (Gitignored — NEVER Push)

These files and directories are **excluded from Git** and must **never** be committed or pushed.

### Secrets & Credentials

| Path | Contains | Risk if Exposed |
|---|---|---|
| `config.local.yaml` | Broker credentials, Telegram tokens, account config | Real-money trading capability |
| `.env.ai_selector.local` | OpenAI API key, Telegram tokens, personal filesystem paths | API abuse, credential theft |
| `.env` | Environment variable overrides | May contain tokens |

### Runtime State & Data

| Path | Contains |
|---|---|
| `state/` | AI selection state, paper portfolio state, broker cache, risk state, notification ledger, selection bundles, sell locks, yfinance cache, order state, position sync |
| `artifacts/` | Selection artifacts, funnel reports, backtest results, learning governance data, candidate datasets, shadow data, universe snapshots |
| `reports/` | Daily AI selection JSON reports (historical) |
| `quote/` | Real-time quote logs (daily files) |
| `logs/` | Runtime logs (combined, engine, per-symbol) |
| `runtime/` | Process PIDs |
| `data/market/` | Cached market data |
| `site/` | Generated research site |

### Generated Trading Configs

| Path | Contains |
|---|---|
| `configs/TOP1.yaml`, `configs/TOP2.yaml`, `configs/TOP3.yaml` | Daily generated trading configs consumed by engine |
| `configs/SOXS.yaml` | SOXS-specific trading parameters |
| `config/candidate_models/` | Candidate model configurations |

### Other Private Files

| Path | Notes |
|---|---|
| `private_ops/` | Personal deployment scripts, launchd plists, auto_trade.sh |
| `HANDOVER.md` | Personal project handover (cf. public `HANDOFF.md`) |
| `*.log`, `*.pid` | Any log or PID files |
| `dist/`, `build/`, `*.egg-info/` | Build artifacts |

---

## 4. Forbidden Exports — AI Agent Rules

**Any AI assistant must refuse to commit, stage, or push any file that matches these rules:**

| Rule | Check |
|---|---|
| **NEVER** commit any file containing an API key or token | `grep -E 'sk-[A-Za-z0-9]{20,}'` |
| **NEVER** commit any file containing broker credentials | `app_key`, `app_secret`, `access_token` with non-empty values |
| **NEVER** commit any file with absolute paths to `/Users/` or `$HOME/` | Paths that identify the maintainer's machine |
| **NEVER** commit files from `state/`, `artifacts/`, `logs/`, `reports/`, `quote/`, `runtime/` | These directories are entirely gitignored |
| **NEVER** commit `config.local.yaml` or `.env.ai_selector.local` (the real ones, not `.example`) | Secrets live here |
| **NEVER** commit `configs/TOP*.yaml` or `configs/SOXS.yaml` | Generated trading configs |
| **NEVER** commit `private_ops/` | Personal deployment tooling |
| **NEVER** commit `HANDOVER.md` | Personal handover document |

### If Asked to Commit a Forbidden File

1. **Refuse** — state which rule would be violated.
2. **Warn** — explain the risk (credential exposure, personal path leak, etc.).
3. **Suggest** — propose the correct alternative (use `.example` template, use env vars, etc.).

### Emergency: If a Secret Was Committed

1. **Alert the human immediately.**
2. **Do NOT push.**
3. Human must rotate the exposed credentials (API keys, tokens).
4. Use `git filter-repo` or `git rebase -i` to purge from history.
5. Verify with `git log -p` that no trace remains.

---

## 5. Future Architecture (Pro Boundary)

**Do not create these repositories now.** This section documents the planned evolution so AI agents understand the long-term boundary.

```
Future State:

┌─────────────────────────┐
│ quantcairn/quantcairn   │  ← PUBLIC — Community Edition (Apache 2.0)
│                         │
│ Research framework      │
│ Selection pipeline      │
│ Scoring model           │
│ Backtesting             │
│ Demo mode               │
│ Documentation           │
└─────────────────────────┘
            ▲
            │ imported as dependency
            │
┌─────────────────────────┐
│ quantcairn/pro          │  ← PRIVATE — Commercial Pro Edition
│                         │
│ SaaS backend            │
│ User accounts & auth    │
│ Billing & subscriptions │
│ Cloud execution engine  │
│ Enterprise features     │
│ API gateway             │
└─────────────────────────┘

┌─────────────────────────┐
│ quantcairn/data         │  ← PRIVATE — Data & Analytics
│                         │
│ User feedback           │
│ Analytics & telemetry   │
│ Model training data     │
│ Outcome datasets        │
│ Performance metrics     │
└─────────────────────────┘
```

**Key rules for Pro boundary:**
- Pro imports `quantcairn` as a library — it does NOT fork the public repo
- Pro adds commercial features on top of the CE foundation
- Data is strictly separated — never in the same repo as code
- CE features are developed in the public repo, not backported from Pro

---

## 6. What Goes Where (Decision Reference)

| Concern | Goes In | Rationale |
|---|---|---|
| Core pipeline (`src/openalpha/`) | Public CE | Foundation of the platform |
| Scoring model (`src/scoring/`) | Public CE | Research framework |
| Universe management | Public CE | Configuration, not data |
| Backtesting, regime detection | Public CE | Research tools |
| Dashboard (`src/dashboard/`) | Public CE | Read-only, no sensitive logic |
| Safety guards (`src/safety/`) | Public CE | Transparency — users should see the protections |
| Paper broker (`src/broker/paper_broker.py`) | Public CE | Simulation code is educational |
| Real broker credentials | Private runtime | Secrets (gitignored) |
| Trading engine configs (TOP*.yaml) | Private runtime | Generated, contains position data |
| Portfolio state | Private runtime | Personal financial data |
| Trade outcomes | Private data (future) | Training data for ML |
| User accounts, billing, auth | Private Pro (future) | Commercial SaaS |
| Cloud execution, API gateway | Private Pro (future) | Infrastructure |
| Analytics, telemetry | Private data (future) | User data |

---

## 7. Environment Variable Naming

| Current Prefix | Status | Migration |
|---|---|---|
| `QUANTCAIRN_*` | ✅ Current standard | Use for new vars |
| `OPENALPHA_*` | ⚠️ Legacy | Migrate to `QUANTCAIRN_*` in v0.13.x |
| `SOXS_*` | ⚠️ Legacy (original project name) | Migrate to `QUANTCAIRN_*` in v0.13.x |

**AI agent rule**: When adding new environment variables, use `QUANTCAIRN_*` prefix. Do NOT introduce new `OPENALPHA_*` or `SOXS_*` names.

---

## 8. Pre-Commit Checklist for AI Agents

Before proposing any commit:

1. [ ] Read `.ai/safety.md` — identify if any never-modify modules are touched
2. [ ] Read this file — verify no forbidden exports
3. [ ] Run `git diff --stat` — verify only intended files
4. [ ] Run `git status` — check for untracked files that might be secrets
5. [ ] Scan diff for secrets: `git diff --cached | grep -E '(sk-[A-Za-z0-9]{20,}|app_secret.*["\x27][A-Za-z0-9])'`
6. [ ] Run `pytest tests/ -q` — confirm no new failures
7. [ ] If behavior changed: update `CHANGELOG.md`
8. [ ] If architecture changed: update `.ai/DECISION_LOG.md`
9. [ ] **Wait for explicit human approval** before `git push`

---

*This document is part of the QuantCairn AI context layer. It should be updated whenever the public/private boundary changes.*
