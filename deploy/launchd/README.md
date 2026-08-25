# QuantCairn — macOS launchd Deployment

Templates for running QuantCairn services under macOS `launchd` (LaunchAgent).

## Services

| Label | Script | Purpose | Frequency |
|-------|--------|---------|-----------|
| `com.quantcairn.combined` | `scripts/start_combined.py` | Unified research dashboard (port 8090) | Long-lived (KeepAlive) |
| `com.quantcairn.ai-selector` | `scripts/ai_selector_wrapper.py` | AI selection pipeline scheduler | 21:35 / 21:45 / 22:00 / 22:15 / 22:30 Beijing time |
| `com.quantcairn.candidate-validation` | `scripts/run_candidate_validation_scheduler.py --apply` | Candidate validation scheduler (`com.quantcairn.candidate-validation.plist.template`) | 21:40 / 21:50 / 22:05 / 22:20 / 22:35 Beijing time |
| `com.quantcairn.research` | `scripts/run_daily_research.py --mode independent` | Committed-bundle Research scheduler | 22:50 Beijing time |
| `com.quantcairn.daily-runtime-snapshot` | `scripts/generate_daily_runtime_snapshot.py` | Read-only daily runtime evidence snapshot | 23:30 local machine time |
| `com.quantcairn.top-engines` | `scripts/start_top_engines.sh` | TOP paper trading engines (ports 8080/8081/8082) | Long-lived (KeepAlive) |
| `com.quantcairn.orphan-monitor` | `scripts/start_orphan_monitor.py` | Disabled PAPER-safe orphan monitor template | Long-lived (KeepAlive) |

## Quick Start

### 1. Prerequisites

- macOS with Python 3.12+ and a venv at `<PROJECT_ROOT>/.venv`
- Project cloned and `pip install -e ".[demo,test]"` completed
- Production TOP runtime: install `quantcairn[paper-runtime]` into a
  release-associated venv; do not use an editable development install.

### 2. Install Templates

Replace the placeholder paths in each `.plist.template` file, then install:

```bash
# Set your paths
PROJECT_ROOT="/path/to/quantcairn"
PYTHON_PATH="$PROJECT_ROOT/.venv/bin/python"
STATE_ROOT="/Users/chenwei/soxs-range-arbitrage/state"
REPORTS_ROOT="/Users/chenwei/soxs-range-arbitrage/reports"
ARTIFACTS_ROOT="/Users/chenwei/soxs-range-arbitrage/artifacts"
LOGS_ROOT="/Users/chenwei/soxs-range-arbitrage/logs"
CONFIG_ROOT="/Users/chenwei/soxs-range-arbitrage/state"
TOP_CONFIG_ROOT="/Users/chenwei/soxs-range-arbitrage/state/top_configs_paper"
RELEASE_SHA="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"

# Copy and substitute
for tmpl in deploy/launchd/*.plist.template; do
    name=$(basename "$tmpl" .template)
    sed -e "s|REPLACE_WITH_PYTHON_PATH|$PYTHON_PATH|g" \
        -e "s|REPLACE_WITH_PROJECT_ROOT|$PROJECT_ROOT|g" \
        -e "s|REPLACE_WITH_STATE_ROOT|$STATE_ROOT|g" \
        -e "s|REPLACE_WITH_REPORTS_ROOT|$REPORTS_ROOT|g" \
        -e "s|REPLACE_WITH_ARTIFACTS_ROOT|$ARTIFACTS_ROOT|g" \
        -e "s|REPLACE_WITH_LOGS_ROOT|$LOGS_ROOT|g" \
        -e "s|REPLACE_WITH_CONFIG_ROOT|$CONFIG_ROOT|g" \
        -e "s|REPLACE_WITH_TOP_CONFIG_ROOT|$TOP_CONFIG_ROOT|g" \
        -e "s|REPLACE_WITH_RELEASE_SHA|$RELEASE_SHA|g" \
        "$tmpl" > ~/Library/LaunchAgents/"$name"
done
```

Or edit each file manually — every `REPLACE_WITH_*` token must be replaced.

### 3. Load Jobs

