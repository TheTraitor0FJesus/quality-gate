---
id: "02"
title: "Enable the high-signal Ruff policy"
state: done
blocked_by:
  - "01"
---

# 02 — Enable the high-signal Ruff policy

**What to build:** Make every declared Python component block high-signal Bugbear, Ruff-native, and security findings while preserving valid test patterns and the existing maintainability budgets.

**Blocked by:** 01 — Refresh the pinned Python quality toolchain.

**Implementation uncertainty:** High — the stable RUF and S rules must be calibrated across the gate and all five consumer repositories before the final shared selection is fixed.

- [ ] The shared policy enables B and a documented, calibrated selection of stable RUF and S rules.
- [ ] Cyclomatic complexity remains capped at 10 and statements per function remain capped at 50.
- [ ] Test-only security exceptions are narrow and do not suppress the equivalent production finding.
- [ ] Every excluded or waived candidate rule has a concrete false-positive or overlap rationale.
- [ ] The gate repository and a diagnostic pass over all consumers demonstrate the final policy surface.
