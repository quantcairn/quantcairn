# AI Engineering Standard

> Long-term engineering standard for GPT, Codex, and Claude Code in QuantCairn.
>
> Use this as the stable workflow guide for AI-assisted analysis, implementation, validation, and release work.
>
> For a compact collaboration summary, see [`.ai/AI_COLLABORATION.md`](AI_COLLABORATION.md).
> For routing and precedence, see [`.ai/README.md`](README.md).
> For immutable execution redlines, see [`.ai/safety.md`](safety.md).

## 1. Purpose

QuantCairn is a safety-sensitive research system. AI assistance must improve speed and consistency without weakening correctness, explainability, or control.

This standard exists to:

- unify AI collaboration across tools
- keep implementation changes small and reviewable
- make work traceable from analysis to commit
- prevent prompt drift in long-running tasks

## 2. AI Roles

### GPT

Primary responsibilities:

- architecture analysis
- problem decomposition
- risk assessment
- solution design
- acceptance review

GPT should frame the problem, define the risk, and produce an implementation plan before code changes are made.

### Codex

Primary responsibilities:

- read code
- modify files
- write tests
- execute tests
- perform git operations

Codex should make minimal changes, report the scope clearly, and validate the result before any commit.

### Claude Code

Primary responsibilities:

- deep code understanding
- broad repository reading
- architectural consistency checks
- complex refactoring suggestions

Claude Code is best used when the task needs wider codebase context or deeper coupling analysis.

## 3. Engineering Principles

- **Research First** — read code, docs, and artifacts before editing
- **Paper First** — validate any trading-adjacent behavior in paper or read-only mode first
- **Safety First** — never weaken broker, engine, risk, order, or live gating protections
- **Auditability** — prefer traceable artifacts, logs, and tests over implicit behavior
- **Explainability** — every important result should be diagnosable
- **Metadata First** — add read-only metadata before changing decision inputs
- **Minimal Changes** — use the smallest patch that solves the problem

## 4. Standard Workflow

```text
Context
↓
Goal
↓
Scope
↓
Risk Assessment
↓
Implementation Plan
↓
Execution
↓
Validation
↓
Commit
↓
Acceptance
```

### Context

Gather the relevant repository state, docs, logs, and artifacts.

### Goal

Define the exact outcome in one or two sentences.

### Scope

List the files, modules, and behaviors that may change.

### Risk Assessment

Identify what could break, what is safety-sensitive, and what must not change.

### Implementation Plan

Write the smallest viable plan before editing anything.

### Execution

Apply the approved changes.

### Validation

Run the tests and operational checks that prove the intended behavior.

### Commit

Create one commit for one coherent change. Keep the diff focused.

### Acceptance

Confirm the result against the original goal, safety constraints, and git status.

## 5. Codex Prompt Standard

Every Codex prompt should include:

- Task
- Context
- Goal
- Scope
- Risk Assessment
- Constraints
- Steps
- Validation
- Output Requirements
- Completion Criteria

This keeps prompts specific, bounded, and evidence-oriented.

## 6. Audit Workflow

Use this workflow for:

- bug audit
- performance audit
- architecture audit
- production audit

```text
Analysis
↓
Evidence
↓
Root Cause
↓
Risk
↓
Recommendation
```

Rules:

- do not conclude without evidence
- distinguish symptom from root cause
- separate direct cause from contributing factors
- state uncertainty clearly when evidence is incomplete

## 7. Implementation Workflow

Code changes should follow:

```text
Plan
↓
Minimal Changes
↓
Tests
↓
Validation
↓
Git Status
↓
Commit
```

Guidelines:

- prefer the smallest patch that solves the problem
- do not expand scope unless required to preserve correctness
- run tests that directly cover the changed behavior
- verify git status before commit

## 8. Progress Reporting Standard

Long-running work should report progress at milestones.

### Start

Report:

- current goal
- total plan
- current phase

### During execution

Report:

- completed items
- current progress
- remaining work
- newly discovered risk

### On scope growth or safety concerns

Pause and report when:

- the scope expands beyond the approved boundary
- a safety-sensitive file appears
- a new architectural decision is needed

### Finish

Report:

- completion summary
- test results
- remaining risk

## 9. Validation Standard

Every non-trivial change should confirm:

- unit tests
- integration tests
- system consistency

At minimum, preserve these invariants unless the task explicitly changes them:

- `selected_symbols`
- `FORMAL_TOP`
- `FINAL_SELECTED`
- `score`
- `rank`
- `trade_admission`

If the task changes read-only metadata only, verify that execution behavior is unchanged.

## 10. Git Workflow

- one functional change per commit
- use Conventional Commits
- keep commits small and reviewable
- use annotated tags for releases
- create GitHub Releases from tags only after verification
- never push unless explicitly instructed

## 11. Protected Modules

The authoritative protected-module list lives in [`.ai/safety.md`](safety.md).

Never modify protected modules unless the user explicitly authorizes the exact scope.

Also treat these as protected decision surfaces unless a task explicitly includes them:

- scoring
- ranking
- trade_filter
- PAPER/LIVE gate

## 12. Documentation Ownership

Use the right document for the right job:

- [`README.md`](README.md) — document routing and precedence
- [`CLAUDE.md`](CLAUDE.md) — compact repo orientation and module map
- [`safety.md`](safety.md) — immutable safety rules
- [`architecture.md`](architecture.md) — system structure and data flow
- [`DECISION_LOG.md`](DECISION_LOG.md) — approved decisions and rationale
- [`AI_COLLABORATION.md`](AI_COLLABORATION.md) — concise collaboration summary
- [`AI_ENGINEERING_STANDARD.md`](AI_ENGINEERING_STANDARD.md) — long-form collaboration workflow

Keep this document process-oriented. Do not duplicate the full contents of the other AI docs.
