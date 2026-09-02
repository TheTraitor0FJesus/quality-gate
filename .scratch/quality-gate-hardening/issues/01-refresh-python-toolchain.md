---
id: "01"
title: "Refresh the pinned Python quality toolchain"
state: done
blocked_by: []
---

# 01 — Refresh the pinned Python quality toolchain

**What to build:** Update the source pins for the existing Python policy tools to current compatible stable releases and prove source compatibility without changing the intended check surface. The unified release ticket owns packaging these pins and proving release-backed local/CI parity.

**Blocked by:** None — can start immediately.

**Implementation uncertainty:** Low — version pinning and compatibility checks already exist; release inventory and cross-platform parity are verified only after the unified candidate is packaged.

- [ ] Every existing Python policy tool is pinned to a current compatible stable release selected after reviewing its breaking changes.
- [ ] The source dependency groups resolve one identical exact version for every duplicated policy-tool declaration.
- [ ] The existing check surface and verdict semantics remain unchanged.
- [ ] Focused compatibility tests and the full suite pass with the refreshed tools.
- [ ] Ticket 07 explicitly owns packaging these exact versions and proving the resulting Windows/Linux inventory and verdict parity.
