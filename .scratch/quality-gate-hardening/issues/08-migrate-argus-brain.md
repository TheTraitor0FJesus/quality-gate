---
id: "08"
title: "Migrate ARGUS_BRAIN to the unified gate"
state: todo
blocked_by:
  - "07"
---

# 08 — Migrate ARGUS_BRAIN to the unified gate

**What to build:** Move ARGUS_BRAIN to the unified policy release and resolve its Python policy and dependency-hygiene findings without weakening the shared contract.

**Blocked by:** 07 — Publish the unified immutable policy release.

**Implementation uncertainty:** Low — the target release, migration boundary, and highest verification seam are known even though the first run determines the remediation volume.

- [ ] The manifest selects the unified policy release and the workflow selects its matching reviewed revision.
- [ ] Ruff, mypy, dependency hygiene, tests, and repository checks pass on the final project state.
- [ ] Any necessary waiver is narrow, justified, owned, and time-bounded.
- [ ] Setup, doctor, full verification, and audit succeed with the released tools.
