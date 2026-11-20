# Issue 69: operation evidence, part 2

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
