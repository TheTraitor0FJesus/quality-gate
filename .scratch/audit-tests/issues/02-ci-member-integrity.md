---
id: "02"
title: "Проверить целостность членов CI-архива до извлечения"
state: done
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

## Execution record

- The production extractor already compares each declared member digest before
  `extractall`; no production code change was needed.
- Added exact-diagnostic cases for a policy wheel, a policy file, and a native
  tool. Each case recomputes trusted outer metadata from the final ZIP and checks
  both a new target and an existing target with a sentinel.
- Fault proof `ci-member-integrity-mutation-20261014-01` changed only the member
  digest condition. The focused file reported 17 passed and 6 failed; every new
  negative failed with `DID NOT RAISE CiReleaseError` as intended.
- Restored `quality_gate/ci_release.py` byte-for-byte. Its SHA-256 is
  `A124B2E7E7ED650478726CB00BED7D281E7E82ECD428EF306F325A4F68AC44D2` and its
  Git blob is `67c6db9a9c2a3b6789edbe885d9cff2de0a211f2`.
- Corrected-source run `ci-member-integrity-corrected-20261014-02` passed
  `tests/test_ci_release.py`: 23 passed.
- Shared verification exposed two Windows fixture path failures in the parity
  test and the candidate-policy release-controller test. A 119-character pytest
  base reproduced their cache staging paths at 262 and 263 characters; both
  failed with `WinError 3`. This was separate from member checksum validation.
- Kept each repository function-scoped and used short `tmp_path_factory.mktemp`
  names (`parity` and `candidate`). The corresponding staging paths are 238 and
  242 characters; artifact construction, verification, installation and CLI
  assertions remain real. No production temporary-workspace behavior changed.
- Long-base corrected proof `ci-member-path-corrected-20261015-21` passed both
  original integration tests with the same 119-character pytest base. The
  diagnostic base was removed after each run.
- Shared verification on review candidate tree
  `968460f55aa61549e1806971807d70950c6aea2f` was green:
  `ci-member-candidate-full-20261015-24` ran manifest validation and the installed
  staged Gate (exit 0; no supplemental declarations). Request
  `ci-member-current-source-full-20261015-25` ran current-source `doctor`,
  `python -B -m pytest -p no:cacheprovider -q --tb=long -ra --durations=25`,
  and `python -B -m quality_gate --root . check --verbose`; all exited 0.
  Direct pytest reported 580 passed and 7 skipped in 188.72 seconds, with 587
  collected: the previous 581 plus six member-integrity cases.
- After recording the skip and platform evidence, the final staged candidate tree
  `91e093328c1b88482a3fe5bb13aacf9c0f2a03a3` passed manifest validation and the
  installed staged Gate in the final full verification. The manifest declares no
  supplemental tests; both required stages exited 0 and the Gate reported 14
  skips. The only changes from review tree `968460f` were this
  execution record and the context pointer in ticket 13; implementation and test
  code were unchanged.
- Correction after final review: collection-only diagnostics captured 587 node
  IDs in both the direct checkout and Gate's isolated candidate; the ID sets are
  identical, so no existing test was omitted or deselected. Seven platform
  skips and seven isolated-settings skips account for the 14 Gate skips; the
  587 collected tests therefore reconcile to 573 passed and 14 skipped. The
  earlier `572 passed` figure was a transcription error. The temporary hook and reports were removed;
  its staged diagnostic check exited 1 and was not used as a verification verdict.
- The exact seven direct-suite skips were:
  - `tests/test_distribution_runtime.py::test_sync_restores_executable_permissions_for_zip_tools`
    and `::test_repeat_sync_repairs_existing_zip_tool_permissions`: POSIX executable
    permissions are not portable to Windows.
  - `tests/test_distribution_runtime.py::test_runtime_identity_rejects_dependency_symlink_outside_repository`:
    symbolic links unavailable on this platform.
  - `tests/test_integrity.py::test_git_checks_collect_hygiene_classes_without_binary_content`:
    Windows cannot materialize two case-colliding files.
  - `tests/test_integrity.py::test_unsafe_symlink_is_reported_when_supported`:
    symbolic links unavailable on this platform.
  - `tests/test_runner.py::test_web_budget_does_not_measure_an_asset_through_an_external_link`:
    directory symbolic links unavailable on this platform.
  - `tests/test_snapshot.py::test_candidate_snapshot_preserves_staged_executable_mode`:
    Windows does not expose executable mode bits.
- The seven native-hook/settings controls listed in
  [ticket 01's execution record](01-secret-waivers.md#execution-record) passed in
  the direct full suite. Gate reports retain the green verdict; the direct run
  supplies collection and skip evidence outside the Gate's isolated settings root.
- Verification used the canonical checkout without shortening its root. Generated
  pip cache and five lock-only fixture `temp/` directories were removed; the
  original fixture file list and tracked contents were preserved. Initial
  Standards and Spec reviews confirmed the code candidate; the Spec request to
  record final shared verification is addressed above.
- Remaining platform obligations: Linux and the seven skipped Windows cases on
  a supporting platform. Live GitHub/GHCR publication, separate Node-fixture
  execution and coverage were not exercised. Post-combination canonical-path
  revalidation is handed to [ticket 13](13-complete-verification.md).
