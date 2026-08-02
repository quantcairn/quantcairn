# AI Engineering Standard

> Long-term operating standard for GPT, Codex, Claude Code, and future AI assistants working in QuantCairn.
>
> Use this as the default handbook for AI-assisted analysis, implementation, validation, and release work.
>
> For a compact collaboration summary, see [`.ai/AI_COLLABORATION.md`](AI_COLLABORATION.md).
>
> For document routing and precedence, see [`.ai/README.md`](README.md).
> For immutable trading and execution redlines, see [`.ai/safety.md`](safety.md).

## 1. Purpose

QuantCairn is a safety-sensitive trading research system. AI assistance must improve speed and consistency without weakening correctness, explainability, or control.

This standard exists to:

- unify collaboration across AI tools
- preserve safety boundaries across research, paper, and live-capable paths
- keep implementation changes small and reviewable
- make every decision traceable from analysis to commit
- prevent prompt drift in long-running tasks

This is a standing repository standard, not a task-specific note.

## 2. AI Roles

### GPT

Primary responsibilities:

- architecture analysis
- root cause analysis
- risk assessment
- solution design
- prompt generation
- acceptance review

Expected behavior:

- analyze before changing code
- define scope before implementation
- state risks explicitly
- provide implementation steps and validation expectations
- review outcomes against evidence

Not responsible for:

- large direct code rewrites
- unreviewed refactors across many modules
- bypassing safety boundaries for convenience

### Codex

Primary responsibilities:

- read code
- modify code
- write tests
- execute tests
- perform git operations

Expected behavior:

- make minimal, targeted changes
- preserve repository invariants
- report the files changed and why
- run relevant tests before asking for commit approval
- keep the working tree clean except for intended changes

### Claude Code

Primary responsibilities:

- deep code understanding
- broad repository reading
- complex refactoring suggestions
- architectural consistency checks

Expected behavior:

- find hidden coupling
- map data flow and dependency flow
- identify consistency gaps
- support design review and audit work

## 3. Engineering Principles

### Research First

Understand current behavior before proposing changes. Prefer reading code, artifacts, and logs before editing.

### Paper First

Any behavior that could affect trading must be validated in paper or read-only mode before touching live-capable paths.

### Safety First

Never weaken broker, engine, risk, order, or live gating protections. Safety redlines take priority over speed.

### Auditability

Changes must be explainable. Prefer traceable artifacts, logs, and tests over implicit behavior.

### Explainability

Every significant pipeline result should be diagnosable. If a state or candidate disappears, the system should show where and why.

### Metadata First

When extending the system, prefer adding read-only metadata over altering decision inputs or execution rules.

### Minimal Changes

Use the smallest change set that solves the problem. Avoid broad refactors unless the architecture requires them.

## 4. Standard Workflow

The standard lifecycle for AI-assisted work is:

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

Gather the relevant repository state, docs, and existing behavior.

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

## 5. Prompt Standard

When GPT prepares a task for Codex, the prompt should usually include:

- Task
- Context
- Goal
- Scope
- Risk Assessment
- Constraints
- Investigation Steps or Implementation Steps
- Validation
- Output Requirements
- Completion Criteria

Each part serves a purpose:

- **Task**: what is being asked
- **Context**: what is already known
- **Goal**: the desired result
- **Scope**: what may change
- **Risk Assessment**: what could go wrong
- **Constraints**: what must never change
- **Investigation / Implementation Steps**: how to proceed
- **Validation**: how to prove correctness
- **Output Requirements**: what should be reported back
- **Completion Criteria**: when the task is considered done

Prompts should be specific, bounded, and evidence-oriented.

## 6. Audit Workflow

Use this workflow for:

- bug audit
- performance audit
- architecture audit
- production audit

Standard flow:

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
- do not expand scope during implementation unless required to preserve correctness
- run tests that directly cover the changed behavior
- verify git status before commit

## 8. Validation Standard

Every non-trivial change should confirm:

- unit tests
- integration tests
- system consistency

At minimum, validate that the following remain stable unless the task explicitly changes them:

- `selected_symbols`
- `FORMAL_TOP`
- `FINAL_SELECTED`
- `score`
- `rank`
- `trade_admission`

If the task changes read-only metadata only, verify that execution behavior is unchanged.

## 9. Git Workflow

Repository changes should follow these conventions:

- one functional change per commit
- use Conventional Commits for commit messages
- keep commits small and reviewable
- never push unless explicitly instructed

When a release is needed:

- use an annotated tag
- create a GitHub Release from that tag only after verification

## 10. Release Standard

Before any release, confirm:

### Code

- relevant tests pass
- working tree is clean

### System

- selector behavior is valid
- dashboard reads the intended artifacts
- notifier behavior is correct
- paper broker state is consistent
- scheduler behavior is understood
- system health is readable

### Safety

Confirm no unexpected change in:

- broker
- engine
- risk
- order
- `PAPER/LIVE` gates

## 11. Prompt Continuity Rules

Multi-step AI work should carry forward:

- completed work
- current problem
- remaining risk
- verified assumptions

Do not restart from zero unless the user explicitly asks for a fresh analysis.

Do not repeat the entire repository context if the relevant state is already established.

## 12. Protected Modules

The authoritative protected-module list lives in [`.ai/safety.md`](safety.md).

Never modify protected modules unless the user explicitly authorizes the exact scope.

Also treat these as protected decision surfaces unless a task explicitly includes them:

- scoring
- ranking
- trade_filter

## 13. Recommended AI Collaboration Pattern

A safe default workflow for QuantCairn is:

1. GPT frames the problem and the risk
2. Codex reads the relevant code and implements the minimal patch
3. Codex runs the targeted tests and reports results
4. GPT reviews the evidence and decides whether the change is ready
5. Only then does the workflow move to commit, tag, or release

This pattern keeps architectural reasoning, implementation, and validation separate enough to be reliable, but tight enough to move quickly.
