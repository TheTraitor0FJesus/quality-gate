# Issue 69: operation reporting and cleanup research

This record preserves implementation decisions and verification evidence for [Issue 69](https://github.com/TheTraitor0FJesus/quality-gate/issues/69),
under [specification 68](https://github.com/TheTraitor0FJesus/quality-gate/issues/68). It makes no
speedup claim: the paired unchanged-test run had identical red outcomes, and its single candidate
sample was slower. Paths below are relative to the Quality Gate repository root.

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

## Historical baseline and candidate evidence before Issue 71

The measurements in this section were collected before Issue 71 was merged. They are retained for
research history and do not verify or benchmark the current Issue 69 candidate.

- Historical research and baseline source: `1a2cf132fe80a3e959c12c387131d7881f439c75`.
- Current integration base: `236411ed8448b0a6e50b4aa46e279dcfba086bc3` (`feat/speed-up-quality-gate`).
- Current working branch HEAD before the Issue 69 task commit: `236411ed8448b0a6e50b4aa46e279dcfba086bc3`.
- Code/config tree used by the current-source full check before this audit update:
  `360b30a2dc8b07f0bd0011410931a3c8dfdce746`.
- Baseline index tree: `1da161d76b80980cceed0e38c3134f78c8f2d20e`; the worktree and index were
  clean before dispatch and unchanged after completion.
- Work branch: `feat/issue-69`; the Issue 70 content is integrated in parent PR #158.
- Historical baseline environment: Windows 11 Pro, build `10.0.26100`, 64-bit; AMD Ryzen 9 3900X,
  12 cores / 24 logical processors; Python `3.12.10`; selected immutable policy `v4.2.0`.
- Current final source run selected immutable policy `v4.5.0`; do not compare it directly with the
  historical v4.2.0 baseline.
- Before measurement, the checkout-specific runtime was missing. Explicit `setup` completed and
  `doctor` reported ready. Setup cost is outside the measured Gate invocation.
- The configured 25-minute aggregate executor budget includes its 15-second emergency cleanup
  reserve. The owner permits increasing it if observed execution requires more time; no test-runner
  configuration change has been made.

One current-source baseline completed on 2026-10-26:

```text
run_id: issue69-baseline-source-20261026-a
scope: full
snapshot: working-tree
command: python -B -m quality_gate --root <resolved repository root> check --verbose
command stage: tests
exit_code: 0
duration_ms: 572087
timed_out: false
observed results: 14 passed; 4 not_applicable
executor blockers: none
```

The configured executor uses `tests` for a full request with an explicit command. Here that command
runs the complete staged Gate; the working tree matched the staged candidate. The root placeholder
above replaces the request's absolute checkout argument without changing its command semantics.
The native `test-runner` role used Luna/low with the current executor instructions. T3 delegation
failed before returning a task ID with `provider_unavailable` / workspace-routing discovery timeout;
that failure launched no Gate process. The native executor launched the measured command once.

The returned run/repository/scope/snapshot and command-stage identities matched the request. Its
completion record had an integer exit code and monotonic duration. This accepts the completed Gate
result, not a matched performance comparison.

Collected test, skip, error and per-check timing counts are unavailable in this historical baseline
response. The baseline implementation discarded successful pytest output before reporting, so even
`--verbose` could not recover its totals. The Issue 69 candidate now retains bounded pytest terminal
outcome totals when present, but this does not retroactively make the baseline equivalent for a
performance comparison. Do not infer missing totals or claim equivalent completed workloads.

Policy artifacts and the runtime were prepared before the run. Ruff/mypy cache directories point
to the per-run temporary workspace in `runner._safe_environment`; these are not retained cross-run
native caches. This source has no complete-result reuse in full Gate. Other feature work was active
on the same machine; admission wait, load and individual stage costs were not measured separately.
There is one sample and no measured variation or speedup. A speed comparison requires at least
three alternating baseline/candidate pairs with equivalent completed work, errors/skips, cache
conditions and environment identities.

## Failing-mypy, repair, and full-pass scenario

The available sequence demonstrates the repair loop without using a partial run as completion
evidence. The pre-edit baseline is not workload-equivalent, so these timings describe this sequence
only and do not support a speedup claim.

1. **Full red:** `issue69-current-full-20261101-a` used `scope: full`,
   `snapshot: working-tree`, and the default manifest orchestration on tree
   `4db3eaa957f33e0cce1a331a8479994d66464f90`. Preflight exited 0 in 187 ms; the staged Gate
   exited 1 in 894796 ms. It reported 12 passed, 4 failed, and 2 not applicable. The failed checks
   were `python.component_1.format`, `python.component_1.mypy`, `python.component_1.pytest`, and
   `python.component_1.ruff`. Its pytest stage collected 1136 passes, 1 failure, and 59 skips in
   854010 ms. The test failure was
   `tests/test_typing_optout.py::test_full_gate_reports_optout_and_keeps_other_python_checks`:
   its snapshot stub rejected the new operation kwargs. Mypy reported two incompatible result
   return types and five uninferred check lambdas; format found two lines in `cli.py`, and Ruff
   found three overlong strings.
2. **Focused correction:** `issue69-focused-correction-20261101-a` used the complete affected CLI,
   runner, operation, reporting, workspace, hooks, admission, and typing-optout test files. It
   passed 216 tests and skipped 30 in 68080 ms. This check was diagnostic evidence only, not a
   replacement for the full Gate.
3. **Intermediate default full pass:** `issue69-current-full-correction-20261101-a` passed the
   staged Gate with 16 passed and 2 not applicable in 910077 ms, before the manifest timeout was
   raised. It confirmed the code corrections; it did not replace the later current-source full pass.
4. **Budget recovery:** `issue69-source-full-after-fix-20261101-a` reported that the current-source
   pytest command hit its manifest component timeout of 300 seconds. Its operation output named
   `python.component_1.pytest`, elapsed 300016 ms, and an operation elapsed 313296 ms; the outer
   runner record reported 164100 ms, so that run's wall-time records conflict and are excluded
   from performance comparisons. The suite's completed 854010 ms stage established that 300
   seconds was insufficient. The manifest's Python component limit was changed to 1800 seconds,
   the documented 30-minute Python-stage budget. The 25-minute test-runner aggregate and 2400-
   second Gate budget were left unchanged.
5. **Current-source full pass:** after `validate`, `setup`, and `doctor` restored the missing local
   runtime, `issue69-source-full-after-runtime-20261101-a` passed on tree
   `360b30a2dc8b07f0bd0011410931a3c8dfdce746`, with `scope: full`, `snapshot: working-tree`, and
   `python -B -m quality_gate --root <repository> check --verbose`. The runner observed 1137
   passed and 59 skipped in 1057000 ms. The emitted operation record reported 963203 ms total,
   including 959250 ms in `candidate.checks` and 328 ms for its one bounded cleanup phase. Per-check
   results were:

   | Check ID | Scope | Outcome | Elapsed |
   | --- | --- | --- | ---: |
   | `python.component_1.deptry` | `quality_gate; dependencies=pyproject.toml` | passed | 531 ms |
   | `python.component_1.format` | `quality_gate` | passed | 108 ms |
   | `python.component_1.mypy` | `quality_gate` | passed | 4218 ms |
   | `python.component_1.pytest` | `quality_gate; tests=tests` | passed, 1137 passed / 59 skipped | 931562 ms |
   | `python.component_1.ruff` | `quality_gate` | passed | 578 ms |

   Policy lookup was a verified cache hit; the component runtime was a validated hit. The Gate
   reported 16 passed checks and 2 not applicable. This run measured a complete current-source
   workload, but it is not comparable with the incomplete 2026-10-26 baseline above.

## Original research findings at baseline 1a2cf (historical)

1. `quality_gate/contracts.py::CheckResult` carries outcome/findings; `Verdict.exit_code` owns
   `unchecked` before `failed` before success. Add telemetry at this existing interface without
   changing check IDs, status meanings or precedence. Timing never establishes result eligibility.
2. `quality_gate/reporting.py` preserves every failed/unchecked status in compact output and all
   observed statuses in verbose output. `quality_gate/ci_parity.py::RESULT_LINE` parses the existing
   `check_id: status - ...` prefix. Preserve these lines and their redaction.
3. `runner._execute_commands` appends an ID before checking remaining time. That list alone does
   not establish that a process started. It also discards successful non-coverage output. Track
   actual disposition and retain bounded workload totals rather than inferring either fact.
4. `runner._check_snapshot` starts the Gate deadline after repository checks, preparation and secret
   checks. `runner._run_snapshot` constructs the candidate earlier. Start one monotonic work clock
   before construction; every stage uses its remaining budget and its applicable shorter cap.
5. Snapshot/workspace context managers own synchronous deletion. Their failures can replace the
   accumulated verdict. Keep completed findings outside teardown, append the infrastructure blocker,
   and emit the final report after all operation-owned cleanup.
6. `runner._run_bounded_subprocess` kills the immediate child and has per-child one-second waits.
   `quality_gate/edit_process.py` already has Windows suspended Job ownership and POSIX process-group
   supervision with bounded streams. Reuse or extract that mechanism without changing edit-hook
   timing contracts; do not create a second check implementation.
7. `quality_gate/hooks.py::_commit_gate_timeout` covers Gate plus local admission wait, without a
   distinct operation cleanup reserve. The reusable CI job has a 50-minute limit. Size affected
   outer limits against the actual work/queue/cleanup contract while retaining the owner's present
   25-minute executor decision.

## Reporting decision

Extend the existing text report and typed verdicts. Keep current status lines, then add bounded,
redacted metadata for operation/profile, candidate and checked scope, stage, monotonic elapsed
duration, disposition (`executed`, `reused`, `not-run`) and reason. A failed check can be executed;
failure is an outcome, not a disposition. Show preparation, readiness, lookup/validation and cleanup
separately. A reused result keeps its original execution metadata separate from the current lookup.
Native tool-cache execution still counts as executed; cache availability does not prove a hit.

Use existing focused commands for repair diagnostics over the real complete affected scope. For
this repository, the Gate mypy invocation targets the complete declared `quality_gate` scope with
the selected runtime and released mypy configuration. Working-tree diagnostics, including
`edit-types` where applicable, never certify the staged full Gate. Record exact commands, scopes,
outcomes and elapsed times for the failing-mypy / repeated-fix / final-full-pass scenario.

No new JSON CLI format, diagnostic selector, automatic invalidation inference, dependency graph or
complete-result reuse implementation is proposed in Issue 69. Current per-check output can locate a
failure, but cannot establish a safe diagnostic scope or preservation of earlier evidence by name
alone. Evidence eligibility, later profile/result protocols and host orchestration retain their
existing owners. This is the existing-report extension authorized by Issue 69. It introduces no
new output format or diagnostic orchestration responsibility requiring a separate owner decision.

## Cleanup ownership and Issue 70 handoff

There was no shared operation cleanup enforcement at the historical research source. Synchronous
`snapshot._remove_snapshot` calls `shutil.rmtree`; checking elapsed time afterward detects an
overrun but does not bound it. Neither the runner's per-command waits nor the host executor's
emergency reserve is an inner operation cleanup owner.

Issue 69 owns one operation registry and one physically bounded cleanup phase, with supervised
process-tree termination. All registered operation-owned roots share that phase and allowance.
Keep completed red results when cleanup fails or is incomplete. Trusted-root validation and
scheduling belong to Issue 69. Do not silently renew cleanup time for each registered root.

Issue 70 is merged through PR #158. Its snapshot seam is present in the current parent and is
integrated by Issue 69 through this public interface:

```python
candidate_snapshot(
    repository=".",
    *,
    timeout_seconds=None,
    max_blob_size_mib=None,
    deadline: float | None = None,
    register_cleanup: Callable[[Path], None] | None = None,
)
```

`deadline` is the operation's original absolute `time.monotonic()` work deadline, propagated through
preparation, Git batches and materialization. It is not renewed and does not establish a cleanup
bound by itself. `register_cleanup` transfers the complete allocated staging root exactly once:
normal final teardown, including body/index-assertion failure, or construction failure after
allocation. No allocation means no callback. Registration also runs after work-deadline expiry.
The callback registers only; it does not delete, start a process or obtain a new budget. With the
callback supplied, local deletion is replaced entirely. Registration failure becomes `SnapshotError`
chained from its cause, retaining the original construction/body error where possible, without
fallback synchronous deletion. `None` preserves existing local synchronous deletion for standalone
callers. Issue 70 adds no cleanup worker or cleanup budget. Issue 69's operation owns transferred
roots and applies its separate bounded cleanup phase.

The merged parent seam owns snapshot allocation, deadline-aware Git/materialization and one-time
cleanup registration. Issue 69 consumes that public contract without changing `snapshot.py`.

Issue 71 owns persistent native Ruff/mypy caches and was merged through PR #164 into the current
parent. Issue 69 consumes that cache interface without conflating native cache execution with
complete-result reuse. When a Gate operation is active, native source staging is prepared under its
original deadline, the staged source root is registered with the operation, and the cache lease is
held until the shared bounded cleanup phase finishes. The standalone cache API retains its existing
synchronous behavior. The focused evidence for this integration is listed in the current checkpoint.

## Acceptance coverage

- Reporting: covered by `tests/test_reporting.py` for status/outcome separation, synthetic reused
  metadata, bounded redaction and pytest totals. The operation and per-check timings currently
  recorded above belong to the pre-Issue-71 candidate and are historical. Current-candidate full Gate
  timing remains pending.
- Deadline and disposition: focused preparation, readiness, scanner, nested-command, missing-path
  and snapshot tests pass on the Windows executor; command caps remain nested under the operation
  deadline. The Linux container process-tree probe also passed.
- Cleanup: Windows child/grandchild termination, shared root cleanup and failure retention have
  focused tests. `issue69-posix-grandchild-retry-20261101-b` additionally verified POSIX process-tree
  termination with a real grandchild in Linux/Python 3.11; its single unittest passed.
- Retained findings: `test_deadline_keeps_completed_failed_finding_and_marks_later_check_not_run`
  and `test_cleanup_failure_retains_completed_red_findings` cover later timeout/cleanup failure.
- Workload evidence: the historical repair sequence is superseded by the isolated current-source
  scenario below. The paired 36-module workload above is complete but red on both revisions;
  the full baseline Gate is still unavailable. Current-provider full verification remains pending;
  the isolated consumer scenario does not replace it. Make no speedup claim.
- Cross-platform evidence: Windows child/grandchild cleanup is covered by the focused suite. POSIX
  process-tree termination passed in the Linux/Python 3.11 Docker probe recorded above; the
  executor checkout was mounted read-only, and the test exercised the current `GateOperation` code.
- Contracts and navigation: `contracts.py`, `docs/agent-reference.md` and `docs/architecture.md`
  now describe the operation. Test-runner configuration and edit-hook contracts remain unchanged.
- The five untracked fixture `temp/` roots remain outside the candidate; their removal was blocked by
  the local file control. Independent CI is outside the owner's stop-at-PR boundary.

## Continuation verification (2026-11-02)

The previous unknown Gate process is no longer live in this worktree. A separately running Gate
in the main checkout was preserved. No previous incomplete full run is accepted as completion.
The owner authorized normalizing whitespace in the two existing prompt-contract tests, preserving
their phrase/order assertions and the test-runner configuration. Before the correction,
`issue69-runner-contract-probe-20261102-a` returned 4 passed and 2 failed in 2531 ms; afterward,
`issue69-runner-contract-fixed-20261102-a` returned 6 passed, exit 0, in 1984 ms.

The delegated repair scenario used Luna/high and current provider source while the main candidate
was frozen at index tree `00f19d76231d16f60b7fb004c4342613ea015ba0`. The SHA-256 of
`quality_gate/operation.py` was `57062233dbac4232c3805fea4b2b704e8711cc2637014d0dda8af5113fff1b70`.
All consumer files, copied immutable policy v4.5.0, runtime and diagnostic cache were isolated under
the repository's root `temp/`. No global policy/runtime cache or test-runner configuration changed.

The fixture declares typed component `app`, containing two Python source files, and `tests`,
containing one test. Its two initial functions return values incompatible with their annotations.
From the fixture root, every diagnostic executes the prepared Python with
`-m mypy --config-file <isolated-policy>/quality_gate/policy/mypy.ini app`, with
`MYPY_CACHE_DIR` pointing to the same scenario-local diagnostic cache. The full Gate executes
the current-source CLI with `--root <fixture> check --verbose` and the isolated cache selection.

| Run ID suffix (prefix `issue69-repair-loop-`) | Actual scope | Outcome | Command elapsed |
| --- | --- | --- | ---: |
| `initial-gate-20261102-c` | Full staged consumer Gate | red; mypy 2 return-type errors, pytest 1 passed | 4502 ms |
| `mypy-initial-20261102-d` | Complete typed component `app` | red; 2 errors, 2 source files checked | 2700 ms |
| `mypy-partial-20261102-a` | Complete typed component `app` | red; 1 error, 2 source files checked | 2200 ms |
| `mypy-final-20261102-a` | Complete typed component `app` | green; 2 source files checked | 522 ms |
| `final-gate-logged-20261102-b` | Full corrected staged consumer Gate | green; pytest 1 passed | 4064 ms |

The focused commands execute mypy each time and are diagnostic evidence only, even though their
explicit-command executor envelopes use `scope.kind = full`. They do not establish staged Gate
completion or preserve obligations automatically. The final consumer tree is
`d0a0dcf3f25240403859008a4ad81de876fa3ff0`; its full Gate reports candidate
`git-index-sha256:c15c703c4b82d9a90b24a8322483b6c3a0e6ef033ddcd15d71dba41c606413ac`,
operation `645e1293f0c440f183c96e732a77b052`, elapsed 3109 ms, `complete=true`, and cleanup 234 ms.

The final report records `python.component_1.format` (312 ms), `.mypy` (421 ms), `.pytest`
(328 ms, 1 passed), and `.ruff` (437 ms), all passed with disposition `executed` and component
scope `app` (`app; tests=tests` for pytest). Required documents, Git hygiene, workflow,
documentation, lessons and candidate secrets checks also passed. Coverage, dependency hygiene
without dependency inputs, and unrequested history scanning were explicitly `not_applicable`
with disposition `not-run`. Policy lookup/validation reports a verified hit in 125 ms; the
runtime reports a validated hit in 0 ms. Native tool-cache telemetry is `not-measured`;
neither a tool-internal cache hit nor complete-result reuse is inferred.

The first isolated runtime could not load mypyc DLLs at long Windows paths; observed extension
paths reached 260–263 characters. The same runtime files copied into shorter `temp/c69` passed
`mypy --version` in 798 ms, supporting the path-length diagnosis. Fixture ACL, workflow and
formatting preparation errors were resolved before the accepted final run. The executor's earlier
4800 ms green summary is invalidated: a logged run on that same earlier fixture tree instead
reported a format failure. Only the corrected final run has a retained raw report agreeing with
exit 0. Raw records, the report and parsed check/stage observations remain under
`temp/issue69-repair-loop/`; the attempt to remove its empty `logs` directory was blocked by
the local file control and was not bypassed. These are scenario costs, not a speed comparison.

The provider's default full run `issue69-provider-default-full-20261102-e` conclusively exited 1
on tree `a2ad2724e2a6b1b53e72c9d198fc6520b2ff1a1a`: preflight passed in 482 ms, zero
supplemental stages were declared, and the staged Gate finished in 1289500 ms. It reported
13 passed, 3 failed and 2 not applicable checks; pytest reported 1179 passed, 65 skipped and
one failed test. Ruff identified four policy violations and mypy identified three typing errors
in the Issue 71 integration. These are observed red results, not incomplete process evidence.

The failed native-source ownership test used a Windows path limit overridden only in its parent
process. Its supervised staging child correctly retained the real limit and selected a private
fallback in the deeper full-Gate sandbox. The test now allocates its cache directly under the
pytest session's temporary root, retaining the real child limit and checking actual ownership.
The correction also uses the existing identity-field count, narrows validated tool names,
separates operation-owned source lifetime from standalone cleanup, and types the snapshot with
the existing `CandidateSnapshot`. Command error attribution and deferred lease release retain
their original behavior. Fresh focused/static and full evidence are required after this batch.

`issue69-native-cache-corrections-focused-20261102-b` passed after the correction: the selected
Ruff checks, source formatting and complete-component mypy were green; the native-cache, operation
and runner modules had 103 passed and 6 skipped tests in a deliberately nested temporary sandbox.
The command completed in 30338 ms. This explicit-command run is focused diagnostic evidence,
not a full-provider verdict. The final default orchestration and current-source Gate results
belong to the frozen candidate including this evidence update and are recorded separately by
the implementation workflow.

The next default full run, `issue69-provider-default-full-20261102-f`, completed in 1466543 ms
after green preflight (2234 ms). Ruff, formatting and mypy no longer failed. Pytest reported
1177 passed, 65 skipped and three failures in `tests/test_edit_process.py`: cancellation, orphan
completion and bounded overflow observed `deadline` instead of their intended outcomes. Complete
tracebacks were not retained (`traceback_truncated`), so their exact originating stack locations
remain unverified. This is a red run, not an aggregate timeout. The owner explicitly authorized
raising the local test-runner envelope from 1500000 to 2100000 ms (35 minutes), retaining its
single 15000 ms cleanup reserve. Only those two budget literals changed in the external runner
configuration. The manifest's 30-minute Python test limit and 40-minute Gate limit are unchanged.

## Cleanup diagnosis after the owner-approved pause

The owner explicitly authorized diagnosis and correction of the two cleanup blockers. Source run
`issue69-provider-current-source-20261102-g` had 1179 passed, 65 skipped and one release test failure.
The release test expected an audit failure, but temporary-workspace deletion raised Windows error
145 instead. The final operation also reported incomplete cleanup at its original 15-second bound.
The retained raw report is `temp/issue69-diagnosis/source-g-original.stdout.log`.

A minimal workspace with one file and a real delete-sharing reader reproduced error 145.
Removing the reader from otherwise identical setup passed. The permanent regression
`tests/test_temp_workspace.py::test_workspace_cleanup_unlinks_a_file_with_a_delete_sharing_reader`
failed before correction (`issue69-cleanup-regression-red-20261102-l`) and passed afterward
(`issue69-cleanup-regression-green-20261102-m`). The reader remains valid after the owned name
is removed. Windows legacy disposition waits for all readers to close; POSIX disposition removes
the name when the deletion handle closes. See the [Microsoft deletion contract](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/ns-ntddk-_file_disposition_information_ex).
Removal still uses the same identity-checked handle. Unsupported API/filesystem errors retain
legacy behavior, while permission failures do not fall back. A separate regression covers both.

The second blocker is workload sizing, independently reproduced without open readers. On this
Windows/Python 3.12 host, the 40000-file operation probe exhausted its old cleanup allowance and
left 6310 entries. Profiling three complete 1000-file workspaces took 500, 484 and 515 ms under
cProfile, including repeated WinAPI binding setup and ABI class construction. Those immutable
objects are now initialized once; ownership, identity and attribute observations are never cached.
The controlled flat 40000-file probe then completed under the original allowance, but the actual
deep full-run workspace still exceeded it. This rules out treating that small flat probe as proof
that the real workload fits. The initial 7431-file inventory used ignore filtering and undercounted
the retained tree; the next probe counted 44587 files before partial recovery.

An instrumented replay of the remaining real workspace removed over 38000 files and 11000
directories with steady progress, completing in 28905 ms of the operation's own monotonic clock
when given one diagnostic 120-second allowance. The workspace was removed and its operation was
complete. The executor's reported outer duration was shorter than that inner duration, so its
timing/completion record was rejected and is not formal green verification. The retained progress
and filesystem observations support sizing; fresh authoritative checks still own verification.

The operation now reserves one bounded 120-second cleanup phase for all roots. Existing hook
budget propagation includes the same constant; the 50-minute CI envelope covers 40 minutes of
Gate work plus cleanup and bootstrap. The owner-selected local 35-minute envelope remains the
effective aggregate cap, with its distinct 15-second emergency process-termination reserve.
Edit-hook work deadlines are unchanged. The synthetic probes are functional deadline checks,
not matched-work speed comparisons: an incomplete baseline and a complete result cannot establish
a speedup. The original full scenario must still pass during final verification.

`issue69-cleanup-focused-validation-20261102-s` completed green after the correction: selected
Ruff checks, formatting and complete-component mypy passed, with 61 passed and 29 skipped workspace
and release tests. The command completed in 21973 ms. A preceding wider focused run exercised
native-cache, operation, runner, process-supervision and hook paths with 193 passed and 40 skipped
tests, but its aggregate was red on a Ruff finding corrected before run `-s`. Neither focused
result replaces the required final full-provider checks.

The current-source POSIX probe `issue69-posix-current-source-20261102-t` also passed: one unittest
in the existing Python 3.11 Linux image imported the exact read-only source and verified that an
operation timeout terminates its real child/grandchild process group. Its command exited 0 in
2598 ms. Network access was disabled; its writable temporary data was bound to the repository's
root `temp/`. This refreshes platform evidence after the Windows ABI classes moved to module scope.

## Full verification and review corrections

Candidate tree `5ec3a82e069e965a266e1289fac9c38a03166fbb` passed the current-source full run
`issue69-current-source-full-20261103-m`: 1187 passed and 65 skipped tests, exit 0, with a
1053780 ms observed command duration. The operation reported 1053437 ms total, including
70766 ms cleanup within its single 120-second allowance, and `complete=true`. The exact source
import was checked before invoking the existing CLI. The separate staged Gate request
`issue69-staged-gate-20261103-o` also exited 0, with 16 passed checks and two not applicable,
in 1062333 ms. Both commands used a zero queue-wait budget. Doctor subsequently reported ready.
These are correctness results for that candidate, not a controlled performance comparison.

An earlier full run had one wheel edit-check exceed its separate 10000 ms work deadline. The
owner approved diagnosis without increasing that deadline. Three replays of the retained exact
wheel and cache, six fresh/reused CLI invocations in total, did not reproduce it; the wheel
SHA-256 was `710dc3333651daa83c29c01ced932f4e8a85e59f2f655a24b98ac7e0d6389132`.
This does not establish a causal fix or prove absence. No edit-hook deadline was raised.

Stage-exit regression tests also exposed a completed failed result being replaced when the
deadline expired immediately afterward. Completed results are now retained before the
post-stage deadline check. A blocking operation timeout remains separate from an already
failed tool result; status precedence and check IDs are preserved.

The initial Standards and Spec reviews accepted six corrections. Snapshot removal and captured
index identity now have public interfaces; the existing result-cache capacity/eviction comment
is retained. Native-cache selection caps lock waits by remaining operation time, and runs
pruning, namespace preparation and source staging through the existing bounded child supervisor.
The parent retains the relevant locks while those children work. Ownership, namespace validation,
eviction and private fallback rules remain with the native-cache provider. Selection telemetry
reports its measured cost, persistent/private storage and bounded fallback reason, without
claiming tool-internal hits. Preparation helpers cannot mark the checked tool as executed.
An expired logical check remains `not-run` when its body never starts.

Seven targeted regressions failed before those corrections in
`issue69-review-regressions-red-20261103-q`. The subsequent correction run
`issue69-review-corrections-focused-20261103-r` passed Ruff, formatting and complete-component
mypy, with 245 passed and 39 skipped tests in 76.57 seconds of pytest time. It covered operation,
runner, native cache, snapshot, reporting, workspace and affected release paths. The measured
pytest command duration was 77219 ms. This focused result is diagnostic evidence; the changed
shared behavior requires fresh full/current-source and staged-Gate evidence before review.
