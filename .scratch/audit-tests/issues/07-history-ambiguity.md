---
id: "07"
title: "Проверить отказ сканирования при неоднозначном history base"
state: todo
blocked_by: []
---

# 07 — Проверить отказ сканирования при неоднозначном history base

## What to build

Distinguish ambiguous successful reference resolution from a missing repository
or scanner prerequisite. Authoritative requirements:
[specification group 07](../spec.md#07--ambiguous-history-reference-refusal).

## Acceptance criteria

- Distinct successfully resolved local/remote commits produce ambiguity refusal
  without scanner execution, while a same-commit control reaches scanning.
- Missing-base, shallow-history and redaction protection remain intact.
- First-success selection instead of ambiguity refusal makes the new scenario
  fail for its intended assertion; restoration and shared verification are recorded.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — resolver semantics, disposable Git input and scanner boundary are known.
