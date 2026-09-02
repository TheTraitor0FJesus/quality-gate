---
id: "05"
title: "Add Biome web quality checks"
state: todo
blocked_by:
  - "04"
---

# 05 — Add Biome web quality checks

**What to build:** Lint and verify formatting for every declared JavaScript and CSS asset with one pinned standalone Biome policy that does not require Node.js. This ticket owns the source contract and candidate inventory inputs; ticket 07 owns final platform artifacts and release-backed parity.

**Blocked by:** 04 — Add web components and static asset budgets.

**Implementation uncertainty:** Low — the release already distributes verified platform-specific external tools, and the web component defines the file boundary.

- [ ] Stable Biome JavaScript and CSS lint rules block violations for declared web assets.
- [ ] Biome formatting is checked without modifying the staged candidate.
- [ ] Nursery, experimental language, SCSS, and embedded-language checks remain disabled.
- [ ] Candidate inventory inputs pin the Windows and Linux binaries and their reviewed digests exactly.
- [ ] Missing or corrupt Biome produces `unchecked` in focused contract tests.
- [ ] Ticket 07 packages the pinned binaries and proves their released local/CI and Windows/Linux parity.
