# Release Checklist

Use this checklist when preparing a QuantCairn release.  
Check off each item before proceeding to the next stage.

---

## Before Release

### Code Quality

- [ ] Working tree clean — `git status` shows no unintended modifications
- [ ] All tests pass — `pytest tests/ -q` with zero new failures
- [ ] Demo pipeline executes successfully — `python scripts/run_demo_selector.py`
- [ ] No source code in broker, engine, risk, portfolio, order, or safety modules was modified

### Documentation

- [ ] `CHANGELOG.md` updated with this release's changes
- [ ] `docs/API.md` updated if public API surface changed
- [ ] `README.md` updated if any user-facing instructions changed
- [ ] `.ai/DECISION_LOG.md` updated if new architectural decisions were made

### Versioning

- [ ] `pyproject.toml` version matches the planned release version
- [ ] `quantcairn/__init__.py` `__version__` matches `pyproject.toml`

### Safety Verification

- [ ] `allow_live_order` is `false` (never changed)
- [ ] `reduce_only` is `true` (never changed)
- [ ] Demo mode still works without API keys
- [ ] Funnel invariant `output <= input` validated on latest run

---

## Release

### Git Tag

- [ ] Create annotated tag: `git tag -a v<VERSION> -m "<message>"`
- [ ] Verify tag: `git show v<VERSION> --no-patch`
- [ ] Push tag: `git push origin v<VERSION>`

### GitHub Release

- [ ] Go to `https://github.com/quantcairn/quantcairn/releases/new`
- [ ] Choose the tag just pushed
- [ ] Use `.github/RELEASE_TEMPLATE.md` as the description template
- [ ] Fill in summary, new features, changes, safety notes
- [ ] Publish release

---

## After Release

### Verification

- [ ] Fresh clone installs correctly: `pip install -e .`
- [ ] `python -c "import quantcairn; print(quantcairn.__version__)"` returns the new version
- [ ] Demo pipeline runs: `python scripts/run_demo_selector.py`

### Announcement

- [ ] Release notes published on GitHub Releases page
- [ ] Telegram channel `@QuantCairnPicks` notified (if applicable)
- [ ] `CHANGELOG.md` pushed to default branch

### Post-Release Cleanup

- [ ] Merge release branch (if using separate branches)
- [ ] Delete release branch (optional — keep if it serves as a checkpoint)
- [ ] `git status` confirms clean working tree
