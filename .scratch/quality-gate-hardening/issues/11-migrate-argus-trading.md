---
id: "11"
title: "Migrate ARGUS_TRADING to the unified gate"
state: todo
blocked_by:
  - "07"
---

# 11 — Migrate ARGUS_TRADING to the unified gate

**What to build:** Move ARGUS_TRADING to the unified release, distinguish published assets from JavaScript tests, and remediate both Python and web findings without weakening the shared contract.

**Blocked by:** 07 — Publish the unified immutable policy release.

**Implementation uncertainty:** Low — the project boundaries are known and the released component contract determines the required behavior.

- [ ] The manifest and reusable workflow select the unified release pair.
- [ ] Python checks, dependency hygiene, tests, and repository checks pass.
- [ ] Published JavaScript and CSS are declared as web assets while JavaScript test fixtures remain outside the production asset budget.
- [ ] Existing JavaScript functional tests are declared as supplemental `node-test` targets and remain enforced by the repository CI workflow.
- [ ] Biome lint, formatting, and asset-size checks pass for the declared web component.
- [ ] Setup, doctor, full verification, and audit succeed with the released tools.
