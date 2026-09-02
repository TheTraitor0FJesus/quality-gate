---
id: "12"
title: "Migrate ARGUS_WEB to the unified gate"
state: todo
blocked_by:
  - "07"
---

# 12 — Migrate ARGUS_WEB to the unified gate

**What to build:** Move ARGUS_WEB to the unified release, declare its static web assets, and remediate both Python and web findings without weakening the shared contract.

**Blocked by:** 07 — Publish the unified immutable policy release.

**Implementation uncertainty:** Low — the project contains one known Python component and one bounded set of static web assets.

- [ ] The manifest and reusable workflow select the unified release pair.
- [ ] Python checks, dependency hygiene, tests, and repository checks pass.
- [ ] The nine project-owned JavaScript files and the CSS assets are declared within the web component boundary.
- [ ] Biome lint, formatting, and asset-size checks pass for every declared asset.
- [ ] Setup, doctor, full verification, and audit succeed with the released tools.
