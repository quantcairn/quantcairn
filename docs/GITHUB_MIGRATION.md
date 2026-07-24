# QuantCairn GitHub Migration Plan

> **Status**: Planning  
> **Last updated**: 2026-07-25  
> **Context**: Prepares the repository for migration from a private developer identity to the public QuantCairn GitHub organization.  

---

## 1. Current Repository State

| Attribute | Value |
|---|---|
| **Remote** | `git@github.com:cdskjh/-.git` |
| **Current org** | `cdskjh` (private, single-user) |
| **Repository name** | `-` (placeholder name) |
| **Active branch** | `codex/paper-broker-hardening` |
| **Default branch** | `main` |
| **Python package namespace** | `src/openalpha/` (23 modules) |
| **Python package name** | Not yet published — no `pyproject.toml` exists |
| **Test framework** | pytest, ~1075 tests, 59+ core integration tests |

### Existing Tags

```
v0.10.9-ai-context          AI engineering context layer
v0.10.10-beta               Pipeline integrity hardening
v0.10.11-beta               OpenAlpha baseline
v0.11.0-open-source-ready   Open source documentation foundation
research-platform-stable-20260714
safety-baseline-20260719
```

### Current Branches

```
codex/paper-broker-hardening  (active)
main                          (default)
session-audit                 (archived)
```

---

## 2. Target GitHub Identity

| Attribute | Current | Target |
|---|---|---|
| **Organization** | `cdskjh` | `quantcairn` |
| **Repository** | `-` | `quantcairn` |
| **Full path** | `github.com/cdskjh/-` | `github.com/quantcairn/quantcairn` |
| **Clone URL (SSH)** | `git@github.com:cdskjh/-.git` | `git@github.com:quantcairn/quantcairn.git` |
| **Clone URL (HTTPS)** | — | `https://github.com/quantcairn/quantcairn.git` |
| **Visibility** | Private | Public |

---

## 3. Migration Steps

### Phase 1 — GitHub Organization Preparation

| # | Task | Detail | Status |
|---|---|---|---|
| 1 | Create `quantcairn` GitHub organization | Free plan. Configure org name, display name, email | ⬜ Pending |
| 2 | Set organization avatar/logo | QuantCairn brand identity | ⬜ Pending |
| 3 | Configure org security | 2FA requirement, member roles, base permissions | ⬜ Pending |
| 4 | Create org-level README | `.github` repo with public profile README | ⬜ Pending |
| 5 | Invite collaborators | Add team members with appropriate roles | ⬜ Pending |

### Phase 2 — Repository Migration

| # | Task | Detail | Impact |
|---|---|---|---|
| 1 | **⚠️ Requires confirmation**: Choose migration method | **Option A**: GitHub repo transfer (preserves stars, issues, wiki). **Option B**: Create new repo, push with full history | ⬜ Pending |
| 2 | Update local remote | `git remote set-url origin git@github.com:quantcairn/quantcairn.git` | Low |
| 3 | Push all branches | `git push --all origin` | Low |
| 4 | Push all tags | `git push --tags origin` | Low |
| 5 | Set default branch | Set `main` as default in GitHub settings | Low |
| 6 | Verify clone works | Test: `git clone git@github.com:quantcairn/quantcairn.git` in a temp directory | Low |
| 7 | Verify all tags present | All 6 tags accessible after clone | Low |
| 8 | Verify branch history intact | All commits, authors, dates preserved | Low |

**Note**: GitHub automatically redirects from old repository URLs to the new location. Clones, pulls, and pushes to the old URL will work (with a warning to update).

### Phase 3 — Documentation Update

| # | Task | File(s) | Status |
|---|---|---|---|
| 1 | Update README badges | License badge, stars badge → `quantcairn/quantcairn` | ⬜ Pending |
| 2 | Update clone command in README | `README.md` quick start section | ✅ Done (Phase 1.4) |
| 3 | Update CONTRIBUTING.md links | GitHub Discussions, issue template paths | ⬜ Pending |
| 4 | Update ROADMAP.md references | Repository links | ⬜ Pending |
| 5 | Update `.ai/CLAUDE.md` | Clone URL, repository identity | ⬜ Pending |
| 6 | Create `.github/` directory | `CODEOWNERS`, `FUNDING.yml`, issue templates | ⬜ Pending |
| 7 | Update `docs/BRAND_MIGRATION.md` | Phase 2 completion status | ⬜ Pending |
| 8 | Update `docs/GITHUB_MIGRATION.md` | This file — mark Phases 1–3 complete | ⬜ Pending |

### Phase 4 — Package Migration Preparation

| # | Task | Detail | Status |
|---|---|---|---|
| 1 | Evaluate `src/openalpha/` → `src/quantcairn/` rename scope | 23 Python files + 40+ test files + scripts | ⬜ Pending |
| 2 | Create `pyproject.toml` | Package name: `quantcairn`, version: `0.11.0` | ⬜ Pending |
| 3 | Plan legacy compatibility layer | `src/openalpha/__init__.py` with re-exports + deprecation warnings | ⬜ Pending |
| 4 | Estimate test impact | ~1075 tests, import path updates only — no logic changes | ⬜ Pending |
| 5 | Schedule rename | Should be a dedicated commit with no other changes | ⬜ Pending |

