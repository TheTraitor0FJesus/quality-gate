---
id: "10"
title: "Self-host, document, and release Quality Gate v2"
state: done
blocked_by:
  - "07"
  - "08"
  - "09"
---

# 10 — Self-host, document, and release Quality Gate v2

**Transition gate rule:** Until ticket 18 is completed, the global commit hook must use the v1 runtime at `S:\GITHUB-REPOSITORIES\code_projects\quality-gate-v1-runtime`. No ticket may make v2 mandatory or replace this hook routing. Ticket 18 owns the final switch to v2.

**Outcome:** The public Quality Gate repository passes and governs itself with v2, publishes the first verified immutable release, and provides the English operating and migration contract for later agents.

**Blocked by:** 07 — Add whole-repository audit and the lessons loop; 08 — Run Quality Gate from native Git hooks; 09 — Run the same pinned gate in GitHub CI.

**Execution location:** Start in `S:\GITHUB-REPOSITORIES\specific_project\Entry-point`, then switch to `S:\GITHUB-REPOSITORIES\code_projects\quality-gate` before inspecting, changing, self-hosting, documenting, or releasing Quality Gate. Read that repository's instructions first. Keep this ticket in Entry-point; all repository changes and release evidence belong to Quality Gate.

- [ ] Quality Gate uses schema 2, its isolated runtime, complete audit, native hooks, pinned CI, and a release controller that rejects unlearned or unverifiable state.
- [ ] The complete supported-platform and failure-injection suites pass against rebuilt release artifacts.
- [ ] The public default branch has the specified human-reviewed GitHub ruleset and unique required status.
- [ ] Agent documentation covers operation, extension, troubleshooting, migration, rollback, audit, and lessons through concise pointers to authoritative contracts.
- [ ] The first release is published only after examples and documented commands pass against the exact artifacts being released.

**Handoff:** A released, documented v2 policy is available for isolated consumer migrations.