```bash
# Combined dashboard (always needed)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantcairn.combined.plist

# AI selector (recommended for automated daily selection)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantcairn.ai-selector.plist

# Candidate validation scheduler (safe early validation progression)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantcairn.candidate-validation.plist

# Independent Research consumes the committed Selection Bundle.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantcairn.research.plist

# Orphan monitor (only if using LongBridge live/sandbox)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantcairn.orphan-monitor.plist
```

### 4. Verify

```bash
# Check jobs are loaded and running
launchctl list | grep quantcairn

# Check dashboard
curl -s --max-time 5 http://127.0.0.1:8090/api/status | python3 -m json.tool

# Run health script
bash health_check.sh

# The health report also shows orphan monitor install/load/run status
# and the freshness of its log files.

# Validate the exact interpreter selected for an immutable TOP release.
SOXS_RELEASE_SHA="$RELEASE_SHA" \
SOXS_PROJECT_DIR="$PROJECT_ROOT" \
  "$PYTHON_PATH" "$PROJECT_ROOT/scripts/validate_top_runtime.py" --json
```

### 5. View Logs

```bash
tail -f logs/combined.log           # Dashboard stdout
tail -f logs/combined.err.log       # Dashboard stderr
tail -f logs/ai_selector.out.log    # AI selector stdout
tail -f logs/ai_selector.err.log    # AI selector stderr (scheduler decisions)
tail -f logs/candidate-validation.out.log  # Candidate validation stdout
tail -f logs/candidate-validation.err.log   # Candidate validation stderr / audit
tail -f logs/top-engines.out.log             # TOP engines launcher stdout
tail -f logs/top-engines.err.log              # TOP engines launcher stderr
```

## Migration from Legacy com.soxs Labels

If you previously used `private_ops/launchd/` or `com.soxs.*` labels:

### 1. Identify Old Jobs

```bash
launchctl list | grep -E 'soxs|openalpha'
```

Typical legacy labels: `com.soxs.combined`, `com.soxs.ai_selector`, `com.soxs.arbitrage`

### 2. Unload Old Jobs

```bash
UID=$(id -u)
for label in com.soxs.combined com.soxs.ai_selector com.soxs.arbitrage com.soxs.arbitrage.stop; do
    if launchctl print gui/$UID/$label >/dev/null 2>&1; then
        launchctl bootout gui/$UID/$label
        echo "Unloaded: $label"
    fi
done
```

### 3. Remove Old Plist Files

```bash
rm -f ~/Library/LaunchAgents/com.soxs.*.plist
```

Also remove `private_ops/launchd/` if it still exists (it should not — it was deleted from the repository).

### 4. Install New Templates

Follow the Quick Start steps above. The new labels use `com.quantcairn.*` to match the project rename.

## Placeholder Reference

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `REPLACE_WITH_PYTHON_PATH` | Absolute path to Python interpreter | `/opt/homebrew/bin/python3` or `<PROJECT_ROOT>/.venv/bin/python` |
| `REPLACE_WITH_PROJECT_ROOT` | Absolute path to the repository root | `/Users/alice/quantcairn` |
| `REPLACE_WITH_STATE_ROOT` | Persistent authoritative state root | `/Users/chenwei/soxs-range-arbitrage/state` |
| `REPLACE_WITH_REPORTS_ROOT` | Persistent reports root | `/Users/chenwei/soxs-range-arbitrage/reports` |
| `REPLACE_WITH_ARTIFACTS_ROOT` | Persistent artifacts root | `/Users/chenwei/soxs-range-arbitrage/artifacts` |
| `REPLACE_WITH_LOGS_ROOT` | Persistent logs root | `/Users/chenwei/soxs-range-arbitrage/logs` |
| `REPLACE_WITH_CONFIG_ROOT` | External configuration/state root | `/Users/chenwei/soxs-range-arbitrage/state` |
| `REPLACE_WITH_TOP_CONFIG_ROOT` | External PAPER TOP config root | `/Users/chenwei/soxs-range-arbitrage/state/top_configs_paper` |
| `REPLACE_WITH_RELEASE_SHA` | Immutable release Git SHA | `402af842...` |

### Common Python Paths

