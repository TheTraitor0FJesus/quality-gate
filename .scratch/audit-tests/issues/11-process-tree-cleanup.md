---
id: "11"
title: "Release descendant resources before timeout cleanup"
state: todo
blocked_by: []
---

# 11 — Release descendant resources before timeout cleanup

## What to build

Repair confirmed timeout resource ownership at the runner's shared execution
boundary. Read the architecture map, agent reference and quality policy guide;
follow [specification group 11](../spec.md#11--complete-process-tree-cleanup-on-timeout).

## Acceptance criteria

- A timed-out real parent/child/grandchild command releases nested workspace locks
  and working directories before candidate/workspace cleanup.
- Windows venv launchers, inherited output pipes and a leader exiting before its
  descendants have distinguishing real-process proof. Owned descendants and
  reader threads cannot remain active after the result returns.
- Containment/reaping failures stay visible and bounded. The original check
  failure remains assessable when cleanup also fails; successful cleanup does
  not convert timeout or unavailable verification into a pass.
- Existing active-process workspace synchronization, verdict classification,
  bounded output and other supported platform contracts remain protected.
- The exact WinError 32 experiment fails on the original implementation and
  passes after repair; complete current-source verification also passes.

## Blocked by

None — classifier proof in ticket 06 and fixture ownership in ticket 09 are
independent outcomes.

## Implementation uncertainty

Medium — the parent-only kill defect is proven, but the earlier `taskkill /T /F`
prototype did not pass the full Gate. Inspect existing containment reuse before
selecting an implementation; do not treat that prototype as an accepted fix.
