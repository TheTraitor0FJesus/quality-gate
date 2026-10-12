---
id: "02"
title: "Проверить целостность членов CI-архива до извлечения"
state: todo
blocked_by: []
---

# 02 — Проверить целостность членов CI-архива до извлечения

## What to build

Prove inner member-integrity refusal independently of the trusted outer archive
identity. Authoritative requirements:
[specification group 02](../spec.md#02--ci-member-integrity-before-extraction).

## Acceptance criteria

- Wheel, policy-file and native-tool mismatches fail with their member-integrity
  diagnostic despite correct metadata for the complete final ZIP.
- Refusal creates no extracted content and preserves an existing target sentinel;
  valid extraction and other trust/path/resource controls remain green.
- Bypassing only inner digest comparison makes the new negative proof fail;
  source restoration and applicable shared verification are recorded.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — the extractor, archive fixture and pre-extraction failure boundary exist.
