# QuantCairn Brand Migration Plan

> **Status**: Planning  
> **Last updated**: 2026-07-25  
> **Context**: This document defines the controlled migration from the internal project identity "OpenAlpha" to the public brand "QuantCairn".  

---

## 1. Brand Identity

| Aspect | Current (Internal) | Target (Public) |
|---|---|---|
| **Project name** | OpenAlpha | QuantCairn |
| **Domain** | — | `quantcairn.com` |
| **GitHub org** | `cdskjh` (private) | `quantcairn` |
| **Repository** | `soxs-range-arbitrage` | `quantcairn` |
| **Python package** | Not yet published | `quantcairn` |
| **YouTube** | — | `QuantCairn` |
| **Telegram channel** | `@QuantCairnPicks` | `@QuantCairnPicks` (already branded) |
| **Telegram bot** | `@chenweiderambot` | `@chenweiderambot` (owned, no change needed) |

**Note**: The Telegram channel already uses the QuantCairn name. The codebase uses OpenAlpha internally for AI/assistant context.

---

## 2. Migration Principles

1. **Preserve Git history** — all commits, tags, and authorship carry forward
2. **Avoid breaking existing users** — any current consumer must have a clear upgrade path
3. **Separate branding from refactoring** — name changes only; code structure changes are separate work
4. **Migrate in controlled phases** — documentation first, then repository, then package, then release
5. **Maintain backward compatibility** — legacy import paths with deprecation warnings where possible

---

## 3. Migration Phases

### Phase 1 — Documentation Branding (Current)

**Goal**: All user-facing documentation reflects the QuantCairn brand. Internal `.ai/` context and code remain OpenAlpha.

| File | Action | Status |
|---|---|---|
| `README.md` | Already uses "OpenAlpha" — rebrand to QuantCairn | ⬜ Pending |
| `ROADMAP.md` | Review for brand consistency | ⬜ Pending |
| `CONTRIBUTING.md` | Review for brand consistency | ⬜ Pending |
| `LICENSE` | Update copyright holder name | ⬜ Pending |
| `.ai/CLAUDE.md` | Update project identity section | ⬜ Pending |
| `.ai/architecture.md` | No brand references — no change | ✅ Done |
| `docs/BRAND_MIGRATION.md` | This file | ✅ Done |
| YouTube channel header | Create QuantCairn channel branding | ⬜ Pending |
| Telegram channel `@QuantCairnPicks` | Already branded — no change | ✅ Done |

**Non-changes in Phase 1**:
- Source code (`src/openalpha/`) — stays as-is
- Import paths — stay as-is
- Config files — stay as-is

---

### Phase 2 — Repository Identity

**Goal**: GitHub repository reflects the QuantCairn brand. No code changes.

| Action | Detail | Risk |
|---|---|---|
| Create `quantcairn` GitHub organization | Requires GitHub account | Low |
| Transfer or fork repo to `quantcairn/quantcairn` | Rename from `cdskjh/-` to `quantcairn/quantcairn` | Medium — breaks existing clone URLs |
| Update local remote | `git remote set-url origin git@github.com:quantcairn/quantcairn.git` | Low |
| Update `README.md` badges | CI, license, stars badges point to new org | Low |
| Create GitHub repo description | "AI-driven US stock selection pipeline for range-bound swing trading" | Low |
| Set GitHub repo topics | `quantitative-trading`, `stock-screening`, `range-trading`, `yfinance`, `python` | Low |

**GitHub will redirect** old URLs to new org/repo. Existing clones continue working but should be updated.

---

### Phase 3 — Python Package & Namespace

**Goal**: Source code namespace migrates from `openalpha` to `quantcairn`.

**Current state**: No `pyproject.toml`, `setup.py`, or `setup.cfg` exists. This simplifies Phase 3 significantly — the package build system is created directly under the new name rather than migrated.

