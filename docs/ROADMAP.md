# QuantCairn Roadmap

This roadmap is limited to project management, runtime consistency, and research validation. It does not authorize changes to scoring, ranking, quality thresholds, selection rules, or trading logic.

## P0 — Workspace and Identity

- Freeze secondary development worktrees.
- Confirm `/Users/chenwei/quantcairn` as the sole development context.
- Record file-level ownership for dirty changes.
- Map source commit, release manifest, launchd target, and state directory.

## P1 — Runtime Reconciliation

- Reconcile source with the active release.
- Decide whether release `4826...` or release `25c...` is the valid operational lineage.
- Align combined, selector, research, candidate-validation, TOP, and orphan-monitor launchd targets.
- Resolve the TOP supervisor failure and verify readiness evidence.

## P2 — PAPER End-to-End Gate

- Run the complete PAPER selector with an identified release.
- Verify selector funnel, quality diagnostics, bundle, report, dashboard snapshot, and ledger identity.
- Verify provider timeout and partial-artifact behavior.
- Record runtime evidence separately from unit-test evidence.

## P3 — Research History Decision

- Decide whether `selection_history` is formal product capability, historical implementation, or experiment.
- If formal, define its canonical source and artifact schema.
- If historical, preserve it as an audit reference without treating it as active runtime behavior.

## P4 — Research Outcome Governance

- Validate outcome collection, regime/shadow analysis, and weight-advisor boundaries.
- Establish approval gates for any future model or weighting change.

## LIVE Gate

LIVE is a separate future decision. PAPER success, data readiness, or preflight success must never enable LIVE implicitly.
