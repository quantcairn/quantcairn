# Contributing to QuantCairn

> *Formerly developed under the internal project name OpenAlpha.*

## Welcome

QuantCairn is an open-source AI-driven research system for US stock selection. We welcome contributions that improve the pipeline, diagnostics, documentation, and developer experience.

Before contributing, please read through this guide and our safety documentation.

## Development Workflow

### 1. Read the AI Context Layer First

The `.ai/` directory contains the authoritative project context used by both human developers and AI assistants:

- [`.ai/CLAUDE.md`](.ai/CLAUDE.md) — project identity, module map, pipeline overview, commands
- [`.ai/safety.md`](.ai/safety.md) — immutable safety constraints (MUST read)
- [`.ai/architecture.md`](.ai/architecture.md) — data flow, module dependencies, pipeline stage details
- [`.ai/DECISION_LOG.md`](.ai/DECISION_LOG.md) — historical engineering decisions and their reasons

### 2. Understand the Architecture

QuantCairn has strict module boundaries. Key principles:

- **The Selector writes configs; the Engine reads them.** No runtime coupling.
- **The Dashboard is read-only.** It reads artifacts, never initiates actions.
- **The Notifier is fire-and-forget.** Notification failure never blocks selection.

See [`.ai/architecture.md`](.ai/architecture.md) for the full dependency graph.

### 3. Follow Safety Constraints

**These constraints are non-negotiable.** Never modify:

| Module | Reason |
|---|---|
| `src/broker/` | Broker API — connects to LongBridge |
| `src/engine/trading_engine.py` | Core trading loop |
| `src/risk/manager.py` | Risk management |
| `src/portfolio/` | Portfolio state |
| `src/order/` | Order creation |
| `src/safety/` | Pre-flight guards and environment validation |

See [`.ai/safety.md`](.ai/safety.md) for the complete safety specification.

### 4. Create Focused Commits

- One logical change per commit
- Use conventional commit prefixes: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- Write descriptive commit messages explaining *why*, not just *what*

### 5. Add Tests for Behavior Changes

- If you change pipeline behavior, add tests
- Run `pytest tests/ -q` and confirm no new failures
- The 5 pre-existing env-leak failures are known and unrelated

## Local Development

### Environment Setup

```bash
# 1. Clone
git clone https://github.com/quantcairn/quantcairn.git
cd quantcairn

# 2. Create venv and install in editable mode
python3 -m venv .venv
.venv/bin/pip install -e .

# 3. Verify your environment
.venv/bin/python scripts/check_dev_environment.py
```

### Running Tests

```bash
# Full suite
.venv/bin/python -m pytest tests/ -q

# Specific test groups
.venv/bin/python -m pytest tests/test_demo_data.py -v
.venv/bin/python -m pytest tests/test_public_api.py -v
.venv/bin/python -m pytest tests/test_funnel_invariant.py -v

# Skip slow tests
.venv/bin/python -m pytest tests/ -q -m "not slow"
```

### Running the Demo

```bash
# Full pipeline (9 stages, deterministic data, no API keys)
.venv/bin/python scripts/run_demo_selector.py

# Basic Python API example
.venv/bin/python examples/basic_demo.py
```

### Checking Your Work

```bash
# Environmental checks (Python version, imports, demo)
.venv/bin/python scripts/check_dev_environment.py

# Market data diagnostics (if you have network access)
.venv/bin/python scripts/diag_market_data.py
```



- Follow existing code conventions in each module
- Use type hints for function signatures
- Prefer descriptive names over abbreviations
- Keep functions focused — if a function exceeds ~50 lines, consider splitting

### Documentation

- Update `.ai/CLAUDE.md` if module structure changes
- Update `.ai/DECISION_LOG.md` for new architectural decisions
- Use English for code comments and documentation
- Docstrings should explain *why* a function exists, not *what* it does line-by-line

### Refactoring

- Avoid cosmetic refactoring in PRs that fix bugs or add features
- If you must refactor, do it in a separate commit
- Never refactor safety-critical modules without explicit discussion

## Pull Request Guidelines

### Before Opening a PR

1. Read `.ai/CLAUDE.md` and `.ai/safety.md`
2. Run `pytest tests/ -q` and confirm the test suite passes or has only known failures
3. Check `git status` and verify only intended files are modified

### PR Description

Every PR should include:

- **Motivation** — why this change is needed
- **Design decisions** — what approach was chosen and why
- **Testing results** — test output or summary of test coverage
- **Risk impact** — which modules are affected and what could break

### Example PR Description

```markdown
## Motivation
DATA_QUALITY rejects all candidates during after-hours runs because
bid/ask spread data is unavailable outside market hours.

## Design
Made quality filtering mode-aware. Preflight run_mode now gates
filter strictness. FULL mode keeps existing strict checks.
EOD/AFTER_MARKET/DEGRADED modes skip spread-dependent checks.

## Testing
- 13 new tests in test_quality_mode_awareness.py — all pass
- 46 existing related tests — no regressions
- Full suite: 1109 passed, 8 pre-existing failures

## Risk
Low — only affects selector.py quality filter path.
Broker, Engine, RiskManager untouched.
```

## Safety Rules

### Redlines

1. **Never change `allow_live_order`** from its `false` default
2. **Never change `reduce_only`** from its `true` default
3. **Never modify** Broker, TradingEngine, RiskManager, PortfolioManager, Order modules, or Safety guards
4. **Never bypass** Paper Gate or Live Gate
5. **Never auto-approve** learning governance proposals

### If You're Unsure

If a change touches any file in `src/broker/`, `src/engine/`, `src/risk/`, `src/portfolio/`, `src/order/`, or `src/safety/`:

1. **Stop.** Re-read `.ai/safety.md`.
2. **Ask.** Open a discussion before opening a PR.
3. **Err on the side of caution.** A rejected PR is better than a safety breach.

## Getting Help

- Read the [README](README.md) for project overview
- Read the [Roadmap](ROADMAP.md) for planned work
- Check existing [Decision Log](.ai/DECISION_LOG.md) for previous design discussions
- Open a GitHub Discussion for questions
