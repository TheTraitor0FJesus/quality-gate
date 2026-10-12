---
id: "03"
title: "Доказать передачу кандидата релиза через prepare CLI"
state: todo
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