| Action | Detail |
|---|---|
| Create `pyproject.toml` | Package name: `quantcairn` |
| Rename `src/openalpha/` → `src/quantcairn/` | All 23 `.py` files + `__init__.py` |
| Update internal imports | `from src.openalpha.` → `from src.quantcairn.` |
| Update test imports | All `from src.openalpha` references in `tests/` |
| Update script imports | `scripts/*.py` references |
| Add legacy compatibility | `src/openalpha/__init__.py` with deprecation warning + re-export |
| Update `.ai/CLAUDE.md` | Module map, pipeline references, commands |
| Full test suite pass | 1075+ tests must pass with zero new failures |

**Affected files (estimate)**:

| Path | Lines | Impact |
|---|---|---|
| `src/openalpha/*.py` (23 files) | ~8,000 | Namespace rename + import updates |
| `tests/` (40+ test files) | ~15,000 | `from src.openalpha` → `from src.quantcairn` |
| `scripts/run_ai_selector.py` | ~2,600 | Import updates |
| `scripts/ai_selector_wrapper.py` | ~130 | Import updates |
| `.ai/CLAUDE.md` | ~200 | Module map, file paths |
| `requirements.txt` | ~13 | Add `quantcairn` if published |

**This phase must not be combined with any logic changes.** It is a pure rename.

---

### Phase 4 — Public Release

**Goal**: Public announcement and community infrastructure.

| Action | Detail |
|---|---|
| GitHub repo made public | `quantcairn/quantcairn` visibility: Public |
| `CHANGELOG.md` created | Migration summary + what's new for the public |
| Release notes | Explain: what QuantCairn is, how to use it, what changed from OpenAlpha |
| Community announcement | Twitter/X, LinkedIn, Telegram channel post |
| Demo GIF / screenshot | Show pipeline output, dashboard, Telegram notification |
| Issue templates | Bug report, Feature request, Question |
| GitHub Discussions enabled | For Q&A and community |
| `README.md` finalized | Polished landing page with demo, badges, quick start |

---

## 4. Files Potentially Affected (Full Inventory)

| File / Path | Purpose | Phase | Migration Impact |
|---|---|---|---|
| `README.md` | Project landing page | Phase 1 | Brand name in title + tagline |
| `ROADMAP.md` | Public roadmap | Phase 1 | "OpenAlpha" → "QuantCairn" references |
| `CONTRIBUTING.md` | Contributor guide | Phase 1 | Project name references |
| `LICENSE` | Apache 2.0 | Phase 1 | Copyright holder name |
| `.ai/CLAUDE.md` | AI context (project identity section) | Phase 1 | Brand name, domain |
| `.ai/architecture.md` | Architecture reference | Phase 1 | No brand references exist — skip |
| `.ai/safety.md` | Safety constraints | Phase 1 | No brand references exist — skip |
| `.ai/DECISION_LOG.md` | Decision log | Phase 1 | "OpenAlpha" in decision #1 preamble |
| `docs/BRAND_MIGRATION.md` | This file | Phase 1 | Already done |
| `.github/` (if exists) | GitHub metadata | Phase 2 | Issue templates, workflow files |
| `src/openalpha/` | Core selector Python package | Phase 3 | Rename to `src/quantcairn/` |
| `pyproject.toml` | Not yet created | Phase 3 | Create with `quantcairn` name |
| `tests/` | Test suite | Phase 3 | Import path updates |
| `scripts/*.py` | CLI scripts | Phase 3 | Import path updates |
| `configs/` | Generated TOP configs | Phase 3 | YAML comment references |
| `launchd/` | macOS launchers | Phase 3 | Script path references |
| `CHANGELOG.md` | Not yet created | Phase 4 | Create for public release |

---

