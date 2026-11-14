# Issue 69: operation evidence, part 1

## Verification continuation (2026-11-03)

The checkpoints below describe their recorded candidates, not the final review state.
Run `issue69-pre-review-full-20261102-w` did not complete: its captured progress contained
two setup errors without tracebacks and reached 17%. The parent terminated the still-live
pytest tree after its 1800-second inner limit; subsequent inventory found no owned workers.
The executor used `Date.now()` and omitted complete failure evidence. Its aggregate result
is invalid (`incomplete_execution`, `traceback_truncated`, `duration_not_monotonic`), not a
completed red suite or a speed measurement. The earlier natural-exit claim was corrected.

Inspection found repeated nested copies of `release-gate0/quality-gate-source/temp/v69/`
in the retained tree. The existing CI test fixture copied the repository into a destination
under its own `temp/` without excluding that directory. Its ignore list now includes `temp`,
preventing self-copy when pytest temporary data is correctly kept inside the repository.
This is a supporting test-fixture correction, not a change to release or Gate behavior.
The two early setup errors cannot be attributed individually without their tracebacks.

The external runner's current contract separates full tests from the explicit staged Gate.
The two previously authorized prompt-contract tests now preserve the current ordering of
preflight, validated Python/supplemental declarations and execution, and assert that `full`
does not add a Gate. Whitespace normalization remains; runner configuration is unchanged.

`issue69-runner-contract-current-20261103-a` passed all six contract tests in 2207 ms on
tree `ae283539e168bf6bc1be84ff046a5fe8d8410fa0`.
`issue69-ci-fixture-current-20261103-b` passed all 17 CI-workflow tests in 33923 ms on
tree `76f7253ee82cffa78aa554e2f8c4cdf4b769b5f1`. Both completed with exit 0, disabled
pytest cache, existing Python 3.12 runtime, repository-root temporary paths, retained full
output and command-level `Diagnostics.Stopwatch` measurements. These are focused results;
required full, separate staged-Gate, current-source and commit-hook verification remain due.

The next complete default orchestration, `issue69-default-full-20261103-c`, passed preflight
in 379 ms and completed pytest with exit 1 in 1210538 ms: 1185 passed, 63 skipped and two
failed. No supplemental tests were declared. One wheel-backed `edit_cli/v4.2.1` case reported
`unchecked/deadline` with request elapsed 11406 ms; another typing-opt-out unit test never
reached its mocked checks because the global admission queue was busy. Full tracebacks remain
in `temp/issue69-verification/full-c-python.stdout.log`. This red run verifies tree
`9a556f472e7a099fafe8695b954e70d433f62cb6`, before the following corrections.

The typing-opt-out unit and a writable-temporary-directory runner unit now use the existing
`gate_admission`/`nullcontext` test isolation, as other runner units already do. Admission's
production behavior and dedicated tests are unchanged. Two additional deterministic regression
tests then exposed completed failures being replaced by `unchecked` when the stage's exit
deadline check failed. Both callback and command paths now retain the observed failed result;
the separate `runtime.gate_timeout` still blocks aggregate completion. The red pair is
`issue69-retention-both-red-20261103-f` (2703 ms). After correction and result-recording
deduplication, `issue69-retention-validation-20261103-i` passed Ruff, formatting, whole-component
mypy and 158 tests, with 30 skips, on tree `d551c22b1f9be65ab1246c51814cac0f1e44cd12`.
Its pytest process took 295047 ms. Earlier intermediate batches were red on complexity or
unit-test admission isolation and are not accepted aggregate passes.

The owner separately approved diagnosis of the wheel deadline without increasing edit-hook
limits. Replay `issue69-wheel-deadline-replay-20261103-j` called the original test function
with the retained failing wheel/cache, an equal-length project path and unchanged 10000 ms
request/15-second outer limits. Three fresh/reused trials made six real CLI calls and passed
in 6889 ms of command-level monotonic time. The wheel SHA-256 remained
`710dc3333651daa83c29c01ced932f4e8a85e59f2f655a24b98ac7e0d6389132`.
Zero of three trials reproduced the deadline: this does not establish its cause, absence or
a fix. Other local verification was observed concurrently during the failed full run; its
effect is unmeasured. Artifacts remain under `temp/w69-j/`. No edit-hook deadline changed.

The owner requested no queue waiting and a safe pause if no Gate slot was free. Readiness
probe `issue69-gate-slot-now-20261103-k` used the real admission API with queue timeout zero,
observed no free slot and launched no Gate. The requested pause was taken, then explicitly
resumed after the owner freed the slots. Fresh full, staged and current-source verification
still own final completion; these diagnostic observations do not substitute for them.

## Historical checkpoint (2026-11-02; before final review)

