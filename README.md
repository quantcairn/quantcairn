# OpenAlpha

**AI-driven US stock selection pipeline for range-bound swing trading.**

OpenAlpha runs a 9-stage analytical pipeline that screens a managed universe of 35 US equities and ETFs, scores them with a multi-factor model, applies mode-aware quality filtering, and produces daily candidate selections. Designed for research, paper trading, and transparency — not autonomous execution.

---

## Architecture

```
Market Data (Yahoo / LongBridge)
    │
    ▼
Preflight ──→ Run Mode (FULL / AFTER_MARKET / EOD_ONLY / DEGRADED)
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  9-Stage Selection Pipeline                         │
│                                                      │
│  UNIVERSE ──→ UNIVERSE_FILTER ──→ MARKET_DATA        │
│      35 symbols      cap at 50       OHLCV check      │
│                                                      │
│  SCORING_ELIGIBLE ──→ BASE_RANKING ──→               │
│      multi-factor         polish                      │
│                                                      │
│  FORMAL_ELIGIBILITY ──→ DATA_QUALITY ──→             │
│      formal gate       mode-aware checks              │
│                                                      │
│  COMPOSITION_FILTER ──→ FORMAL_TOP                   │
│      diversification       final candidates           │
└──────────────────────┬──────────────────────────────┘
                       │
              ┌────────▼────────┐
              │  FunnelTracker   │  ← audit & diagnostics
              │  Notifier        │  ← Telegram / webhook
              │  Config Writer   │  ← TOP{1,2,3}.yaml
              └──────────────────┘
```

## Pipeline Stages

| Stage | Input | Output | Description |
|---|---|---|---|
| **UNIVERSE** | — | 35 symbols | Load enabled symbols from managed universe |
| **UNIVERSE_FILTER** | 35 | ≤50 | Cap at configurable max |
| **MARKET_DATA** | ≤50 | data-available | Validate OHLCV independently of scoring |
| **SCORING_ELIGIBLE** | data-available | scored pool | Multi-factor scoring with fallback profiles |
| **BASE_RANKING** | scored pool | scored pool | Score polishing via `score_candidate()` |
| **FORMAL_ELIGIBILITY** | scored pool | formal pool | Filter by `formal_scoring_eligibility` |
| **DATA_QUALITY** | formal pool | quality-passed | Mode-aware spread/volume checks |
| **COMPOSITION_FILTER** | quality-passed | diversified | Sector/correlation diversity selection |
| **FORMAL_TOP** | diversified | TOP K | Final tradable candidates |

**Key invariant**: Every stage enforces `output_count <= input_count`.

## Core Features

- **9-stage selection pipeline** — deterministic, auditable, independently testable
- **Multi-factor scoring** — volatility, volume, trend, repeatability, drawdown
- **Mode-aware quality filtering** — strict during market hours, relaxed otherwise
- **Fallback profiles** — all 35 symbols have recovery paths when Yahoo is unavailable
- **Preflight market check** — detects market state before pipeline execution
- **Market regime detection** — BULL / SIDEWAYS / BEAR / RISK_OFF classification
- **Funnel diagnostics** — per-symbol elimination tracing with exact reasons
- **Paper trading support** — LongBridge sandbox integration
- **Telegram notifications** — auto-chunked reports to `@QuantCairnPicks`
- **Read-only dashboard** — Jinja2 HTML monitoring without trade capability
- **Outcome collection** — Parquet-based trade outcome tracking
- **Learning governance** — human-approval gate for weight proposals

## Safety Architecture

OpenAlpha is designed with defense-in-depth safety:

### Trading Isolation

- **Selector and Trading Engine are completely decoupled.** The selector writes YAML configs; the engine reads them. No runtime coupling.
- **Config Writer refuses to overwrite** live configs when existing positions are present.

### Paper-First

- All trading defaults to paper/sandbox mode.
- Live trading requires three independent gates: `config.local.yaml` approval, `trading_environment_guard.py` validation, and `live_guard.py` pre-flight checks.

### Read-Only Dashboard

- The dashboard reads artifacts and state files only. No POST endpoints, no action buttons, no broker API calls.

### Human-Approval Governance

- All machine learning weight proposals default to `PENDING_HUMAN_APPROVAL`.
- Auto-activation is architecturally impossible: `ACTIVE` state requires explicit `approved_by_human=True`.

### Risk Controls

