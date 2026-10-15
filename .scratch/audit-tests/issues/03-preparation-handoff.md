---
id: "03"
title: "Доказать передачу кандидата релиза через prepare CLI"
state: done
blocked_by: []
---

# 03 — Доказать передачу кандидата релиза через prepare CLI

## What to build

Observe the real preparation envelope and Actions outputs consumed by upload and
product jobs. Authoritative requirements:
[specification group 03](../spec.md#03--preparation-envelope-and-actions-output-handoff).

## Acceptance criteria

- Successful preparation produces the expected canonical candidate file and
  exact digest, original run, artifact-name and upload-required outputs without
  leftover temporary files.
- Reused/recovery identity forwarding and validated/no-release outcomes obey
  the existing envelope-retention contract at the same actual producer seam.
- Wrong file/digest/identity/upload flag is caught; independent state-machine,
  provenance and build protection remain, with shared verification recorded.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — producer/consumer contracts and the controlled Publisher service are known;
test-host selection is a local implementation choice.

## Execution record

- Added five integration cases at the real `publication_cli._main` and
  `_outputs` seam with a controlled Publisher. They cover initial and reused
  preparation, recovery, validation-only and no-release outcomes, and assert the
  canonical envelope, digest, identity/output forwarding and temporary-file
  cleanup.
- The focused `tests/test_publication_flow.py` run passed: 77 passed. Mutation
  checks for all five handoff assertions produced the expected failures; the
  implementation source was restored before review.
- Independent Standards and Spec reviews found no actionable findings. No
  production publisher change was needed.
- PR 55 merged commit `546e2cb` with shorter per-test Windows temporary paths
  for the parity and candidate-policy integration tests. This addressed the
  unrelated staged-Gate path failures recorded in ticket 02.
- Staged Quality Gate request `audit03-pr55-staged-20261015-01` on merged
  candidate tree `e37e65f17e1239517912f8ec473a9ab973e95c5f` passed with exit 0
  in 188 seconds.
