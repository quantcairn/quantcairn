# QuantCairn Public Beta Checklist

> **Purpose**: Readiness gate before making the QuantCairn repository public.  
> **Target release**: v0.12.0-demo  
> **Last updated**: 2026-07-25

---

## Repository

- [ ] README.md reviewed — project description, architecture diagram, features, safety architecture, quick start, demo instructions, disclaimer
- [ ] LICENSE confirmed — Apache 2.0, standard text, copyright placeholder
- [ ] CONTRIBUTING.md reviewed — development workflow, safety constraints, PR guidelines
- [ ] ROADMAP.md reviewed — completed items accurate, planned items reasonable
- [ ] Repository description and topics set on GitHub
- [ ] `.github/RELEASE_TEMPLATE.md` in place

---

## Documentation

- [ ] CHANGELOG.md updated — v0.12.0-demo entry complete
- [ ] docs/API.md reviewed — public API surface, stability policy
- [ ] docs/BRAND_MIGRATION.md reviewed — OpenAlpha → QuantCairn transition documented
- [ ] docs/GITHUB_MIGRATION.md reviewed — migration steps, risks, verification checklist
- [ ] docs/RELEASE_CHECKLIST.md reviewed — release process defined
- [ ] docs/PUBLIC_BETA_CHECKLIST.md — this file
- [ ] `.ai/` context layer reviewed — CLAUDE.md, safety.md, architecture.md, DECISION_LOG.md

---

## Code

- [ ] All tests pass: `pytest tests/ -q` — 1075+ passed, 8 known pre-existing failures
- [ ] Demo pipeline runs: `python scripts/run_demo_selector.py` — candidates produced, artifacts written
- [ ] Package builds: `python -m build` — wheel + source distribution created
- [ ] Package imports: `from quantcairn import AIStrategySelector, DemoDataProvider`
- [ ] CI workflow defined: `.github/workflows/test.yml` — install, test, import check, demo
- [ ] `.gitignore` includes `dist/`, `build/`, `*.egg-info/`

---

## Security

- [ ] No API keys, tokens, or passwords in any committed file
- [ ] No broker credentials (`LONGBRIDGE_*`, `app_key`, `app_secret`, `access_token`) in public docs
- [ ] No Telegram bot tokens in committed files
- [ ] `config.local.yaml` in `.gitignore` — never committed
- [ ] `dist/` and `build/` in `.gitignore` — never committed

---

## Safety

- [ ] `allow_live_order` is `false` in all runtime paths
- [ ] `reduce_only` is `true` in all runtime paths
- [ ] Demo mode has no broker connection capability
- [ ] Demo mode has no order execution capability
- [ ] Broker, Engine, RiskManager, PortfolioManager, Order modules not modified in any demo/doc commit
- [ ] Funnel invariant enforced: `output <= input` per stage
- [ ] `.ai/safety.md` reviewed and accurate

---

## Release

- [ ] Version confirmed: `pyproject.toml` = `quantcairn/__init__.py` = `0.12.0`
- [ ] CHANGELOG.md entry accurate for v0.12.0-demo
- [ ] Git tag `v0.12.0-demo` exists and points to correct commit
- [ ] Release notes prepared using `.github/RELEASE_TEMPLATE.md`
- [ ] All documentation files committed and pushed

---

## Public Launch Decision

- [ ] **Ready for public release** — all checklist items confirmed

### Launch Steps

1. Verify all checklist items above
2. Push latest commits and tags to `origin`
3. Change repository visibility on GitHub: Private → Public
4. Create GitHub Release for `v0.12.0-demo`
5. Announce on Telegram `@QuantCairnPicks`

---

## Audit Results (2026-07-25)

| Audit | Status | Notes |
|---|---|---|
| Repository structure | ✅ | All 11 required files present |
| Documentation consistency | ✅ | README, CONTRIBUTING, API.md, CHANGELOG reviewed |
| Security (no secrets) | ✅ | Zero credentials found in public docs |
| Safety invariants | ✅ | `allow_live_order=false`, `reduce_only=true` enforced |
| Package build | ✅ | Wheel + sdist build successfully; imports from clean venv |
| CI workflow | ✅ | Install → test → import → demo pipeline |
| Tests | ✅ | 1075+ passed |

---

*This checklist is a living document. Update as pre-release items are completed.*
