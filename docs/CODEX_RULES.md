# QuantCairn Codex Rules

## Default Scope

1. Default to read-only audit unless the user explicitly authorizes a change.
2. Require an explicit `TARGET_ROLE` and `TARGET_WORKTREE` for every non-trivial task. The permanent governance worktree is `/Users/chenwei/quantcairn-persistent`; the development worktree is `/Users/chenwei/quantcairn`.
3. Preserve dirty files and unrelated work; never reset, checkout, delete, or overwrite them implicitly.
4. Before runtime work, inspect the actual process, release, launchd target, state path, and execution mode.

## Workspace Role Contract

| Role | Workspace / scope | Default authority |
|---|---|---|
| `GOVERNANCE_WORKTREE` | `/Users/chenwei/quantcairn-persistent` | Governance, evidence, decisions, and audits; no development or runtime mutation |
| `DEVELOPMENT_WORKTREE` | `/Users/chenwei/quantcairn` | Code, tests, focused commits, and integration preparation; no automatic production mutation |
| `PRODUCTION_OPERATIONS` | Immutable release and runtime environment | Explicitly authorized launchd, runtime, PAPER, or deployment operations only |

The governance worktree must retain:

```text
DEVELOPMENT_ALLOWED=NO
```

Codex must not infer a role from a directory, branch, or successful test run.
Missing role identity requires read-only discovery and a stop before any
cross-role action.

## Strategy and Safety Boundaries

5. Do not change scoring, ranking, quality thresholds, selection rules, or trading logic without explicit authorization.
6. PAPER and RESEARCH are the default operating modes.
7. Never infer LIVE permission from data quality, preflight success, or a complete selector run.
8. LIVE mutation requires explicit `LIVE_EXECUTION`, independent arming, and an OPEN kill switch.
9. Treat any stale LIVE configuration as a safety finding, even when its service is not loaded.

## Identity and Artifacts

10. Source, release, launchd, runtime state, bundle, report, and ledger identities must be checked together.
11. Do not claim a runtime fix from source-only evidence.
12. Distinguish unit-test, static, local runtime, launchd, and authenticated/provider evidence.

## Secrets and Operations

13. Never print credentials, tokens, private keys, full environment dumps, or secret-bearing URLs.
14. Do not modify launchd, service state, database, runtime state, or production deployment without explicit scope.
15. Do not commit, push, merge, reset, checkout, or release unless explicitly requested.
16. Do not cross from governance or development into production operations without an explicit `TARGET_ROLE=PRODUCTION_OPERATIONS` task.

## Reporting

17. Report the exact path, branch, commit, release, mode, and blocker status.
18. Mark unknown, stale, inferred, and unverified facts explicitly.
19. For degraded runs, preserve and report partial artifacts and failure metadata.
