---
id: "08"
title: "Проверить настоящий producer Python-версий workflow"
state: todo
blocked_by: []
---

# 08 — Проверить настоящий producer Python-версий workflow

## What to build

Observe the actual workflow step's emitted Python-version record for supported
multicomponent manifests. Authoritative requirements:
[specification group 08](../spec.md#08--execute-the-workflow-python-version-producer).

## Acceptance criteria

- The real inline producer emits independently expected sorted distinct versions;
  duplicate, no-component and malformed-input outcomes match the current contract.
- Bootstrap ordering, setup input, override/fallback and immutable pins remain.
- A fixed single version or truncated record makes the observed-output test fail
  without live Actions; restoration and shared verification are recorded.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — the producer, output format and actual-workflow-script test pattern exist.

## Performance preservation

The same module's installed-release fixture is already module-scoped; isolated
baseline setup took 20.87–21.13 seconds. Preserve its real artifact, installation
and parity proof while strengthening the workflow producer. The optimization
investigation belongs to [ticket 12](12-cli-infrastructure.md); real nested Gate
coverage is part of [ticket 13](13-complete-verification.md).
