# QuantCairn Runtime Identity

> Read-only baseline from the 2026-08-23 project management audit.

## Identity Chain

```text
canonical source
  -> release manifest
  -> launchd plist
  -> process command / working directory
  -> runtime state and artifacts
```

All links must be checked before a runtime result is considered authoritative.

## Source

```text
path=/Users/chenwei/quantcairn
branch=codex/runtime-hardening-integration
head=b1d1427
python=/Users/chenwei/quantcairn/.venv/bin/python
python_version=3.14.4
working_tree=DIRTY
```

## Releases Observed

| Release | Created | Observed role |
|---|---|---|
| `4826b29f1be1a43fec86375d062686d4a2b64f11` | 2026-08-22 | active combined dashboard |
| `25c3248fffd5e8f8d724a9fd6925e6d96bc0fad9` | 2026-08-19 | selector, research, candidate-validation, and TOP launchd target |

The active release manifest SHA is not the current source HEAD. Release equivalence is therefore `NOT_CONFIRMED`.

## Services Observed

| Service | Observed target | Observed status | Mode |
|---|---|---|---|
| `com.quantcairn.combined` | `4826...` | running; port 8090 | PAPER |
| `com.quantcairn.ai-selector` | `25c...` | not loaded | PAPER |
| `com.quantcairn.research` | `25c...` | not loaded | PAPER |
| `com.quantcairn.candidate-validation` | `25c...` | not loaded | PAPER |
| `com.quantcairn.top-engines` | `25c...` | degraded; recent exit 10 | PAPER |
| `com.quantcairn.orphan-monitor` | source checkout | not loaded | stale LIVE config present |

## State and Logs

Primary observed operational paths:

```text
state=/Users/chenwei/soxs-range-arbitrage/state
logs=/Users/chenwei/soxs-range-arbitrage/logs
artifacts=/Users/chenwei/soxs-range-arbitrage/artifacts
```

The source tree also contains `state/`, `logs/`, and `artifacts/`; these must not be assumed to be the active runtime paths.

## Identity Status

```text
source_release_match=NO
launchd_alignment=NO
execution_mode_active=PAPER
top_runtime=DEGRADED
live_config_residual=YES
```
