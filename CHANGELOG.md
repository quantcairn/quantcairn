# Changelog

All notable changes to QuantCairn.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.12.5] — 2026-07-27

Scheduler and production hardening release.

### Changed

- AI selector launchd schedule migrated from 60-second polling to fixed Beijing-time calendar triggers
- Wrapper hardened with trading-day, time-window, and success-marker protection
- LongBridge and Outcome Collector tests stabilized for isolation
- Dashboard read-only status labels improved
- Notification ledger now records send success or failure accurately

### Safety

- No trading strategy changes
- No broker production logic changes
- Live trading remains disabled and gated

---

## [0.12.0] — 2026-07-25

First runnable QuantCairn demo release.

### Added

- **Demo mode**: `scripts/run_demo_selector.py` — full 9-stage pipeline with deterministic synthetic data, no API keys required
- **Demo data provider**: `DemoDataProvider` — seeded random walk OHLCV for 5 symbols (AAPL, MSFT, NVDA, SPY, TSLA), 252 trading days
- **Public Python namespace**: `quantcairn` — importable alongside existing `src.openalpha` paths
- **Public API surface**: 21 documented symbols in `quantcairn.__all__`, `docs/API.md` reference
- **Developer tooling**: `scripts/check_dev_environment.py`, `examples/basic_demo.py`, editable install support
- **GitHub Actions CI**: `test.yml` workflow — install, import check, test suite, demo validation
- **Open-source foundation**: LICENSE (Apache 2.0), CONTRIBUTING.md, ROADMAP.md, CHANGELOG.md
- **AI context layer**: `.ai/` — CLAUDE.md, safety.md, architecture.md, DECISION_LOG.md
- **Brand migration planning**: `docs/BRAND_MIGRATION.md`, `docs/GITHUB_MIGRATION.md`
- **Release templates**: `.github/RELEASE_TEMPLATE.md`, `docs/RELEASE_CHECKLIST.md`

### Changed

- **README** rewritten as open-source project landing page
- **Public branding** migrated from OpenAlpha to QuantCairn in documentation
- **Package configuration** `pyproject.toml` with editable install support and optional `[demo,test]` extras
- **GitHub remote** migrated to `quantcairn/quantcairn`

### Safety

- Demo mode does not connect to brokers or create orders
- `allow_live_order=false` and `reduce_only=true` enforced in all modes
- Pipeline invariant `output <= input` validated by FunnelTracker on every run
- Mode-aware quality filtering prevents false rejections outside market hours

### Tests

- 59+ core integration tests covering pipeline, quality, diagnostics
- 27 demo data tests covering format, reproducibility, zero-network guarantee
- 36 public API + namespace tests verifying compatibility
- CI workflow running on Python 3.11 and 3.14

---

## [0.12.1] — 2026-07-25

Public beta developer experience release.

### Added

- **CI status badge** in README (GitHub Actions `test.yml` workflow)
- **API smoke tests** (`tests/test_public_api_smoke.py`): 18 tests covering package import, `__all__` validation, demo provider, deterministic output, sub-module imports, zero-network guarantee, and zero-broker-import guarantee
- **Release workflow** (`.github/workflows/release.yml`): triggers on `v*` tags, builds wheel + sdist, validates package metadata, smoke-tests the installed wheel, uploads artifacts
- **Public channel Telegram template** (`_build_public_channel_message`): clean ≤1500-char format for `@QuantCairnPicks` — shows trading date, market status, TOP candidates with AI scores, risk level. Hides pipeline internals, provider audit, and fallback debug info.
- **Admin debug Telegram template** (`_build_admin_debug_message`): full diagnostic message sent to admin chat via `QUANTCAIRN_ADMIN_CHAT_ID` env var — reuses existing detailed builder.

### Changed

- **CI Python matrix**: `["3.11", "3.14"]` → `["3.11", "3.12", "3.13"]` — Python 3.14 unavailable in `setup-python@v5`
- **CI job split**: `unit` job (pytest) and `demo` job (pipeline + example) run independently
- **Telegram dispatch**: auto-selects public vs admin template based on `execution_status` and admin chat configuration

### Safety

- All new tests are deterministic — zero network, API key, or broker dependency
- Release workflow does not auto-publish — artifacts only, 7-day retention
- No trading logic, broker, engine, risk, or safety modules modified

### Tests

- 18 new API smoke tests — all pass
- 9 existing Telegram notification tests — all pass
- Demo pipeline + basic API example — both deterministic and verified

---

## [0.12.3] — 2026-07-26

Public Boundary Hardening & Repository Architecture Alignment.

### Added

- **AI Repository Boundary Documentation** (`.ai/REPOSITORY_BOUNDARY.md`): explicit public/private boundary rules for AI coding assistants — public tracked files, private gitignored runtime, forbidden exports, emergency secret-exposure procedure, pre-commit checklist, future Pro architecture planning
- **Decision log entry #18**: Repository Boundary Formalization — rationale, alternatives considered, impact

### Fixed

- **`.gitignore`**: removed `config.yaml` line (file is public-safe defaults and already tracked — redundant entry caused boundary confusion)
- **`health_check.sh`**: replaced `$HOME/soxs-range-arbitrage` fallback with `$SCRIPT_DIR` (removed maintainer's personal home directory path from public repository)

### Changed

- **Historical docs** (`docs/BRAND_MIGRATION.md`, `docs/GITHUB_MIGRATION.md`, `docs/CURRENT_SYSTEM_STATE.md`): added status banners ("Historical Document", "Point-in-Time Snapshot") so readers can distinguish completed migrations from current state
- **Operational scripts** (`monitor.sh`, `scripts/run_top_engine.sh`): added context banners clarifying these are personal maintainer tools, not core library functionality
- **WeChat scripts** (`scripts/wechat_notify.py`, `scripts/wechat_webhook.py`): added "PLATFORM-SPECIFIC: macOS only. Optional helper." banners — clarifies they are not core QuantCairn notification features

### Safety

- Zero production code changes — documentation, shell scripts, and `.gitignore` only
- No broker, engine, risk, portfolio, order, or safety modules modified
- Full test suite: 12 pre-existing failures, zero new failures

---

## [0.12.2] — 2026-07-26

Documentation alignment and repository hygiene release.

### Fixed

- **AI context branding**: `.ai/CLAUDE.md` title and project identity line updated from "OpenAlpha" to "QuantCairn"
- **`.gitignore` hardened**: added `HANDOVER.md`, `artifacts/`, `config/candidate_models/` patterns (prevent accidental commit of runtime artifacts and personal documents)

### Changed

- **Decision log**: entries #16 (v0.12.1) and #17 (v0.12.2) added

### Safety

- Zero code changes, zero test impact
- All changes are documentation and `.gitignore` only

---

## [Unreleased]

- PaperBroker now rolls back in-memory state when portfolio-state persistence fails, so rejected orders no longer leave cash, positions, or trade history partially mutated.
- Multi-provider data adapters (Alpha Vantage, Polygon)
- Backtest validation harness
- Expanded universe profiles
- Docker deployment
