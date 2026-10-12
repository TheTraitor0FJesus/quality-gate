---
id: "01"
title: "Доказать точность исключений для секретов"
state: todo
blocked_by: []
---

# 01 — Доказать точность исключений для секретов

## What to build

Protect exact credential exceptions and isolate each invalid waiver-admission
rule. Authoritative diagnosis, seams, preservation and verification:
[specification group 01](../spec.md#01--exact-secret-waivers).

## Acceptance criteria

- A same-target finding with a different fingerprint remains blocking while the
  reviewed matching credential is waived; raw secrets stay redacted.
- Each invalid approval, target, date and fingerprint-shape case fails for its
  own admission reason; valid acceptance and unknown-field rejection remain.
- Targeted faulty/corrected proof and applicable shared verification are recorded
  as required by the specification, with original production source restored.

## Blocked by

None — can start immediately.

## Implementation uncertainty

Low — matching/admission owners and existing fake-scanner/CLI seams are known.
