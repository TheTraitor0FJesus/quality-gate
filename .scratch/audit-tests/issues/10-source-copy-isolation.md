---
id: "10"
title: "Exclude active scratch from source-wheel copies"
state: todo
blocked_by: []
---

# 10 — Exclude active scratch from source-wheel copies

## What to build

Repair the source-copy boundary in `tests/test_ci_workflow.py::_build_wheel`.
Authoritative diagnosis and preservation requirements:
[specification group 10](../spec.md#10--exclude-active-scratch-from-source-wheel-copies).

## Acceptance criteria

- Copying while a real workspace is active does not read or copy its `temp/`
  tree, including when the build destination lies beneath generated scratch.
- Required source files, cached-wheel selection, real wheel creation and installed
  CLI/parity controls remain protected; no source build is replaced by fixture bytes.
- Reinstating the old exclusions makes the regression proof fail. Active workspace
  content and lifetime remain unchanged, and all test-owned copies are cleaned.
- Focused timing and complete verification are recorded without a speed-up claim.

## Blocked by

None — this copy defect is independent of timeout process-tree cleanup.

## Implementation uncertainty

Low — the copy owner and WinError 33 reproduction are established. Exact speed
benefit has not been measured and is not required for this correctness repair.
