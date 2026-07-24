# Changelog

All notable changes to QuantCairn.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.12.0-demo] — 2026-07-25

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

## [Unreleased]

- Multi-provider data adapters (Alpha Vantage, Polygon)
- Backtest validation harness
- Expanded universe profiles
- Docker deployment
