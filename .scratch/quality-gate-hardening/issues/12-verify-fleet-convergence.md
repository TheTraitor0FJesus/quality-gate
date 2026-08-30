---
id: "12"
title: "Verify fleet-wide policy convergence"
state: todo
blocked_by:
  - "07"
  - "08"
  - "09"
  - "10"
  - "11"
---

# 12 — Verify fleet-wide policy convergence

**What to build:** Demonstrate that every consumer repository uses the same immutable policy release and matching reusable workflow revision and that the complete fleet is green under that contract.

**Blocked by:** 07 — Migrate ARGUS_BRAIN; 08 — Migrate ARGUS_TG; 09 — Migrate ARGUS_tracker_multiuser; 10 — Migrate ARGUS_TRADING; 11 — Migrate ARGUS_WEB.

**Implementation uncertainty:** Low — this ticket verifies convergence rather than introducing another policy behavior.

- [ ] All five consumer manifests select the same unified policy release.
- [ ] All five reusable workflow callers select the matching reviewed workflow revision.
- [ ] Setup, doctor, full verification, and audit pass independently in every repository.
- [ ] No active project configuration refers to an older policy release or mismatched workflow revision.
- [ ] The fleet result records the verified release, workflow revision, platforms, and repository set.
