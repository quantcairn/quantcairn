## Summary

- Keep the live LongBridge path on the legacy API-key flow.
- Keep dry-run by default and record every broker request/response to `logs/trades-YYYYMMDD.jsonl`.
- Add debug scripts for broker-only and AI-selector-to-broker validation.
- Add unit coverage for dry-run audit logging, API-key broker wiring, and technical signal scoring.

## Test Steps

```bash
.venv/bin/python run_tests.py
PYTHONPYCACHEPREFIX=/tmp/soxs-pycache .venv/bin/python -m py_compile \
  src/broker/longbridge.py src/strategy/technical.py scripts/debug_longbridge.py scripts/debug_ai_selector.py tests/test_longbridge.py tests/test_technical.py
.venv/bin/python scripts/debug_longbridge.py
.venv/bin/python scripts/debug_ai_selector.py --symbols AAPL.US,MSFT.US,NVDA.US --skip-order
```

Technical selector knobs:

- `LONGBRIDGE_TOP_SYMBOLS="AAPL.US,MSFT.US,NVDA.US"`
- `LONGBRIDGE_MARKET_PROXY="SPY.US"`

Sandbox smoke test, after API-key credentials are available:

```bash
export LONGBRIDGE_AUTH_MODE="apikey"
export LONGBRIDGE_API_KEY="..."
export LONGBRIDGE_API_SECRET="..."
export LONGBRIDGE_ACCESS_TOKEN="..."
export LONGBRIDGE_BASE_URL="https://<official-sandbox-base-url>"
export DRY_RUN=false
.venv/bin/python scripts/debug_longbridge.py --symbol AAPL --qty 1 --live
```

## Sandbox Credentials

1. Log in to the LongBridge/OpenAPI developer console.
2. Request or enable sandbox trading permission.
3. Create a sandbox API key/secret pair.
4. Copy the official sandbox REST base URL from the LongBridge docs or console.
5. Export credentials as environment variables. Do not commit them:

```bash
export LONGBRIDGE_AUTH_MODE="apikey"
export LONGBRIDGE_API_KEY="..."
export LONGBRIDGE_API_SECRET="..."
export LONGBRIDGE_ACCESS_TOKEN="..."
export LONGBRIDGE_BASE_URL="..."
```

## Notes

- `DRY_RUN=true` is the default. Real requests require `DRY_RUN=false`, credentials, and `LONGBRIDGE_BASE_URL`.
- Endpoint paths can be overridden with:
  - `LONGBRIDGE_PLACE_ORDER_PATH`
  - `LONGBRIDGE_CANCEL_ORDER_PATH`
  - `LONGBRIDGE_ORDER_STATUS_PATH`
  - `LONGBRIDGE_POSITIONS_PATH`
- Low-balance protection:
  - `RISK_MIN_OPEN_CAPITAL_USD=1000`
  - `RISK_MIN_OPEN_BUYING_POWER_USD=1000`
  - `RISK_REDUCE_ONLY_BELOW_MIN_CAPITAL=true`
  - When live account buying power is below the floor, the engine blocks new buy/open orders and still allows reduce-only exits.