---

## 4. Risks

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| **Clone URLs break** | Low | Low | GitHub auto-redirects old URLs. Document the new URL. |
| **Tags lost during transfer** | High | Low | Push tags explicitly after transfer. Verify with `git tag -l`. |
| **Branch history corrupted** | Medium | Very Low | Transfer preserves all commits. Verify with `git log` after transfer. |
| **README badges show broken links** | Low | High | Update all badge URLs in Phase 3 — quick fix. |
| **CI/CD breaks** | Low | Medium | No CI/CD exists yet — created in Phase 3, Step 6. Minimal risk. |
| **Package namespace confusion** | Medium | Medium | `src/openalpha/` is an internal name — no external consumers. Confusion is internal only. |
| **Old git remotes on dev machines** | Low | High | Each developer runs `git remote set-url`. GitHub redirect works as fallback. |
| **Release tag naming confusion** | Low | Low | Existing tags (`v0.10.x`, `v0.11.0`) carry forward. Add `v0.11.x` with `quantcairn` package name. |
| **Organization name squatting** | Medium | Unknown | Create org before announcing. Verify `quantcairn` is available now. |

---

## 5. Verification Checklist

### Pre-Migration

- [ ] `quantcairn` GitHub organization created and configured
- [ ] All collaborators have GitHub accounts and 2FA enabled
- [ ] Repository description and topics prepared
- [ ] `README.md` branding migrated (Phase 1.4)
- [ ] `docs/BRAND_MIGRATION.md` reviewed and approved
- [ ] `docs/GITHUB_MIGRATION.md` reviewed and approved

### Post-Migration

- [ ] Repository accessible at `https://github.com/quantcairn/quantcairn`
- [ ] Clone works: `git clone git@github.com:quantcairn/quantcairn.git`
- [ ] All 6 tags preserved
- [ ] All branches preserved
- [ ] Commit history intact (verify with `git log --oneline | tail`)
- [ ] `main` is the default branch
- [ ] README renders correctly on GitHub
- [ ] Badges show correct counts (stars, license)
- [ ] Old URL redirects to new URL
- [ ] Local remote updated on the primary development machine

### Post-Documentation

- [ ] `README.md` badges updated
- [ ] `CONTRIBUTING.md` links updated
- [ ] `.ai/CLAUDE.md` repository references updated
- [ ] `.github/` directory created with templates
- [ ] `docs/BRAND_MIGRATION.md` Phase 2 marked complete
- [ ] This file marked with completion dates

---

## 6. Post-Migration Git Commands

Run on the development machine after GitHub migration:

```bash
# Update local remote
git remote set-url origin git@github.com:quantcairn/quantcairn.git

# Verify
git remote -v
# Expected: origin  git@github.com:quantcairn/quantcairn.git (fetch)
# Expected: origin  git@github.com:quantcairn/quantcairn.git (push)

# Push all branches (if not already transferred)
git push --all origin

# Push all tags
git push --tags origin

# Verify tags survived
git ls-remote --tags origin

# Clone test (in temp directory)
cd /tmp
git clone git@github.com:quantcairn/quantcairn.git
cd quantcairn
git tag -l
git log --oneline -5
rm -rf /tmp/quantcairn
```

---

## 7. Launchd / Cron Path Updates

After repository rename, macOS scheduling paths must be updated:

```bash
# Launchd plist paths to update
~/Library/LaunchAgents/com.soxs.ai_selector.plist
#  → ProgramArguments: path/to/quantcairn/scripts/ai_selector_wrapper.py

# Crontab entries to update (if applicable)
crontab -l | grep "soxs-range-arbitrage"
#  → update paths to new directory name
```

**Note**: The plist file names (`com.soxs.*`) and the wrapper script names do not need to change — only the directory paths within them.

---

## 8. Current Decision

**GitHub migration should execute only after:**

1. Public documentation is complete and reviewed (Phase 1.1–1.4)
2. `README.md` renders a complete project landing page
3. `docs/BRAND_MIGRATION.md` is approved
4. Demo mode or a getting-started guide exists so new visitors have a working example
5. Package migration plan (Phase 3 of Brand Migration) is reviewed

**Decision date**: ⚠️ *Pending confirmation* — documented here for review.

---

## 9. Non-Changes (Explicitly Out of Scope)

The following are **not** part of this migration:

- Python package rename (`src/openalpha/` → `src/quantcairn/`) — separate effort, see `docs/BRAND_MIGRATION.md` Phase 3
- Source code changes — not in any phase of this plan
- Import path changes — not in any phase of this plan
- Config file changes — not in any phase of this plan
- Trading logic changes — never part of any migration
- Broker / Engine / RiskManager changes — never

---

*This plan is a living document. Update it as phases are completed. Migration execution requires explicit approval before each phase begins.*