## 5. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Broken imports after rename** | High | All 1075+ tests must pass before merge. Add legacy `src/openalpha/__init__.py` with re-export + `DeprecationWarning`. |
| **GitHub clone URLs break** | Medium | GitHub auto-redirects from old org/repo. Document the new URL. |
| **Telegram channel already branded** | Low | `@QuantCairnPicks` already uses the name — consistent. |
| **Documentation inconsistency** | Low | Audit all `.md` files for "OpenAlpha" after Phase 1. |
| **User confusion during transition** | Medium | Clear changelog, release notes, and deprecation timeline. |
| **launchd / cron paths break** | Medium | Update `com.soxs.ai_selector.plist` and crontab entries after rename. |
| **Config references to "openalpha"** | Medium | Env vars like `OPENALPHA_TOP_K`, `OPENALPHA_MAX_SYMBOLS` need rename plan (or keep as-is — these are internal). |

### Risk Decision: Environment Variables

Environment variables prefixed `OPENALPHA_` (e.g., `OPENALPHA_TOP_K`, `OPENALPHA_UNIVERSE`, `OPENALPHA_LIVE_DATA`) are **kept as-is** in Phase 3 to avoid breaking launchd plists and macOS environment. They can be renamed in a later phase with dual-read support.

---

## 6. Decision: Why QuantCairn

| Factor | OpenAlpha | QuantCairn |
|---|---|---|
| **Meaning** | "Alpha" = trading returns | "Cairn" = marker/guidepost in mountain terrain |
| **Scope implication** | Alpha generation (narrow) | Navigation platform (broad) |
| **Expansion** | Tied to one strategy (range arbitrage) | Room for multiple strategies, asset classes, signals |
| **Uniqueness** | Generic — many projects use "Alpha" | Unique — zero code/search collisions |
| **Domain** | N/A | `quantcairn.com` is available |
| **Name collision** | Likely exists in quantitative finance | Does not exist anywhere |

**Decision date**: ⚠️ *Pending confirmation* — documented here for review.

---

## 7. Migration Checklist

### Phase 1 — Documentation Branding
- [ ] `README.md` title: "OpenAlpha" → "QuantCairn"
- [ ] `README.md` tagline updated
- [ ] `ROADMAP.md` brand references updated
- [ ] `CONTRIBUTING.md` brand references updated
- [ ] `LICENSE` copyright holder confirmed and filled
- [ ] `.ai/CLAUDE.md` project identity section updated
- [ ] `.ai/DECISION_LOG.md` decision #1 preamble updated
- [ ] YouTube channel created (optional)

### Phase 2 — Repository Identity
- [ ] GitHub organization `quantcairn` created
- [ ] Repository transferred/renamed to `quantcairn/quantcairn`
- [ ] Local `git remote` updated
- [ ] GitHub repo description and topics set
- [ ] Badges updated in README.md

### Phase 3 — Python Package Migration
- [ ] `pyproject.toml` created with package name `quantcairn`
- [ ] `src/openalpha/` renamed to `src/quantcairn/`
- [ ] All internal imports updated
- [ ] All test imports updated
- [ ] All script imports updated
- [ ] Legacy `src/openalpha/__init__.py` added (re-export with deprecation warning)
- [ ] Full test suite passes (1075+ tests, zero new failures)
- [ ] `.ai/CLAUDE.md` file paths and commands updated
- [ ] `launchd` plist paths updated
- [ ] `crontab` entries updated (if applicable)

### Phase 4 — Public Release
- [ ] Repository made public
- [ ] `CHANGELOG.md` created
- [ ] Release notes published
- [ ] Community announcement prepared
- [ ] Issue templates created
- [ ] GitHub Discussions enabled

---

## 8. Pre-Migration Verification

Before executing any phase, verify:

1. All tests pass: `pytest tests/ -q` → 1075+ passed, ≤8 known failures
2. Git status clean: no unintended files modified
3. Safety constraints intact: `allow_live_order=false`, `reduce_only=true`
4. No trading logic modified

---

*This plan is a living document. Update it as phases are completed or new risks are identified. Phase execution requires explicit approval before each phase begins.*
