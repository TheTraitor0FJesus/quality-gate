---
id: "09"
title: "Migrate ARGUS_tracker_multiuser to the unified gate"
state: todo
blocked_by:
  - "06"
---

# 09 — Migrate ARGUS_tracker_multiuser to the unified gate

**What to build:** Move ARGUS_tracker_multiuser to the unified release, declare its web assets, and remediate both Python and web findings without weakening the shared contract.

**Blocked by:** 06 — Publish the unified immutable policy release.

**Implementation uncertainty:** Low — the component boundaries and verification seam are known; initial Biome formatting will create a deliberate asset diff.

- [ ] The manifest and reusable workflow select the unified release pair.
- [ ] Python checks, dependency hygiene, tests, and repository checks pass.
- [ ] Project-owned JavaScript and CSS are declared as one web component and satisfy size budgets.
- [ ] Biome lint and formatting checks pass for every declared web asset.
- [ ] Setup, doctor, full verification, and audit succeed with the released tools.
