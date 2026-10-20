---
id: incident-2026-10-secret-release-cache
status: learned
incident: The owner-merged 3.0.0 candidate failed its isolated audit on Linux and Windows.
expected_layer: release.artifact_audit
miss_cause: A real-wheel comparison assumed a sibling v2.5.0 cache entry; local checks selected v2.5.0 and hid that dependency.
adaptation: Compare the supplied integrity-verified wheel with its version-specific fingerprint contract, without selecting an unrelated cached release.
evidence: test_selected_wheel_and_provider_preserve_their_fingerprint_contracts covers immutable v2 behavior and corrected v3 behavior; the isolated Linux audit reproduces the missing-cache failure before the fix.
---

# Secret comparison in an isolated release cache

The self-host release controller installs only the candidate in its private cache.
The later full-suite phase prepares the published source-policy pin in a different
cache. Its setup cannot supply a historical wheel to the earlier artifact audit.

The scanner comparison now uses the release supplied by its fixture. A v2 wheel
must retain its historical placeholder digest; a v3 wheel must hash the actual
synthetic value. Both are compared with the current provider's exact paths and
actual fingerprint, and neither may emit the synthetic value. Run candidate
qualification with only that candidate present as well as the pinned-policy suite.

Original failure: PR #61, release run `39403272564`, source
`5acc8064341c8bb197a613bfa0178ea8d41a8f96`. The retained candidate remains unchanged;
a new owner-reviewed release PR must authorize the corrected source.
