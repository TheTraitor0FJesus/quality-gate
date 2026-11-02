---
id: incident-2026-10-candidate-policy-compatibility
status: learned
incident: The owner-merged v4.0.0 candidate failed its isolated artifact audit on Linux and Windows.
expected_layer: release.artifact_audit
miss_cause: Pre-merge checks selected published v3.1.0, while edit compatibility tests covered only historical policies and omitted the current product pin.
adaptation: Exercise the current product pin through the real wheel CLI compatibility test and stream the artifact audit's redacted report to the release log.
evidence: test_wheel_on_pythonpath_checks_working_files_without_extraction includes the current product version; test_release_controller_rejects_a_failed_artifact_stage verifies visible audit diagnostics. The original v4.0.0 scanner comparison fails with exit 2 instead of 1.
---

# Candidate policy compatibility

PR #94 merged source `10056484d43c811cea6ded65add560519066c573` for v4.0.0.
Release run `40118703163` rejected both platform artifacts during their private
audits. The shared edit selector supported policies only through v3.2.0, so
candidate-backed scanner and typing tests could not select their own policy.
The release controller captured and discarded the audit report, leaving only
the stage and exit code in the CI log.

The source manifest must retain its published pin during release preparation.
Test the current product version separately through the existing wheel CLI seam
and qualify the exact candidate artifact in its isolated release cache.
Keep unknown versions fail-closed. A corrected source requires a new owner-reviewed
release PR; the original candidate and any existing assets retain their identities.