```bash
# Homebrew Python (macOS Apple Silicon)
/opt/homebrew/bin/python3

# Project venv (recommended)
<PROJECT_ROOT>/.venv/bin/python

# System Python (not recommended — may lack dependencies)
/usr/bin/python3
```

## Environment Variables

Set in the plist `EnvironmentVariables` dict:

| Variable | Purpose | Default |
|----------|---------|---------|
| `QUANTCAIRN_EXECUTION_MODE` | `PAPER` / `RESEARCH` / `LIVE` | `PAPER` |
| `QUANTCAIRN_HOME` | Absolute project root used by launchd/wrapper | `<PROJECT_ROOT>` |
| `YF_DISABLE_CURL_CFFI` | Use `requests` instead of `curl_cffi` (proxy compat) | `1` |
| `SOXS_OPENALPHA_TELEGRAM_BOT_TOKEN` | Telegram bot token loaded from the local secrets file | (none) |
| `SOXS_OPENALPHA_TELEGRAM_CHAT_ID` | Telegram chat/channel ID loaded from the local secrets file | (none) |
| `QUANTCAIRN_ADMIN_CHAT_ID` | Optional admin chat ID loaded from the local secrets file | (none) |
| `SOXS_PROJECT_DIR` | Code/source root | `<PROJECT_ROOT>` |
| `SOXS_RELEASE_SHA` | Immutable release Git SHA | `<RELEASE_SHA>` |
| `SOXS_PYTHON_BIN` | Explicit approved Python runtime for TOP | `<PYTHON_PATH>` |
| `SOXS_CONFIG_DIR` | External configuration/state root | `<CONFIG_ROOT>` |
| `SOXS_TOP_CONFIG_DIR` | External PAPER TOP config root | `<TOP_CONFIG_ROOT>` |
| `SOXS_LOG_DIR` | Explicit operational log root | `<LOGS_ROOT>` |
| `SOXS_DISABLE_LIVE_CREDENTIALS` | Prevent live credential use in PAPER TOP | `1` |
| `PYTHONDONTWRITEBYTECODE` | Prevent runtime bytecode in immutable release | `1` |
| `SOXS_STATE_DIR` | Override state directory | `<STATE_ROOT>` |
| `SOXS_REPORTS_DIR` | Override reports directory | `<REPORTS_ROOT>` |
| `SOXS_ARTIFACTS_DIR` | Override artifacts directory | `<ARTIFACTS_ROOT>` |
| `SOXS_LOGS_DIR` | Override logs directory | `<LOGS_ROOT>` |
| `SOXS_DISABLE_ORPHAN_MONITOR` | Keep the default PAPER orphan template disabled | `1` |
| `OPENALPHA_WRAPPER_VERBOSE` | Enable scheduler decision logging | `1` |

## Troubleshooting

### Job not starting

```bash
# Check for syntax errors in plist
plutil -lint ~/Library/LaunchAgents/com.quantcairn.combined.plist

# Check launchd error output
tail -20 logs/combined.err.log
```

### Port 8090 already in use

```bash
lsof -i :8090 -P | grep LISTEN
# Kill the stale process if needed, then reload
launchctl bootout gui/$(id -u)/com.quantcairn.combined
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantcairn.combined.plist
```

### AI selector not running on trading days

```bash
# Check scheduler decisions
grep '\[SCHEDULER\]' logs/ai_selector.err.log | tail -10

# Force a manual run
FORCE_AI_RUN=1 .venv/bin/python scripts/ai_selector_wrapper.py
```

### Check all jobs at once

```bash
python3 scripts/system_health.py
```

## File Layout

```
deploy/
└── launchd/
    ├── README.md                                    # This file
    ├── com.quantcairn.combined.plist.template        # Combined dashboard
    ├── com.quantcairn.ai-selector.plist.template     # AI selector scheduler
    ├── com.quantcairn.candidate-validation.plist.template  # Candidate validation scheduler
    ├── com.quantcairn.research.plist.template      # Independent Research scheduler
    ├── com.quantcairn.top-engines.plist.template    # TOP paper trading engines
    ├── com.quantcairn.orphan-monitor.plist.template  # Orphan position monitor
    └── com.quantcairn.daily-runtime-snapshot.plist.template  # Daily runtime evidence
```
