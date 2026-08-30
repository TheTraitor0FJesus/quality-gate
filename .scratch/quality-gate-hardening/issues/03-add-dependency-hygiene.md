---
id: "03"
title: "Add dependency hygiene checks"
state: todo
blocked_by: []
---

# 03 — Add dependency hygiene checks

**What to build:** Check each declared Python component for undeclared, transitive-only, misclassified, and unused runtime dependencies using its existing dependency declarations. This ticket implements and verifies the source contract; ticket 06 owns release packaging and cross-platform parity for deptry.

**Blocked by:** None — can start immediately.

**Implementation uncertainty:** High — the new check crosses component dependency inputs, isolated runtimes, release inventory, reporting, and typed waivers.

- [ ] Each applicable Python component produces one stable dependency-hygiene result.
- [ ] The check detects the supported deptry finding classes without treating development-only tools as unused runtime dependencies.
- [ ] Plugin, entry-point, configuration-driven, and dynamic imports can use the existing narrow waiver contract.
- [ ] Missing tool or unusable dependency metadata produces `unchecked`, never a false pass.
- [ ] The source contract pins deptry exactly and focused tests preserve stable results and verdict semantics.
- [ ] Ticket 06 packages that exact deptry version and proves identical local/CI and Windows/Linux results.
