# QuantCairn Python API

> **Stability**: Alpha. Public names in `quantcairn.*` are stable. Internal `src.openalpha.*` paths may change without notice.

## Installation

```bash
# Editable install (recommended for development)
pip install -e .

# or install from github
pip install git+https://github.com/quantcairn/quantcairn.git
```

**Requirements**: Python 3.11+, pandas, numpy, pyyaml.

## Quick Start

```python
from quantcairn import DemoDataProvider

# No API keys or broker connection required
provider = DemoDataProvider()

print(provider.symbols)
# ['AAPL', 'MSFT', 'NVDA', 'SPY', 'TSLA']

df = provider.get_ohlcv("AAPL")
print(df.tail())
#            Open    High     Low   Close    Volume
# Date
# 2026-07-21  208.12  210.34  206.78  209.45  48500000
# 2026-07-22  209.80  212.15  208.90  211.20  51200000
# 2026-07-23  210.50  213.00  209.75  212.80  49800000
```

## Public Components

### Selection Pipeline

#### `AIStrategySelector`
*Module: `quantcairn.selector` (also `from quantcairn import AIStrategySelector`)*

Main pipeline orchestrator. Runs the 9-stage selection pipeline against a configured universe.

```python
from quantcairn.selector import AIStrategySelector

selector = AIStrategySelector()
result = selector.run_selection(write_configs=False)
print(result["top5"])       # TOP K candidates
print(result["run_mode"])   # FULL / EOD_ONLY / DEMO / ...
```

### Pipeline Audit

#### `FunnelTracker`
*Module: `quantcairn.funnel_tracker`*

Tracks pipeline execution: per-stage input/output counts, elimination reasons, timing, and consistency validation. Used by `AIStrategySelector` internally but can be instantiated standalone for testing.

#### `FunnelStageRecord`
Dataclass representing one pipeline stage's metrics.

### Demo Mode

#### `DemoDataProvider`
*Module: `quantcairn.demo_data`*

Deterministic synthetic OHLCV provider. Generates 252 trading days of data for 5 symbols (AAPL, MSFT, NVDA, SPY, TSLA) using a seeded random walk. Results are fully reproducible.

```python
from quantcairn import DemoDataProvider

provider = DemoDataProvider()
df = provider.get_ohlcv("NVDA")       # 252 rows, no network calls
price = provider.price_at("NVDA")     # most recent close
```

#### `get_demo_provider()`
Module-level singleton — returns the same `DemoDataProvider` instance across calls.

#### `DEMO_SYMBOLS` / `DEMO_HISTORY_ROWS`
Constants: `['AAPL', 'MSFT', 'NVDA', 'SPY', 'TSLA']` and `252`.

### Market Preflight

#### `PreflightReport`
*Module: `quantcairn.preflight`*

Dataclass containing market state assessment: `market_state`, `run_mode`, `quote_coverage_pct`, `ohlcv_coverage_pct`, etc.

#### `run_preflight(dry_run=True)`
Runs preflight check and returns a `PreflightReport`. Determines whether the market is open, closed, in pre-market, etc., and recommends a run mode.

### Data Diagnostics

#### `check_data_availability(symbols)`
*Module: `quantcairn.data_diagnostics`*

Pre-scoring data check. For each symbol, tests OHLCV availability via PriceFetcher → Yahoo API → yfinance chain. Returns `(available_symbols, dropped_records)`.

#### `diagnose_market_data_drops(universe_symbols, scored_symbols)`
Per-symbol diagnostic: which symbols failed to reach scoring and why (no data, no fallback profile, fallback rejected by universe filter, etc.).

### Candidate Ranking

#### `score_candidate(candidate)` / `score_candidates(candidates)`
*Module: `quantcairn.candidate_ranking`*

Score polishing: computes liquidity, trend, volatility, risk, and strategy-fit subscores; applies formal/diagnostic score type.

### Universe Filtering

#### `evaluate_universe_candidate(candidate, *, skip_atr_validation=False)`
*Module: `quantcairn.universe_filter`*

Evaluates a single candidate against universe rules (price range, volume, market cap, ATR). Returns a `UniverseEvaluation` with `rejected` flag and `rejection_reason` tuple.

#### `filter_universe_candidates(candidates)` — returns `(accepted, rejected)` lists.

#### `load_universe_rules(path)` — loads rules from YAML config.

#### `infer_asset_type(symbol)` — determines `common_stock`, `etf`, `leveraged_etf`, etc.

#### `UniverseRule` / `UniverseEvaluation` — dataclasses.

### Runtime Settings

#### `load_runtime_settings()` / `get_float_setting(name, default)`
*Module: `quantcairn.settings`*

Reads runtime configuration from environment variables and config files.

## Internal vs Public API

| Namespace | Stability | Use |
|---|---|---|
| `quantcairn.*` | **Stable** | Public API — safe to depend on |
| `src.openalpha.*` | Unstable | Internal implementation — may change in any release |
| `src.scoring.*` | Unstable | Internal |
| `src.engine.*`, `src.broker.*`, `src.risk.*` | Internal | Should not be imported directly |

**Rule**: If you `import quantcairn`, you're on the stable path. If you `from src.openalpha import ...`, you may need to update imports after a minor release.

## Sub-Module Reference

All sub-modules are importable both ways:

```python
# These are equivalent:
from quantcairn.selector import AIStrategySelector
from quantcairn import AIStrategySelector
```

Full sub-module list:

| Module | Purpose |
|---|---|
| `quantcairn.selector` | Selection pipeline |
| `quantcairn.funnel_tracker` | Pipeline audit |
| `quantcairn.demo_data` | Demo data provider |
| `quantcairn.preflight` | Market preflight |
| `quantcairn.data_diagnostics` | Data quality diagnostics |
| `quantcairn.candidate_ranking` | Candidate score polishing |
| `quantcairn.universe_filter` | Universe rule filtering |
| `quantcairn.settings` | Runtime settings |
