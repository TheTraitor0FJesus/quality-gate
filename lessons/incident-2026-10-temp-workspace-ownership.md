---
id: incident-2026-10-temp-workspace-ownership
status: open
incident: Edit checks could remove unrelated entries from a consumer's temp directory.
expected_layer: python.component_1.pytest
miss_cause: Stale cleanup treated every non-active child as disposable without proof that Gate owned it.
adaptation: Reclaim only workspaces with a valid root-bound ownership marker and an exclusively acquired run lock; preserve ambiguous entries.
evidence: tests/test_temp_workspace.py covers foreign and misleading entries, active and interrupted workers, links, Windows read-only objects, public edit modes and normal cleanup.
---

# Temporary workspace reclamation requires ownership proof

The edit manager previously treated the consumer's `temp/` directory as if every
child belonged to Quality Gate. A live lock protected active work, but an absent
lock did not prove ownership. Ordinary edit checks could therefore remove
unrelated files or another tool's work directory.

Write an ownership marker only after acquiring the run lock. Reclaim a stale
workspace only when its marker binds the exact temp root and directory name and
the existing lock can be acquired exclusively. Keep unknown entries, active
workers, legacy directories, links and the shared root lock intact. Exercise
reclamation only with disposable roots, and include calls through each public
edit mode so routing and cleanup use the same manager.