This is the evidence record for Issue 69, not a separate audit task. The owner authorized continuing
through opening the pull request, targeting `feat/speed-up-quality-gate`. The work branch is
`feat/issue-69`; the fixed base and current HEAD are `2bf5824375a5cb643ab35690be7428a12becc07b`,
which includes merged Issue 70 PR #158 and Issue 71 PR #164. The Issue 69 candidate is staged and
uncommitted. Before this checkpoint update, its index tree was
`4ffa1b497c0817574bb10563d2fa2ec916a56add`; the final reviewed tree will be recorded after this
evidence update. No Issue 69 commit or PR exists yet.

The last focused run after integrating operation ownership with Issue 71's native tool-cache
workspace passed: `issue69-native-cache-integration-focused-20261101-a`, 103 passed and 6 skipped in
15.31 seconds. Current-candidate `doctor` passed: `issue69-doctor-stage4-a`, ready in 0.918 seconds.
The default full orchestration remains incomplete, and fresh Standards and Spec reviews are still
required. Full runs recorded in historical sections below used the pre-Issue-71 base and do not
verify this candidate.

The closest comparable measurement runs the 36 test modules unchanged between base
`2bf5824375a5cb643ab35690be7428a12becc07b` and the candidate, with the same runtime and the pytest
cache provider disabled. The command selected `tests` and ignored the nine changed test modules.
`issue69-common-base-stage4-a` completed in 1070.14 seconds with 929 passed, 20 skipped, and two
failures; `issue69-common-candidate-stage4-a` completed in 1220.91 seconds with the same totals and
the same failures: `test_prompt_contract_orders_supplementals_before_the_staged_gate` and
`test_manifestless_full_scope_prompt_contract_keeps_ordinary_suite_discovery` in
`tests/test_runner_contract.py`. This single pair shows the candidate 150.77 seconds (14.1%) slower;
it does not establish stable performance. Pytest's cache was disabled, but OS/native cache state and
host contention were not controlled. These common-module runs are not a full Quality Gate baseline.

The current default full orchestration was attempted three times, with no accepted full verdict.
`issue69-full-final-stage4-a` returned an inconsistent error after `manifest-preflight`: the command
record says `validate` exited 1 in 0.688 seconds, while the summary says the wrapper failed parsing
supplemental-test logic and did not capture preflight or run Gate. `issue69-full-final-stage4-b`
recorded green preflight in 0.609 seconds, then stopped on the host's
`attribution UNCHECKED` (`mutation_not_confirmed`) notice; no supplemental stage or Gate ran. The
owner authorized ignoring `UNCHECKED`, but that attempt still provides no Gate result.
`issue69-full-final-stage4-c` recorded green preflight in 0.675 seconds and no supplemental tests;
the staged Gate remained queued for a local run slot, with no exit or duration and blockers
`process_state_unknown` and `incomplete_execution`. Do not rerun while that process state remains
unknown; no current-candidate full Gate result is accepted.

Earlier full-base attempts `issue69-base-full-20261101-b` and `-c` were unusable: the first found a
missing runtime; the next found a 213-character provider release-test workspace path against a
180-character limit and collected no tests. Attempts `-d`, `-e`, and `-f` ended with
`process_state_unknown`; no full baseline Gate result is accepted.

The prior Standards review found no standards violations on its earlier frozen diff
(`46F2F8D1B066E7F2E8415A94ADCA5AF9BE6447506615E0C247F3B8120AD6C012`). The prior Spec review on
the older base `236411ed8448b0a6e50b4aa46e279dcfba086bc3` found four gaps: physically bounded
preparation/cleanup, the fallback-workspace owner check, a comparable current baseline, and exact
repair-loop evidence. The current implementation now routes native-cache staging and lease release
through the operation-owned cleanup phase and includes the fallback-root correction. A paired
common-test measurement is recorded below; the full baseline Gate remains incomplete. The isolated
current-source repair loop is recorded in the continuation section; both reviews must be repeated. The owner
authorized reviewers to see the complete `quality_gate/secrets.py` diff.

Five untracked fixture temporary roots remain outside the candidate:
`tests/fixtures/{missing-test-path,multi-component,no-python,python-tests,valid}/temp`. Each contained
only its one-byte `.quality-gate.lock`; earlier recursive and explicit-file deletion attempts were
blocked before launch. Do not bypass that control or stage those artifacts. The owner also authorized
ignoring only the commit hook's `UNCHECKED` result; other hook results remain binding. The test-runner
aggregate budget was 25 minutes at handoff. The owner subsequently approved 35 minutes, as recorded
below; immutable release pins, CI selection and edit-hook timing contracts remain unchanged.

## Historical execution checkpoint (pre-Issue-71; superseded)

The owner authorized implementation through opening the Issue 69 pull request. At this checkpoint,
the changes are staged and uncommitted on `feat/issue-69`; no task commit or PR exists yet. Work
branch HEAD and fixed review base are `236411ed8448b0a6e50b4aa46e279dcfba086bc3`, the squash
integration of Issue 70 PR #158 on `feat/speed-up-quality-gate`. The current-source full check used
code/config tree `360b30a2dc8b07f0bd0011410931a3c8dfdce746`. The final candidate tree is captured in
the review identity rather than embedded in this self-referential audit file.

