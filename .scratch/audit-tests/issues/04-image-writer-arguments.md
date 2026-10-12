---
id: "04"
title: "Проверить аргументы CLI image writer"
state: todo
blocked_by: []
---

# 04 — Проверить аргументы CLI image writer

## What to build

Make the existing CLI test observe exact PR, candidate and receipt-directory
forwarding. Authoritative requirements:
[specification group 04](../spec.md#04--image-writer-argument-forwarding).

## Acceptance criteria

- The CLI forwards the supplied original PR, candidate ID and output directory
  exactly once; its existing success/exit-output assertions remain.
- Independently wrong PR, candidate ID or directory makes the forwarding
  assertion fail, with exact restoration and focused verification recorded.
- Direct writer identity and workflow input/pin protection remain intact;
  applicable shared verification passes.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — the existing test and observable forwarding seam are explicit.
