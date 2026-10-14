---
id: "12"
title: "Measure repeated CLI setup and attempt safe reuse"
state: todo
blocked_by: []
---

# 12 — Measure repeated CLI setup and attempt safe reuse

## What to investigate

Separate repeated scanner-release preparation from child execution in
`tests/test_contracts_cli.py::_run`. Apply changes only when the measurements
identify safe, worthwhile reuse. Follow
[specification group 12](../spec.md#12--investigate-repeated-immutable-cli-preparation)
and the integration-fixture rules in the repository coding standards.

## Acceptance criteria

- Focused setup/call/teardown and preparation/process costs are recorded before
  ownership changes, with operation owner, classification and impact.
- Any shared state is verified immutable infrastructure; consumer repositories,
  manifests, indexes, mutable caches and fault injections remain function-scoped.
- Real scanner execution, digest verification, artifact installation and all
  existing positive/negative assertions remain distinguishing proof. Release
  initialization under test and native hooks are not mocked away.
- An accepted optimization improves equivalent focused/full measurements and
  survives repetition and reordered tests. Collection and skip differences are
  reconciled; no test is removed for speed.
- If no safe measurable improvement is established, retain the code and record
  the measured limitation. This is a valid investigation outcome; no percentage
  improvement or absolute suite time is promised.

## Blocked by

None — preparation profiling and any isolated infrastructure change can proceed
independently. Ticket 13 combines its outcome with the reliability repairs.

## Implementation uncertainty

Medium — focused attribution confirms a 33.65-second parent-side remainder and
17 synchronizations. A compatible sharing boundary and the achievable saving
still need proof; no implementation or speed-up is accepted yet.
