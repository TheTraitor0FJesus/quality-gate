---
id: "05"
title: "Различать отказы guards Biome и ошибки запуска"
state: todo
blocked_by: []
---

# 05 — Различать отказы guards Biome и ошибки запуска

## What to build

Prove size and digest admission guards before downstream execution rather than
accepting an unrelated launch error. Authoritative requirements:
[specification group 05](../spec.md#05--biome-guard-specific-rejection).

## Acceptance criteria

- Each negative observes its exact rejection reason and zero forbidden tool
  executions while keeping the actual selection/guard logic under test.
- Corrected-digest and allowed-size controls reach the downstream execution seam;
  missing-tool and real argument/asset protection remain.
- Removing either named guard makes its negative proof fail, with exact source
  restoration and applicable focused/shared verification recorded.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — both guards and a successful downstream recorder use existing seams.
