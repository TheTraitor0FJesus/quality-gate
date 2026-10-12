---
id: "06"
title: "Доказать классификацию ошибок сбора pytest"
state: todo
blocked_by: []
---

# 06 — Доказать классификацию ошибок сбора pytest

## What to build

Exercise actual subprocess-output classification while retaining downstream
verdict routing and redaction. Authoritative requirements:
[specification group 06](../spec.md#06--pytest-result-classification).

## Acceptance criteria

- Real classification maps collection-marker outcomes to unchecked, ordinary
  assertion failure to failed, and does not grant pytest treatment to other tools.
- Existing CLI redaction, result propagation and declared test-path assertions
  remain independently protected.
- Disabling collection detection makes the new classifier proof fail; exact
  restoration and applicable focused/shared verification are recorded.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — the classifier and controlled process-output test seam already exist.
