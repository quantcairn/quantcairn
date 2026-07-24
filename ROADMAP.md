# QuantCairn Roadmap

> *Formerly developed under the internal project name OpenAlpha.*

> Last updated: 2026-07-25
>
> This roadmap reflects current project state and intended direction. Items in **Planned** are aspirations, not commitments. Priorities may shift based on community feedback.

---

## Completed

### Pipeline Architecture
- [x] **9-stage selection pipeline** with invariant enforcement (`output <= input` per stage)
- [x] **FunnelTracker** — authoritative pipeline audit trail with per-stage timing, counts, and reason codes
- [x] **Pipeline diagnostic reports** — per-symbol elimination tracing with exact failure reasons
- [x] **Funnel consistency validation** — automated chain-break detection with quality fallback suppression

### Market Data & Quality
- [x] **Preflight market state detection** — FULL / AFTER_MARKET / EOD_ONLY / DEGRADED mode selection
- [x] **Mode-aware quality filtering** — strict checks during market hours, relaxed otherwise
- [x] **MARKET_DATA independence from SCORING** — data availability validated separately from scoring fitness
- [x] **Universal fallback profile coverage** — all 35 universe symbols have recovery paths
- [x] **Fallback ATR suppression** — synthetic volatility from band width no longer triggers false rejections
- [x] **Market data diagnostic tool** (`scripts/diag_market_data.py`) — Python/SSL, proxy, DNS, HTTPS, yfinance checks

### Scoring & Selection
- [x] **Multi-factor scoring model** — 30/20/20/15/10 weighted volatility/volume/trend/repeatability/drawdown
- [x] **Diversification filter** — sector-aware greedy selection with correlation penalties
- [x] **Formal vs Preview candidate distinction** — tradable candidates separated from research-only

### Safety & Governance
- [x] **Trading engine isolation** — selector and engine decoupled via YAML config files
- [x] **Paper-first trading** — multi-layer gates before any live order is possible
- [x] **Human-approval learning governance** — weight proposals require explicit human sign-off
- [x] **Read-only dashboard** — no trade-affecting functionality in the monitoring layer

### Infrastructure
- [x] **Managed universe system** — 35 symbols with 4-level filter pipeline, CLI management
- [x] **Telegram notification system** — auto-chunked messages with deduplication ledger
- [x] **Config layering** — `config.yaml` → `config.local.yaml` → environment variables
- [x] **macOS scheduling** — launchd + crontab for daily automated selection

### Documentation
- [x] **AI engineering context layer** (`.ai/`) — CLAUDE.md, safety.md, architecture.md, DECISION_LOG.md
- [x] **Public README** — professional open-source landing page
- [x] **Contributor guidelines** — safety-first development workflow
- [x] **Apache 2.0 licensing**

### Testing
- [x] 59+ core integration tests covering pipeline, quality, diagnostics, notifications
- [x] ~1075 total tests with 8 known pre-existing failures (env leak, unrelated)

---

## In Progress

- [ ] **Open source preparation** — licensing, governance documents, public roadmap
- [ ] **Demo mode** — end-to-end selection with sample data, no API keys required
- [ ] **Documentation improvement** — public-facing architecture docs, getting started guide

---

## Planned

### Near-Term

- [ ] **Public demo environment** — reproducible selection run with pre-recorded market data
- [ ] **Improved dashboard experience** — better mobile layout, dark mode, filtering by candidate type
- [ ] **Multi-provider data adapters** — Alpha Vantage, Polygon.io as Yahoo Finance alternatives
- [ ] **Backtest validation harness** — automated regression testing of pipeline changes against historical runs
- [ ] **Expanded universe profiles** — support for operator-defined custom symbol profiles

### Medium-Term

- [ ] **Docker-based deployment** — containerized selector + dashboard stack
- [ ] **CI/CD pipeline** — GitHub Actions for linting, testing, and artifact validation
- [ ] **Selection result history browser** — time-series view of past selection output
- [ ] **Performance benchmarking** — standardized latency and throughput metrics for pipeline stages

### Community

- [ ] **GitHub Discussions** — Q&A and feature requests
- [ ] **Issue templates** — structured bug reports and feature proposals
- [ ] **Community symbol profiles** — contributor-submitted fallback profiles for non-US markets

---

## Not Planned

These are explicitly out of scope for the foreseeable future:

- **Fully autonomous live trading** — human-in-the-loop is an architectural requirement
- **Real-time intraday selection** — the pipeline is designed for daily batch execution
- **Multi-asset class support** (crypto, forex, futures) — US equities only
- **Mobile trading app** — Telegram notifications + web dashboard are sufficient
- **Profit guarantees or performance claims** — this is a research system, not a product

---

*This roadmap is a living document. Priorities are set by maintainers and community input. Nothing here constitutes a commitment to deliver.*
