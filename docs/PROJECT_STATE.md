# QuantCairn Project State

> Baseline created from the read-only Project Management Audit on 2026-08-23.

## Canonical Source Candidate

- Path: `/Users/chenwei/quantcairn`
- Branch: `codex/runtime-hardening-integration`
- HEAD: `b1d1427`
- Working tree: dirty; source changes are not yet ownership-resolved
- Project entry `/Users/chenwei/Documents/QuantCairn` is an empty Git shell and is not the source repository

The canonical source is a candidate, not a release-approved immutable baseline. Do not infer release readiness from this file.

## Runtime Baseline

- Runtime root: `/Users/chenwei/quantcairn-runtime`
- Active combined release: `4826b29f1be1a43fec86375d062686d4a2b64f11`
- Selector/research/TOP launchd target: `25c3248fffd5e8f8d724a9fd6925e6d96bc0fad9`
- Execution mode observed: `PAPER`
- Combined dashboard: running on port `8090`
- Selector and research launchd jobs: not loaded at audit time
- TOP supervisor: degraded; recent exit code `10`

Source, release, launchd, and state identity are not currently aligned.

## Current Phase

`Phase 4C-3R Runtime Hardening Integration`

The Research/PAPER platform is substantially implemented. The immediate work is source/release/launchd reconciliation and runtime validation, not strategy redesign.

## Safety Baseline

- No evidence that the active combined service is executing LIVE trades.
- PAPER and LIVE credential-disable settings are present on active PAPER services.
- A stale, unloaded orphan-monitor plist still contains `QUANTCAIRN_EXECUTION_MODE=LIVE`.
- Execution authorization and kill-switch controls exist in the source tree, but the source worktree is dirty and release equivalence is not proven.

## Blockers

1. Canonical source is not clean or release-approved.
2. Active services point to multiple releases.
3. TOP supervisor is not healthy.
4. Secondary worktrees and temporary phase worktrees remain.
5. Selection history implementation is not confirmed as part of the active canonical release.

## Operating Principle

Research First. Paper First. Safety First.
