---
id: "07"
title: "Publish the unified immutable policy release"
state: done
blocked_by:
  - "02"
  - "03"
  - "05"
  - "06"
---

# 07 — Publish the unified immutable policy release

**What to build:** Package and publish one immutable Quality Gate release that delivers the refreshed Python toolchain, dependency hygiene, web asset budgets, Biome checks, and supplemental test declarations through the same verified local and CI contract. This is the only implementation ticket that claims release-backed Windows/Linux parity.

**Blocked by:** 02 — Enable the high-signal Ruff policy; 03 — Add dependency hygiene checks; 05 — Add Biome web quality checks; 06 — Add supplemental test declarations and unified final verification.

**Implementation uncertainty:** Low — the repository already defines the release controller, immutable inventory, self-host validation, and human-reviewed publication procedure.

- [x] The release inventory contains the exact source-pinned versions from tickets 01, 03, and 05 plus every required policy file, wheel, external binary, and digest for Windows and Linux.
- [x] Templates and user-facing documentation describe the final Python, web, and supplemental test contracts.
- [x] Self-host validation, the full suite, audit, and release validation pass on the exact candidate before publication.
- [x] The release tag and assets are immutable and correspond to the reviewed source revision.
- [x] The gate repository selects the published release through its staged manifest and matching reusable workflow revision.
- [x] Release-backed jobs on Windows and Linux report the expected exact tool inventory and identical check surface, verdicts, redaction, history, and `unchecked` behavior, and the comparison job passes.
