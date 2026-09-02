---
id: "04"
title: "Add web components and static asset budgets"
state: todo
blocked_by: []
---

# 04 — Add web components and static asset budgets

**What to build:** Let a repository declare project-owned web components whose staged JavaScript and CSS assets are checked against consistent per-file and component-wide size budgets. This ticket owns platform-neutral source behavior; ticket 07 owns release-backed Windows/Linux parity.

**Blocked by:** None — can start immediately.

**Implementation uncertainty:** High — this introduces a new public component contract and a new repository check surface.

- [ ] A web component declares its root and the project-owned JavaScript and CSS assets within that boundary.
- [ ] Default limits are 100 KiB per JavaScript file, 50 KiB per CSS file, 250 KiB total JavaScript, and 100 KiB total CSS.
- [ ] Generated or vendor assets are excluded only by explicit component configuration.
- [ ] Invalid paths, unsafe patterns, duplicate component identities, and files outside the repository are rejected by manifest validation.
- [ ] Focused tests prove platform-neutral size measurement and stable findings for Windows- and POSIX-style paths.
- [ ] Ticket 07 proves identical released check results on Windows and Linux.
