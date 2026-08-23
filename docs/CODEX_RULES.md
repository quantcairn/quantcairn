# QuantCairn Codex Rules

## Default Scope

1. Default to read-only audit unless the user explicitly authorizes a change.
2. Use `/Users/chenwei/quantcairn` as the source candidate until the project state is changed explicitly.
3. Preserve dirty files and unrelated work; never reset, checkout, delete, or overwrite them implicitly.
4. Before runtime work, inspect the actual process, release, launchd target, state path, and execution mode.

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

## Reporting

16. Report the exact path, branch, commit, release, mode, and blocker status.
17. Mark unknown, stale, inferred, and unverified facts explicitly.
18. For degraded runs, preserve and report partial artifacts and failure metadata.
