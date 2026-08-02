# QuantCairn AI Documentation Hub

This directory is the entry point for AI-assisted work in QuantCairn.

Use it as a router, not as a second copy of the policy docs.

## Purpose

The goal of this folder is to make AI-assisted work predictable:

- find the right document quickly
- avoid repeating the same rule in many places
- make conflicts obvious instead of silent
- keep long-term guidance separate from task-specific contracts

## Recommended Reading Order

For a normal task, read in this order:

1. [`CLAUDE.md`](./CLAUDE.md)
2. [`safety.md`](./safety.md)
3. [`architecture.md`](./architecture.md)
4. [`DECISION_LOG.md`](./DECISION_LOG.md)
5. [`AI_COLLABORATION.md`](./AI_COLLABORATION.md) for long-term collaboration rules
6. [`AI_ENGINEERING_STANDARD.md`](./AI_ENGINEERING_STANDARD.md) for the longer operational handbook
7. Task-specific contract docs, when relevant

## Authority Precedence

If documents disagree, use this precedence:

1. [`safety.md`](./safety.md)
2. [`REPOSITORY_BOUNDARY.md`](./REPOSITORY_BOUNDARY.md)
3. Task-specific contract docs such as [`CANDIDATE_LIFECYCLE.md`](./CANDIDATE_LIFECYCLE.md) and [`EARNINGS_RISK.md`](./EARNINGS_RISK.md)
4. [`architecture.md`](./architecture.md)
5. [`DECISION_LOG.md`](./DECISION_LOG.md), for still-valid approved decisions
6. [`AI_ENGINEERING_STANDARD.md`](./AI_ENGINEERING_STANDARD.md)
7. [`CLAUDE.md`](./CLAUDE.md)
8. This file, [`README.md`](./README.md)

When a lower-priority doc conflicts with a higher-priority one:

- do not silently merge the rules
- report the conflict explicitly
- follow the higher-priority document
- update the lower-priority document only if it is the right canonical home

## Document Responsibilities

| Document | Primary responsibility | Keep / avoid |
|---|---|---|
| `README.md` | Entry point, reading order, authority precedence, update routing | Keep short; do not restate all rules |
| `CLAUDE.md` | Compact repo orientation, module map, common commands | Keep as a quick reference |
| `safety.md` | Immutable trading and execution redlines | Keep authoritative and specific |
| `architecture.md` | System topology, dependencies, data flow | Keep authoritative for structure |
| `DECISION_LOG.md` | Approved decisions and why they were made | Keep as the decision record |
| `AI_COLLABORATION.md` | Long-term AI collaboration rules and task cadence | Keep concise and canonical for collaboration |
| `AI_ENGINEERING_STANDARD.md` | Long-term AI collaboration standard | Keep process-oriented |
| `REPOSITORY_BOUNDARY.md` | Tracked vs gitignored boundary and private runtime rules | Keep boundary-focused |
| `CANDIDATE_LIFECYCLE.md` | Candidate lifecycle contract | Keep contract-focused |
| `EARNINGS_RISK.md` | Earnings event risk contract | Keep contract-focused |

## Task Reading Matrix

| Task type | Minimum docs to read |
|---|---|
| Docs-only change | `README.md`, plus the target doc |
| Code change in general modules | `README.md`, `CLAUDE.md`, `architecture.md`, `DECISION_LOG.md` as needed |
| Safety-sensitive change | `README.md`, `safety.md`, plus any directly related architecture or decision docs |
| Pipeline / selector change | `README.md`, `CLAUDE.md`, `architecture.md`, `DECISION_LOG.md` |
| Candidate contract change | `README.md`, the relevant contract doc, `selection_bundle`-related docs |
| Earnings-awareness change | `README.md`, `EARNINGS_RISK.md`, and any directly related runtime docs |
| Repository boundary / secrets change | `README.md`, `REPOSITORY_BOUNDARY.md`, `safety.md` |
| Release / commit / tag work | `README.md`, `CLAUDE.md`, `AI_ENGINEERING_STANDARD.md`, plus task-specific docs |
| Long-running task coordination | `README.md`, `AI_COLLABORATION.md`, and task-specific docs |

## Update Routing

When a topic changes, update the right home instead of duplicating it elsewhere:

- routing, precedence, and doc map -> `README.md`
- immutable safety rules -> `safety.md`
- module boundaries, data flow, pipeline structure -> `architecture.md`
- approved decisions and rationale -> `DECISION_LOG.md`
- collaboration rules and long-task reporting -> `AI_COLLABORATION.md`
- collaboration workflow, prompt structure, commit hygiene -> `AI_ENGINEERING_STANDARD.md`
- repo/public-private boundary -> `REPOSITORY_BOUNDARY.md`
- candidate lifecycle contract -> `CANDIDATE_LIFECYCLE.md`
- earnings contract -> `EARNINGS_RISK.md`

## Conflict Handling Principle

If two documents seem to say different things:

1. find the higher-priority doc
2. follow it
3. do not infer a compromise unless the docs explicitly allow it
4. write the discrepancy down in the task report
5. fix the lower-priority doc later if needed

## Minimum Pre-Read Requirements

Before any AI-assisted change:

- read `README.md`
- read the repo-specific docs that match the task
- read `safety.md` whenever a change could touch execution, broker, risk, order, or live capability
- read `architecture.md` whenever a change affects data flow or module dependencies
- read `DECISION_LOG.md` whenever a change introduces a new design choice

## No Duplicate-Definition Rule

Each rule should have one canonical home.

Other docs may summarize it briefly, but they should point back to the canonical source instead of redefining the rule in full.

This is especially important for:

- safety rules
- workflow rules
- boundary rules
- contract schemas

## Maintenance Checklist

Update this hub when:

- a new AI doc is added
- the reading order changes
- authority between docs changes
- a doc starts repeating a rule that should live elsewhere
- a task-specific contract becomes stable enough to deserve its own file

Keep this file short, navigational, and low-risk.
