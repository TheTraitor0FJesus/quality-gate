---
id: "13"
title: "Verify complete coverage and a measured time budget"
state: todo
blocked_by: ["09", "10", "11", "12"]
---

# 13 — Verify complete coverage and a measured time budget

## What to verify

Accept the complete candidate after reliability and setup investigations. Follow
[specification group 13](../spec.md#13--complete-verification-and-time-budget-acceptance)
and retain [baseline evidence](../performance-evidence.md).

## Acceptance criteria

- Two sequential isolated complete pytest runs and the real current-source/staged
  Gate are tied to exact candidate identities, commands, interpreter and plugins.
- Existing tests remain, with every skip/deselection explained. Compare direct
  full-suite coverage with the Gate's isolated environment; green Gate alone does
  not substitute for native-hook/settings controls that its environment skips.
- Record focused and complete durations without claiming improvement from changed
  collection or environment. Completeness takes priority over speed; 240 seconds
  is not an acceptance threshold.
- The actual timed command has a justified margin. Any timeout edit follows the
  manifest contract and is backed by measurements; raising it alone does not
  satisfy process-resource recovery.
- Canonical nested Windows installation paths are checked. If the historical
  WinError 3 recurs, identify the failing producer and scope its repair from
  current evidence; a short-checkout workaround is not canonical acceptance.
- Gate is green, timeout recovery leaves no owned descendants or locks, source
  fixtures are unchanged, and verification workers are no longer active.

## Blocked by

Tickets 09, 10, 11 and 12 — final proof must include fixture isolation, scratch
copying, descendant cleanup and the measured infrastructure decision. Ticket 12
can complete with an evidence-backed no-change outcome.

## Implementation uncertainty

Medium — full-suite timing is measured, but post-repair recovery, Gate environment
coverage and canonical nested-path acceptance remain to be established.

## Context from ticket 02

[Ticket 02's execution record](02-ci-member-integrity.md#execution-record) records
current reproductions of the historical Windows cache-staging failures at 262
and 263 characters, and the short per-test directory names that repaired them.
Its staged and current-source Gate checks passed from the canonical checkout.
Retain those test directory names when combining tickets 09–12, then repeat the
canonical-path, timing, resource and skip verification required above.