- `allow_live_order` is forced to `false` regardless of config.
- `reduce_only` is forced to `true` — no new positions can be opened.
- SOXS is permanently `reduce_only`.

See [`.ai/safety.md`](.ai/safety.md) for the complete safety constraint specification.

## Quick Start

### Requirements

- Python 3.14+
- macOS (scheduling via launchd; Linux works for execution)
- LongBridge account (for broker integration; paper mode doesn't require one)

### Setup

```bash
# Clone
git clone https://github.com/example/openalpha.git
cd openalpha

# Create venv and install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Copy and edit config
cp config.sample.yaml config.yaml
# Edit config.yaml with your settings

# Run tests
.venv/bin/python -m pytest tests/ -q
```

### Run AI Selection

```bash
# Full managed universe selection
.venv/bin/python scripts/run_ai_selector.py --universe-source managed

# With Surge proxy (disable curl_cffi TLS impersonation)
YF_DISABLE_CURL_CFFI=1 .venv/bin/python scripts/run_ai_selector.py --universe-source managed

# Market data diagnostics
.venv/bin/python scripts/diag_market_data.py

# Force a selection run regardless of time
FORCE_AI_RUN=1 .venv/bin/python scripts/ai_selector_wrapper.py
```

### Scheduling

```bash
# macOS launchd (auto-runs at 09:00 ET on trading days)
mkdir -p ~/Library/LaunchAgents
cp launchd/com.soxs.ai_selector.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.soxs.ai_selector.plist
```

### Health Check

```bash
bash health_check.sh
bash monitor.sh
```

## Project Structure

```
openalpha/
├── src/
│   ├── openalpha/            # Core: selection pipeline, diagnostics, preflight
│   ├── scoring/              # Multi-factor scoring model
│   ├── universe/             # Symbol universe management (35 symbols)
│   ├── engine/               # Trading engine (read-only runtime)
│   ├── broker/               # LongBridge & paper brokers
│   ├── risk/                 # Risk management
│   ├── safety/               # LiveGuard, environment guard
│   ├── notifier/             # Telegram, webhook, macOS notifications
│   ├── dashboard/            # Read-only combined dashboard (Jinja2 HTML)
│   ├── backtest/             # Backtesting framework
│   ├── outcome/              # Trade outcome collection & governance
│   ├── regime/               # Market regime detection
│   ├── data/                 # PriceFetcher (yfinance wrapper)
│   ├── strategy/             # Strategy definitions
│   ├── shadow/               # Shadow trading observation
│   └── utils/                # Market calendar, helpers
├── scripts/                  # CLI tools, wrappers, diagnostics
├── tests/                    # pytest: 59+ core integration tests, ~1075 total
├── .ai/                      # AI assistant context layer
│   ├── CLAUDE.md             # Primary AI context
│   ├── safety.md             # Immutable safety constraints
│   ├── architecture.md       # Module map, data flow, pipeline details
│   └── DECISION_LOG.md       # 15 engineering decisions with reasons
├── config/                   # Configuration templates
├── configs/                  # Generated TOP{1,2,3}.yaml configs
├── launchd/                  # macOS launchd plist files
└── state/                    # Runtime state directory
```

## Roadmap

### Completed

- [x] 9-stage selection pipeline with invariant enforcement
- [x] Mode-aware quality filtering (FULL / AFTER_MARKET / EOD_ONLY / DEGRADED)
- [x] Preflight market state detection
- [x] Universal fallback profile coverage (35 symbols)
- [x] Pipeline diagnostic reports with per-symbol elimination tracing
- [x] Funnel consistency validation
- [x] Telegram message chunking for long reports
- [x] Paper trading foundation
- [x] AI engineering context layer (`.ai/`)

### Next

- [ ] Demo mode with sample data (no API keys required)
- [ ] Public documentation site
- [ ] Dashboard usability improvements
- [ ] Multi-provider data fallback (Alpha Vantage, Polygon)
- [ ] Backtest validation harness for pipeline changes

## Disclaimer

**This project is for research and educational purposes only.**

OpenAlpha is not financial advice, investment advice, or a trading recommendation. It does not guarantee any trading outcome. The system is designed to run in paper/sandbox mode by default. Live trading requires explicit multi-layer configuration changes that are architecturally prevented from being enabled by accident.

Past performance of the selection pipeline does not guarantee future results. All trading involves risk. Use at your own discretion.

---

*Questions? Found a bug? Open an issue or reach out via Telegram [@QuantCairnPicks](https://t.me/QuantCairnPicks).*