Five untracked fixture temporary roots remain: `tests/fixtures/{missing-test-path,multi-component,
no-python,python-tests,valid}/temp`. Each was inspected and contained only its one-byte
`.quality-gate.lock`, with no active run directory. Both verified recursive removal and the safer
explicit-file/empty-directory removal were rejected before process creation with `blocked by policy`.
No removal took place. Do not bypass the control or stage these unrelated artifacts; resolve through
an allowed cleanup route before finalizing if one is available.

The operation is integrated with the CLI-owned lifetime: local admission precedes the operation;
repository discovery, staged budget selection, workspace allocation, Issue 70 snapshot preparation,
readiness, policy/runtime cache validation, scanners and checks share one anchored monotonic work
deadline. Snapshot and workspace roots transfer to the operation registry. One bounded supervised
cleanup phase runs before the finalized report is emitted. Per-check status/outcome remains separate
from execution disposition; planned checks blocked by prerequisites are reported `not-run`, and
terminal pytest totals are retained when present. Candidate identity is derived from the captured
index fingerprint. No complete-result reuse or automatic rerun selection is introduced.

The source full Gate initially reported `python.component_1.pytest` unchecked at its 300-second
component limit, although the complete suite needed 931562 ms in the succeeding run. The manifest's
component `timeout_seconds` is now 1800 seconds, matching the repository's documented 30-minute
Python test budget. The repository Gate remains 2400 seconds. The owner permits increasing the
test-runner aggregate budget if an observed run needs it; the configured 25-minute aggregate remains
unchanged because the full runs below completed within it. Installed release pins remain unchanged.

The current-source full Gate passed on code/config tree
`360b30a2dc8b07f0bd0011410931a3c8dfdce746`; the configured default full orchestration also passed
on the immediately prior code/test tree `4db3eaa957f33e0cce1a331a8479994d66464f90`, before the
timeout change. After staging the prior audit update, both full routes passed on candidate tree
`51691ce53934f44b35f63a329751c0f76581656e`: `issue69-final-default-full-20261101-a` completed in
886687 ms with 16 passed and 2 not applicable; `issue69-final-source-full-20261101-a` completed in
962853 ms with 1137 passed and 59 skipped. This audit update changes the staged tree again. The
final full rerun IDs selected for the resulting candidate are
`issue69-final-default-full-20261101-b` and `issue69-final-source-full-20261101-b`.

The owner explicitly authorized giving reviewers the complete `quality_gate/secrets.py` diff for
the mandatory review. The commit hook remains enabled; its specifically `UNCHECKED` result may be
ignored only as the user directed, while every other hook result remains binding.

Earlier static/runner request `issue69-runner-checks-20261026-m` exited 0 in 166734 ms; its summary
reported mypy/Ruff passed and 49 pytest passes plus one skip. The skip's identity was not supplied.
Do not infer its cause or use this as a matched performance comparison.

The earlier `issue69-probes-red-20261026-o` request established the missing readiness transport,
but its scanner traceback was incomplete. The implementation added `operation.run_probe` and routed
readiness/scanner work through the original deadline. `issue69-probe-scanner-current-20261101-a`
passed 2 focused tests (15 deselected); its green result is scoped to those tests only.

An earlier probe request was blocked before launch by the host edit hook. Numeric comparisons in
the new test were named, and the scanner deadline test was moved into `tests/test_operation.py`.
`tests/test_secrets.py` was restored to its original contents; no credential fixture or hook policy
was weakened. The blocked request supplied no test evidence and its executor was retired.

Focused accepted evidence includes:

- `issue69-probe-scanner-current-20261101-a`: 2 passed, 15 deselected.
- `issue69-reporting-operation-focused-20261101-a`: 40 passed.
- `issue69-snapshot-cli-focused-20261101-a`: 19 passed, 135 deselected.
- `issue69-runner-recheck-20261101-a`: 42 passed, 1 skipped because Windows could not create
  directory symbolic links.
- `issue69-dispositions-recheck-20261101-c`: 2 passed, 42 deselected.
- `issue69-candidate-identity-green-20261101-a`: 1 passed, 44 deselected.
- `issue69-focused-post-format-20261101-a`: 211 passed, 30 skipped in 87870 ms.
- `issue69-focused-correction-20261101-a`: 216 passed, 30 skipped in 68080 ms, including the
  corrected `test_typing_optout.py` snapshot stub and runner/operation/reporting/CLI/workspace paths.
- `issue69-posix-grandchild-retry-20261101-b`: `GateOperation` timed out and reaped a real
  grandchild in a Python 3.11 Linux Docker container using the read-only checkout mount; 1 unittest
  passed in 1.005 seconds.

The repair-loop evidence and current-source per-check timings are recorded below. POSIX process-tree
verification passed in the Linux container evidence. Equivalent-work baseline data remain
unchecked. The commit hook remains enabled; the owner permits ignoring only its `UNCHECKED` result.
Test-runner configuration, policy pins and CI selection have not been changed.
