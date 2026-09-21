---
id: incident-2026-09-release-test-history
status: learned
incident: The first owner-merged 2.1.0 release failed nine CLI tests on both platforms.
expected_layer: release.full_suite
miss_cause: Direct pytest inherited PR history metadata that the ordinary Quality Gate isolates.
adaptation: Remove parent history-selection variables only from the release test subprocess environment.
evidence: test_release_build_isolates_test_history_and_preserves_evidence covers environment isolation and failed-test evidence rejection.
---

# Release tests under a pull-request event

The release trigger changed from push to a merged pull request. Disposable test repositories
inherited the parent event's base and head commits, making their history checks unavailable.
Candidate scanning succeeded; the generic Gitleaks recovery message obscured the history mismatch.

Keep Actions identity in the build process for publication evidence. Isolate only the child test
process, preserve its pinned policy/cache, and emit no evidence after a failed test suite.
