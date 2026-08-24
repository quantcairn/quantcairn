# QuantCairn Decision Log

This file records project-management decisions and audit conclusions. It does not replace code review or release evidence.

## 2026-08-23 — Establish Project Management Baseline

- Decision: Treat `/Users/chenwei/quantcairn` as the canonical source candidate.
- Reason: It contains the runtime-hardening integration lineage and is ahead of the paper-broker branch by 12 commits.
- Constraint: The worktree is dirty, so this is not yet a clean release baseline.

## 2026-08-23 — Reject Empty Workspace as Project Root

- Decision: Do not use `/Users/chenwei/Documents/QuantCairn` as the development root.
- Reason: It is an empty Git shell with no project commit or source files.

## 2026-08-23 — Runtime Identity Is Not Reconciled

- Finding: The combined dashboard uses release `4826...`, while selector, research, candidate-validation, and TOP launchd targets use `25c...`.
- Decision: Do not treat any cross-service result as a single-version end-to-end validation until release identity is unified.

## 2026-08-23 — PAPER Remains the Operational Boundary

- Finding: Active combined and PAPER service configurations use PAPER mode and disable LIVE credentials.
- Finding: An unloaded orphan-monitor plist retains a LIVE mode value.
- Decision: Preserve PAPER/RESEARCH-only operation and track the stale LIVE configuration as an unresolved safety finding.

## 2026-08-23 — TOP Runtime Is Degraded

- Finding: `com.quantcairn.top-engines` targets `25c...` and has a recent exit code of 10; state reports `start_failed`.
- Decision: Runtime health is degraded until readiness and ownership evidence is re-established.

## 2026-08-23 — Selection History Requires Product Status Decision

- Finding: Selection ledger code exists in source and releases, while the fuller selection-history implementation is not consistently present in the active release.
- Decision: Classify selection history as unresolved until its canonical source and release status are explicitly decided.

## Pending Decisions

- Which release lineage is approved for operations.
- Whether the secondary paper-broker worktree contributes any changes to the canonical source.
- Whether selection history is formal mainline capability or historical implementation.
- How and when the stale LIVE orphan-monitor configuration is removed or formally retired.

## 2026-08-25 — Formalize Workspace Role Boundaries

- Decision: Establish `/Users/chenwei/quantcairn-persistent` as the permanent
  `GOVERNANCE_WORKTREE` for project state, decisions, audits, and release/runtime
  evidence.
- Decision: Use `/Users/chenwei/quantcairn` as the primary
  `DEVELOPMENT_WORKTREE` for feature work, tests, focused commits, and
  integration preparation.
- Decision: Treat immutable releases, runtime roots, launchd, selector/research
  jobs, TOP supervisor, dashboard, and PAPER broker execution as the separate
  `PRODUCTION_OPERATIONS` role.
- Constraint: Governance keeps `DEVELOPMENT_ALLOWED=NO`; no Codex task may
  cross roles without explicit `TARGET_ROLE` and target scope.
- Workflow: Analysis/governance → development → validation → commit →
  governance evidence sync → explicit production authorization → deployment /
  runtime mutation → post-deploy verification → governance final sync.
- Safety: A passing test never authorizes production mutation, and governance
  or development worktrees must not directly deploy or restart runtime.
