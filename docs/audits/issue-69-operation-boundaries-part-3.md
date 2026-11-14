# Issue 69: operation evidence, part 3

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
