---
id: "04"
title: "Проверить аргументы CLI image writer"
state: done
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

## Execution record

- Strengthened the existing CLI double with a call recorder and the independent
  expectation `[(7, 9, tmp_path)]`. The assertion proves exact argument forwarding
  and one invocation; the existing exit-code and stdout assertions remain.
- Independently mutated the dispatch in `quality_gate/publication_cli.py` to pass
  PR `8`, candidate ID `10`, and `options.output / "wrong"`. Requests
  `audit04-mutation-pr-20261017-01`, `audit04-mutation-candidate-20261017-02`,
  and `audit04-mutation-output-20261017-03` each collected one test and exited 1
  at the new call-list assertion, identifying the respective wrong tuple field.
- Restored the exact original source bytes between mutations and before the
  corrected run. The original and restored SHA-256 is
  `bb2c466814913c9aba04022ae8ed9b1af7489dba88802cb7bcbde8d77d2de8cb`;
  the production file and its index entry have no final diff.
- Corrected request `audit04-focused-restored-20261017-04` ran
  `python -B -m pytest -p no:cacheprovider -q --tb=long -ra tests/test_publication_github.py tests/test_publication_writer.py tests/test_publication_workflows.py tests/test_publication_flow.py`:
  198 passed, exit 0, 29.70 seconds. This retains direct writer identity,
  publication-flow and workflow input/pin protection. No tests were added or
  removed and no production behavior or architecture changed.
- `quality-gate doctor` initially reported a missing component runtime; the
  documented `quality-gate setup` recovery restored pinned policy `v2.4.2`,
  and the repeated doctor reported ready. No policy pin was changed.
