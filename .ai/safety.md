# Safety Constraints — Immutable Rules

> **Purpose**: This file exists so any AI assistant immediately understands what must never change, regardless of how innocuous a modification might seem.

## Core Redlines

### 1. Order Execution Gate (`allow_live_order`)

```
config.local.yaml → allow_live_order: false (NEVER true)
```

- **Location**: `src/safety/trading_environment_guard.py:42`
- **Effect**: Even if `config.local.yaml` says `true`, the guard sets it to `false` before any engine starts
- **Consequence of violation**: Real money orders could be sent to LongBridge

### 2. Reduce-Only Mode (`reduce_only`)

```
engine._reduce_only = True (NEVER false)
```

- **Location**: `src/safety/live_guard.py:12`
- **Effect**: Trading engine can only close/reduce existing positions, never open new ones
- **Consequence of violation**: New positions could be opened without human approval

### 3. Never-Modify Modules

Do not modify these files or directories under any circumstance:

| Path | Reason |
|---|---|
| `src/broker/*` | Broker API — connects to LongBridge. Changes could send real orders |
| `src/engine/trading_engine.py` | Core trading loop. Changes could bypass safety gates |
| `src/risk/manager.py` | Risk management. Changes could disable position limits |
| `src/portfolio/*` | Portfolio state. Changes could corrupt position tracking |
| `src/order/*` | Order creation. Changes could create unexpected orders |
| `src/safety/live_guard.py` | Pre-flight checks. The last line of defense |
| `src/safety/trading_environment_guard.py` | Environment validation. Sets `allow_live_order=false` |

### 4. Gate Protections

| Gate | Path | Rule |
|---|---|---|
| **Paper Gate** | `src/broker/paper_broker.py` | Never modify. Paper broker simulates trades but the gate logic must remain intact |
| **Live Gate** | `src/broker/longbridge_broker.py` | Never modify. Live broker has multi-layer confirmation before any order |

### 5. Config Writer

- **Location**: `src/openalpha/config_writer.py`
- **Behavior**: Writes TOP1/TOP2/TOP3.yaml configs consumed by the trading engine
- **Safety flag**: `Skipping TOP{N} disabled write: existing live config preserved` — this message means the writer detected existing live configs and refused to overwrite them
- **Never**: Remove this protection or force-overwrite live configs

## Human-Approval Gate for Learning

- **Location**: `src/outcome/governance.py`
- **Rule**: All weight proposals default to `PENDING_HUMAN_APPROVAL`
- **Transition to ACTIVE**: Requires `approved_by_human=True` with non-empty reason
- **State machine**: `DRAFT → BACKTESTED → WALK_FORWARD_VALIDATED → REVIEW_REQUIRED → APPROVED → ACTIVE`
- **Never**: Auto-approve proposals or bypass the human gate

## Quality Fallback Semantics

- **Preview Candidates**: Research-only, visible in dashboard, NEVER tradable
- **Formal Candidates**: Passed all gates, CAN be tradable (subject to mode)
- **FULL mode + all rejected**: `quality_fallback_active=True`, Formal=empty
- **Non-FULL mode + all rejected**: Relaxed path produces Formal candidates (RESEARCH_ONLY type)

## If an Assistant Violates Any Rule

The assistant must:
1. Immediately state the violation
2. Revert the change
3. Warn the user that safety was compromised

## Testing Safety Changes

Any change to safety modules or the never-modify list requires:
1. Full test suite pass (`pytest tests/ -q`)
2. Explicit human approval before commit
3. No new test failures introduced
