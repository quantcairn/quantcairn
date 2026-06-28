# AI Stock Selection

Daily premarket scanner for the `soxs-range-arbitrage` workflow.

## What it does

- Screens U.S. stocks by price, liquidity, market cap, and 30-day volatility.
- Scores news sentiment from filings, news, Reddit, and social sources.
- Scores technical setup using SMA20/50/200, RSI, MACD, ATR, support, and resistance.
- Produces a weighted top-10 ranking.
- Writes `configs/TOP1.yaml`, `configs/TOP2.yaml`, and `configs/TOP3.yaml`.
- Writes a daily markdown and JSON report in `outputs/`.

## Run

```bash
cd /Users/chenwei/Documents/Codex/2026-06-21/all-10-checks-passed-no-errors/work/longbridge_patch
.venv/bin/python scripts/run_ai_stock_selection.py
```

Set candidates explicitly if you do not want to scan the full market:

```bash
export LONGBRIDGE_TOP_SYMBOLS="AAPL.US,MSFT.US,NVDA.US"
.venv/bin/python scripts/run_ai_stock_selection.py --symbols AAPL.US,MSFT.US,NVDA.US
```

## Schedule

Use `launchd` or `cron` to run the script before the U.S. market opens.

The repo includes a launchd plist at `launchd/com.soxs.ai_selector.plist` and a wrapper at `scripts/run_ai_stock_selection.sh`.

Install it with:

```bash
launchctl bootstrap gui/$(id -u) /Users/chenwei/Documents/Codex/2026-06-21/all-10-checks-passed-no-errors/work/longbridge_patch/launchd/com.soxs.ai_selector.plist
launchctl enable gui/$(id -u)/com.soxs.ai_selector
```

The job runs at 20:55 local machine time. Adjust the hour/minute in the plist if your Mac is not on Asia/Shanghai time.

Example command to schedule:

```bash
cd /Users/chenwei/Documents/Codex/2026-06-21/all-10-checks-passed-no-errors/work/longbridge_patch && .venv/bin/python scripts/run_ai_stock_selection.py
```

## Output schema

`configs/TOP*.yaml` includes:

- `ticker`
- `status` (`区间触发中` or `未触发`)
- `range.low`
- `range.high`
- `range.state`
- `range.triggered`
- `risk.*`
- indicator and scoring metadata
