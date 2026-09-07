---
id: "06"
title: "Add supplemental test declarations and unified final verification"
state: done
blocked_by: []
---

# 06 — Add supplemental test declarations and unified final verification

**What to build:** Let a repository declare strict supplemental functional tests in `quality-gate.toml` without making them part of Quality Gate execution. Make the typed test-runner use one `scope: full` request to run those tests and then the staged gate, while repositories without Quality Gate retain ordinary complete-suite discovery.

**Blocked by:** None — can start immediately and must complete before release assembly.

**Implementation uncertainty:** Medium — the manifest contract, Codex test-runner instructions, setup skill, and repository CI boundary must agree without introducing arbitrary command execution or a second test catalog.

- [ ] Schema 2 accepts optional supplemental test declarations with a unique identifier, the strict `node-test` runner, and one or more safe repository-relative targets; arbitrary commands are not representable.
- [ ] Manifest validation rejects unknown runners, duplicate identities, unsafe or empty targets, and unmatched targets.
- [ ] `quality-gate check`, `audit`, native hooks, and the reusable workflow validate but do not execute supplemental tests.
- [ ] The typed test-runner interprets `scope: full` as complete repository verification: with `quality-gate.toml` it runs every declared supplemental test, collects all failures, then executes the staged Quality Gate; without the manifest it preserves ordinary full-suite discovery.
- [ ] A supplemental failure does not suppress the staged Quality Gate result, and no Quality Gate-owned pytest suite is rerun as an ordinary full suite.
- [ ] `docs/agent-reference.md` is the single manifest-maintenance guide, the canonical manifest template documents the optional declaration, and `$setup-repo` consumes that template instead of a private copy.
- [ ] The generated Quality contract pointer routes future component, test, dependency, document, workflow, and waiver changes to the guide and requires `quality-gate validate` after manifest edits.
- [ ] Repository-owned CI remains responsible for supplemental tests; the ARGUS_TRADING migration declares its existing `node:test` targets and preserves their separate CI execution.
- [ ] Focused manifest and orchestration tests plus the complete Quality Gate suite pass on the final source candidate; ticket 07 owns immutable packaging and released platform parity.
