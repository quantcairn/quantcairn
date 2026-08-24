# AI Collaboration Standard

> Long-term collaboration standard for AI assistants working in QuantCairn.
>
> Use this document for the stable rules that apply across GPT, Codex, and Claude Code.

## 1. Purpose

QuantCairn is a safety-sensitive research system. AI collaboration must improve speed and consistency without weakening correctness, explainability, or control.

This standard exists to:

- unify collaboration across AI tools
- keep long-term rules in one place
- preserve safety boundaries across research, paper, and live-capable paths
- keep changes small, reviewable, and auditable

## Workspace Roles and Authority

QuantCairn uses separate workspaces for governance, development, and
production operations. An AI assistant must not infer a role from the current
directory or cross these boundaries implicitly.

### GOVERNANCE_WORKTREE

```text
/Users/chenwei/quantcairn-persistent
```

Responsibilities are project state, architecture and decision records,
release/runtime evidence, audits, and risk tracking. The default contract is:

```text
DEVELOPMENT_ALLOWED=NO
```

Do not use this worktree for product code, bug fixes, broker operations,
selector execution, launchd mutation, deployment, runtime restart, mode
changes, or secrets changes unless a later task explicitly changes the role.

### DEVELOPMENT_WORKTREE

```text
/Users/chenwei/quantcairn
```

This worktree is for feature development, bug fixes, runtime hardening, tests,
candidate validation, focused commits, and integration preparation. Passing
tests never authorizes production changes.

### PRODUCTION_RUNTIME

Production operations cover immutable releases, runtime roots, launchd,
selector/research jobs, TOP supervisor, dashboard, and PAPER broker runtime.
They require an explicit task with:

```text
TARGET_ROLE=PRODUCTION_OPERATIONS
```

Governance and development worktrees must not mutate production by default.

### Explicit Prompt Role

Every QuantCairn Codex task must declare one of:

```text
TARGET_ROLE=GOVERNANCE
TARGET_WORKTREE=/Users/chenwei/quantcairn-persistent
```

```text
TARGET_ROLE=DEVELOPMENT
TARGET_WORKTREE=/Users/chenwei/quantcairn
```

```text
TARGET_ROLE=PRODUCTION_OPERATIONS
```

If the role or target is missing, perform read-only identity discovery and do
not cross a workspace boundary.

## 2. Project Principles

### Research First

Understand current behavior before proposing changes. Read code, artifacts, and logs before editing.

### Paper First

Anything that could affect trading must be validated in paper or read-only mode before touching live-capable paths.

### Safety First

Never weaken broker, engine, risk, order, or live gating protections. Safety redlines take priority over speed.

### Metadata Only

Prefer adding read-only metadata over altering decision inputs or execution rules.

### Data Quality First

Do not silently convert missing, invalid, or failed data into success.

### Auditability

Changes should be explainable with artifacts, logs, and tests.

### Minimal Changes

Use the smallest patch that solves the problem. Avoid broad refactors unless required for correctness.

## 3. AI Roles

### GPT

Primary responsibilities:

- architecture analysis
- problem decomposition
- root cause analysis
- risk assessment
- solution design
- acceptance review

GPT should not be used for large direct code rewrites when a smaller plan and Codex patch are enough.

### Codex / Claude Code

Primary responsibilities:

- read code
- modify files
- write tests
- execute tests
- perform git operations

Expected behavior:

- make minimal, targeted changes
- preserve repository invariants
- report the files changed and why
- validate before commit

## 4. Standard Workflow

Any code change should follow:

```text
Problem Analysis
↓
Risk Assessment
↓
Implementation Plan
↓
Confirmation
↓
Execution
↓
Validation
↓
Commit
↓
Acceptance
```

For approved changes, the full cross-role flow is:

```text
Analysis / Governance
        ↓
Development Worktree
        ↓
Tests / Validation
        ↓
Commit
        ↓
Governance Evidence Sync
        ↓
Explicit Production Authorization
        ↓
Deployment / Runtime Mutation
        ↓
Post-deploy Verification
        ↓
Governance Final Sync
```

Rules:

- analyze before changing code
- do not expand scope during implementation unless required for correctness
- run tests that directly cover the changed behavior
- verify git status before commit
- do not push unless explicitly asked

The following flows are prohibited:

```text
Governance Worktree → direct code change → direct deployment
Development Worktree → tests pass → automatic production mutation
```

## 5. Safety Boundary

Unless the task explicitly authorizes the exact scope, do not modify:

```text
src/broker/*
src/engine/*
src/risk/*
src/order/*
src/safety/*
```

Also treat these as protected decision surfaces unless a task explicitly includes them:

- scoring
- ranking
- trade_filter
- PAPER/LIVE gate

## 6. Git and Release Rules

- one feature per commit
- small, focused commits
- use conventional commit messages
- use annotated tags for releases
- create GitHub Releases from tags only after verification
- never push unless explicitly instructed

## 7. Validation Standard

Every non-trivial change should confirm:

- selector behavior
- dashboard rendering
- notifier behavior
- paper broker state
- system health output
- launchd validation

Preserve these invariants unless the task explicitly changes them:

- `selected_symbols`
- `FORMAL_TOP`
- `FINAL_SELECTED`
- `score`
- `rank`
- `trade_admission`

## 8. Long-Running Tasks

For long-running tasks, GPT should provide milestone progress updates and clearly report:

- current phase
- completed items
- remaining work
- risks

## 9. Prompt Continuity

Carry forward:

- completed work
- current problem
- remaining risk
- verified assumptions

Do not restart from zero unless the user explicitly asks for a fresh analysis.

## 10. Relationship to Other AI Docs

This document is the compact collaboration standard.

Use it together with:

- [`README.md`](README.md) for document routing and precedence
- [`CLAUDE.md`](CLAUDE.md) for compact repo orientation
- [`AI_ENGINEERING_STANDARD.md`](AI_ENGINEERING_STANDARD.md) for the longer operational handbook
- [`safety.md`](safety.md) for immutable execution redlines
- [`architecture.md`](architecture.md) for module structure and data flow
- [`DECISION_LOG.md`](DECISION_LOG.md) for approved design decisions
