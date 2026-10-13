---
id: "01"
title: "Доказать точность исключений для секретов"
state: done
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

## Execution record

- The matching and same-target mixed-finding controls cover both outcomes: a sole
  reviewed finding is WAIVED, while a different fingerprint at the same target
  remains FAILED. Compact and verbose output keep both raw values redacted.
- Fault injection: ignoring the fingerprint comparison made the same-target candidate test return `WAIVED` instead of `FAILED`. Removing each target, approver, expiry, review-date, or fingerprint-shape guard made its isolated CLI case accept the invalid waiver.
- All production mutations were restored; `quality_gate/contracts.py` matches its original Git blob object ID `957fca5f3ddab8210011ba298d3879b03bad0254`.
- Focused waiver tests: `audit-tests-01-final-focused-20261013-d415b1af-1dbb-4aaf-8b6a-6db93cbd6855` — 9 passed, 36 deselected.
- Preserved native-hook and runner-contract controls: `audit-tests-01-final-controls-20261013-9dc923c0-1a8b-4d67-9c6d-ed671844e2d1` ran the seven exact node IDs below — 7 passed.
  - `tests/test_hooks.py::test_native_pre_push_hook_blocks_default_branch_and_allows_feature_branch`
  - `tests/test_native_git_hooks.py::test_real_git_commit_preserves_staged_and_unstaged_state`
  - `tests/test_native_git_hooks.py::test_real_git_pre_push_blocks_default_updates_and_allows_feature_pushes`
  - `tests/test_native_git_hooks.py::test_native_hook_setup_preserves_unrelated_files_and_refuses_conflicts`
  - `tests/test_native_git_hooks.py::test_native_hook_documentation_records_local_bypass_limits`
  - `tests/test_runner_contract.py::test_prompt_contract_orders_supplementals_before_the_staged_gate`
  - `tests/test_runner_contract.py::test_manifestless_full_scope_prompt_contract_keeps_ordinary_suite_discovery`
- Full Windows/Python 3.12 suite on the prior code candidate: `audit-tests-01-final-full-20261013-c84a4efd-ae4c-45f9-9001-778561f6646c` — 574 passed, 7 skipped, 0 failed. After this documentation-only correction, `audit-tests-01-record-full-20261013-72e85580-55af-428a-a652-96cc924eab8b` again passed all 574 passing tests; its compact runner summary omitted the skip total.
- The exact seven platform skips in `audit-tests-01-final-full-20261013-c84a4efd-ae4c-45f9-9001-778561f6646c` were:
  - `tests/test_distribution_runtime.py::test_sync_restores_executable_permissions_for_zip_tools` — POSIX executable permissions are unavailable on Windows.
  - `tests/test_distribution_runtime.py::test_repeat_sync_repairs_existing_zip_tool_permissions` — POSIX executable permissions are unavailable on Windows.
  - `tests/test_distribution_runtime.py::test_runtime_identity_rejects_dependency_symlink_outside_repository` — symbolic links are unavailable on this platform.
  - `tests/test_integrity.py::test_git_checks_collect_hygiene_classes_without_binary_content` — Windows cannot materialize the two case-colliding paths in one working tree.
  - `tests/test_integrity.py::test_unsafe_symlink_is_reported_when_supported` — symbolic links are unavailable on this platform.
  - `tests/test_runner.py::test_web_budget_does_not_measure_an_asset_through_an_external_link` — directory symbolic links are unavailable on this platform.
  - `tests/test_snapshot.py::test_candidate_snapshot_preserves_staged_executable_mode` — Windows does not expose executable mode bits.
- Staged Quality Gate after the first execution-record update: `audit-tests-01-record-staged-gate-20261013-10a00ac3-4acd-4cd0-bfdd-fcc4670a246e` — exit 0 on tree `db9c547cb989988f118525c078297655fbe4908f` from the short-path checkout `C:\q`. Its typed-runner response reported the green verdict but omitted collection totals.
- On predecessor candidate tree `29d2f4e56bef7c0ec0ac7dcc00e9198af9e7dc30`, full Quality Gate run `audit-tests-01-full-short-path-valid-20261013-22` passed; its aligned test run `audit-tests-01-skip-xml-safe-exact-env-20261013-29` collected 580 tests: 566 passed, 14 skipped. Those 14 skips were the seven platform cases above plus these seven isolated-settings-root controls. The current candidate adds one positive waiver test; the final staged gate on it exited 0, while the compact runner response omitted collection totals.
  - `tests/test_hooks.py::test_native_pre_push_hook_blocks_default_branch_and_allows_feature_branch` — native hook wrapper unavailable under isolated settings root.
  - `tests/test_native_git_hooks.py::test_real_git_commit_preserves_staged_and_unstaged_state` — native pre-commit wrapper unavailable under isolated settings root.
  - `tests/test_native_git_hooks.py::test_real_git_pre_push_blocks_default_updates_and_allows_feature_pushes` — native pre-push wrapper unavailable under isolated settings root.
  - `tests/test_native_git_hooks.py::test_native_hook_setup_preserves_unrelated_files_and_refuses_conflicts` — native hook setup wrapper unavailable under isolated settings root.
  - `tests/test_native_git_hooks.py::test_native_hook_documentation_records_local_bypass_limits` — native hook documentation unavailable under isolated settings root.
  - `tests/test_runner_contract.py::test_prompt_contract_orders_supplementals_before_the_staged_gate` — typed test-runner configuration unavailable under isolated settings root.
  - `tests/test_runner_contract.py::test_manifestless_full_scope_prompt_contract_keeps_ordinary_suite_discovery` — typed test-runner configuration unavailable under isolated settings root.
- A staged gate attempt from the long canonical checkout path previously failed only when Windows could not create nested fixture paths (`WinError 3`) in `tests/test_ci_workflow.py::test_ci_and_local_cli_runs_match_on_a_release_backed_repository` and `tests/test_release.py::test_release_controller_audits_with_candidate_policy_when_source_pin_is_previous`. Both tests passed individually, and the final staged gate passed against the identical candidate tree from `C:\q`.
- Unverified obligations: Linux behavior; live GitHub/GHCR publication; separate Node-fixture execution; coverage. The full-suite skips above still need Linux evidence.
- Final post-review candidate verification on tree `f36b3b88805962316eca2082bfe995592bed77b8`: full suite `audit-tests-01-fixcli-full-20261013-4e8b89cb` — 574 passed, 7 skipped, 0 failed; staged Quality Gate `audit-tests-01-fixcli-gate-20261013-7cb239af` from the short-path checkout `C:\q` — exit 0.
