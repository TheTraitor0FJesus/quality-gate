---
id: "09"
title: "Изолировать изменяемые фикстуры восьми runner-тестов"
state: todo
blocked_by: []
---

# 09 — Изолировать изменяемые фикстуры восьми runner-тестов

## What to build

Run the eight affected checks against disposable fixture copies while preserving
real workspace lifecycle and original source inputs. Authoritative requirements:
[specification group 09](../spec.md#09--isolate-runner-fixture-ownership).

## Acceptance criteria

- Each affected case owns function-scoped mutable input and preserves its original
  status, continuation and routing assertions under the actual outer workspace.
- Two focused runs and combined verification leave original fixture paths/content
  unchanged; existing root-lock and workspace lifecycle protection remain.
- Required pre-change focused timing, clean-tree comparisons, owned-artifact
  cleanup and applicable shared verification are recorded without a speed-up claim.
- Disposable copies exclude generated `temp/` and bytecode. Real outer-workspace
  locking and setup/call/teardown measurements remain observable; removing that
  lifecycle or hiding its errors is not an optimization.

## Performance handoff

Two isolated complete runs again created the five source-fixture lock roots;
see [performance evidence](../performance-evidence.md#isolated-baseline).
This repair enables [ticket 13](13-complete-verification.md), but makes no
speed-up promise. [Ticket 12](12-cli-infrastructure.md) separately investigates
measured immutable-infrastructure reuse; retaining code is a valid outcome when
no safe improvement is demonstrated.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — the eight owners and existing disposable-copy pattern are identified.
