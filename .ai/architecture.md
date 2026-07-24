# Architecture Reference

> Deep-dive module map, data flow, and dependency graph. Read after `CLAUDE.md`.

## Data Flow

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Preflight   │ →  │  9-Stage     │ →  │  Config      │
│  (market     │    │  Pipeline    │    │  Writer      │
│   state)     │    │  (selector)  │    │  (TOP YAMLs) │
└──────────────┘    └──────┬───────┘    └──────┬───────┘
                           │                   │
                    ┌──────▼───────┐    ┌──────▼───────┐
                    │  Funnel      │    │  Trading     │
                    │  Tracker     │    │  Engine      │
                    │  (audit)     │    │  (consumes)  │
                    └──────┬───────┘    └──────────────┘
                           │
                    ┌──────▼───────┐
                    │  Notifier    │
                    │  (Telegram,  │
                    │   webhook)   │
                    └──────────────┘
```

## Module Dependency Graph

```
selector.py
  ├── preflight.py          (market state → run_mode)
  ├── universe/manager.py   (load enabled symbols)
  ├── data_diagnostics.py   (check_data_availability)
  ├── scoring/scorer.py     (multi-factor scoring)
  ├── candidate_ranking.py  (score_candidate polish)
  ├── universe_filter.py    (evaluate_universe_candidate)
  ├── funnel_tracker.py     (pipeline audit trail)
  ├── config_writer.py      (write TOP configs)
  └── notifier/alerts.py    (send selection results)

scorer.py
  ├── data/fetcher.py       (PriceFetcher → yfinance)
  ├── universe_filter.py    (candidate validation)
  ├── candidate_ranking.py  (score polish)
  └── FALLBACK_PROFILES     (35 symbol hard-coded profiles)

funnel_tracker.py
  ├── FunnelStageRecord     (per-stage timing/counts)
  ├── validate()            (invariant checks)
  ├── print_diagnostic_report()  (detailed per-symbol trace)
  └── write_debug_artifact()     (JSON artifact)

notifier/alerts.py
  ├── Notifier class        (console, macOS, webhook, Telegram)
  ├── _telegram_send_chunked()  (long message splitting)
  ├── _build_ai_selection_message()  (Chinese-formatted report)
  └── notification_ledger   (dedup tracking)
```

## Pipeline Stage Details

### Stage 1: UNIVERSE
- **Input**: 35 managed symbols from `UniverseManager.load_snapshot()`
- **Fallback**: If managed universe unavailable → `_load_local_snapshot()` (legacy sample)
- **Categories**: Index ETFs (4), Mega Caps (7), Semiconductors (3), Sector ETFs (8), Leveraged/Inverse (12), Additional Stocks (12)

### Stage 2: UNIVERSE_FILTER
- Cap at `max_symbols` (50, via `OPENALPHA_MAX_SYMBOLS`)

### Stage 3: MARKET_DATA
- **Independent of scoring** — validates OHLCV availability only
- Uses `check_data_availability()` from `data_diagnostics.py`
- Three data sources tried in order: PriceFetcher → Yahoo chart API → yfinance
- Safe bottom: if all symbols fail, pool is NOT emptied (scoring can still use fallback)

### Stage 4: SCORING_ELIGIBLE
- `Scorer.score_universe()` → ThreadPoolExecutor (8 workers)
- Each symbol: `_load_history()` → if insufficient → `_fallback_profile_for_symbol()` → `_fallback_scored_item()`
- `score_candidate()` polishes with liquidity/trend/volatility/risk/strategy subscores

### Stage 5: BASE_RANKING
- Pass-through: records scoring output for pipeline consistency

### Stage 6: FORMAL_ELIGIBILITY
- Filters `formal_scoring_eligibility=True`

### Stage 7: DATA_QUALITY (mode-aware)
- **FULL mode**: Strict — spread, bid/ask, volatility all required
- **Non-FULL**: Relaxed — only volume ≥500K and price presence enforced
- Fallback hints (`avg_daily_volume_hint`, `price_midpoint_hint`) used in non-strict modes
- Budget-constrained: max 8 seconds (`OPENALPHA_QUALITY_BUDGET_SECONDS`)
- Timeout → `fast_preliminary` mode

### Stage 8: COMPOSITION_FILTER
- Greedy diversity selection: `_select_diversified_top_k()`
- Correlation penalties: ≥0.90→60, ≥0.80→40, ≥0.65→20, ≥0.50→10
- Sector bonus: +8 points for different sector

### Stage 9: FORMAL_TOP
- Full mode, quality passed: `topk ⊆ quality_passed` → formal candidates
- Full mode, all rejected: `quality_fallback_active=True` → preview only
- Non-FULL mode: `topk` from relaxed quality → formal candidates (`RESEARCH_ONLY`)

## Scoring Model Reference

### Subscore Formulas

**Volatility Score** (`_volatility_score`):
```python
combined = (atr_pct + return_vol_pct) / 2.0
score = 100.0 - abs(combined - 3.5) * 14.0  # ideal around 3.5%
score += min(10.0, range_width_pct / 6.0)     # range bonus
```

**Volume Score** (`_volume_score`):
```python
base = log10(avg_volume_20 / 1M + 1) * 35.0
activity = min(30.0, volume_spike * 10.0)
persistence = min(25.0, log10(avg_volume_60 / 1M + 1) * 10.0)
score = 20.0 + base + activity + persistence
```

**Rejection conditions** (any one → candidate dropped):
| Condition | Threshold |
|---|---|
| Range too wide | `range_width_pct > 45%` |
| Range too tight | `range_width_pct < 4%` |
| ATR too low | `atr_pct < 1%` |
| ATR too high | `atr_pct > 12%` |
| Gap risk | `gap_rate > 20%` |
| Event/news | `news_score ≥ 80` |
| Strong trend | `_strong_trend() = True` |
| Too flat | `_too_flat() = True` |
| Spread too narrow | `range_spread < 3%` |

### Fallback Profile Keys

All 35 universe symbols have `FALLBACK_PROFILES` entries with: `score`, `range_low`, `range_high`, `volume`. Supporting data: `FALLBACK_RANGE_PCT`, `FALLBACK_SECTOR`, `FALLBACK_MARKET_CAP`.

## Universe Filter Rules

| Asset Type | Price Range | Min Daily $ Vol | ATR Range | Min Market Cap |
|---|---|---|---|---|
| common_stock | $5–200 | $50M | 1%–8% | $2B |
| etf | $5–300 | $20M | 0.5%–6% | none |
| leveraged_etf | $5–100 | $10M | 1%–10% | none |
| inverse_etf | $5–100 | $10M | 1%–10% | none |

**Fallback exception**: `skip_atr_validation=True` for fallback-scored candidates (synthetic volatility from band width).

## Output Artifacts

| Artifact | Path | Format |
|---|---|---|
| Funnel Report | `artifacts/selector_funnel/{date}/{run_id}/funnel_report.json` | JSON |
| Debug Artifact | `artifacts/selection/funnel_debug.json` | JSON (latest run) |
| Selection Log | `logs/selection_{date}.log` | JSONL |
| Preflight JSON | `artifacts/selection/preflight.json` | JSON |
| TOP Configs | `configs/TOP{1,2,3}.yaml` | YAML |
| Notification Ledger | `state/notifications/notification_ledger.jsonl` | JSONL |
