---
id: incident-2026-11-rollout-compare-path
status: learned
incident: Consumer rollout could create pull requests but failed to refresh existing ones for v4.6.0.
expected_layer: python.component_1.pytest
miss_cause: Rollout tests replaced the GitHub client and missed its rejection of the valid compare API triple-dot separator.
adaptation: Permit only GET comparisons between full commit SHAs through the traversal guard and exercise rollout refresh through the real GitHub client.
evidence: tests/test_consumer_rollout.py tests/test_release_rollout.py tests/test_publication_github.py
---

# Existing rollout pull requests failed to refresh

[Consumer rollout run 41284745758](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/41284745758)
failed for three consumers with existing open rollout pull requests. The shared client rejected
`/compare/<base-sha>...<head-sha>` because its traversal guard prohibited every `..` substring.

The regression checks retain the real GitHub client and replace only command execution. They cover
refreshing the same pull request, synchronizing a branch when behind, and rejecting traversal or
malformed compare requests before command execution.
