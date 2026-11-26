# Local check scheduling for #200

The ordinary local staged check has one fixed admission barrier before Python
test commands. It uses the existing snapshot, check IDs, verdict and process
supervisor. There is no new manifest setting or result status.

## Result ownership

| Work | Owner | Before barrier | Failure behavior |
| --- | --- | --- | --- |
| Manifest and required documents, Git/workflow/document integrity, lessons and web budgets | `runner_snapshot.py` | Yes | Retain existing failed/unchecked/waived classification |
| Candidate secrets and applicable history checks | `runner_snapshot.py` | Yes | Remain mandatory; a finding never suppresses other available prerequisite checks |
| Biome checks | `runner_web.py` | Yes | Complete available web checks before Python tests |
| Component paths, test paths, runtime and deptry | `runner_python_checks.py` | Yes | Preserve existing missing-path, unavailable-runtime and waiver behavior |
| Ruff lint/format, including eligible evidence reuse, and mypy | `runner_python_checks.py`, `runner_python_tools.py` | Yes, for every component | Retain every completed prerequisite result |
| Pytest, coverage collection and coverage report | `runner_python_checks.py`, `runner_python_tools.py` | No | Admit the whole test stage only after prerequisites pass; then collect all available test-stage results |

The existing policy can explicitly make a check not applicable or waive a
finding. Those outcomes do not block admission. Failed or unchecked mandatory
prerequisites do. Unexecuted test commands use `unchecked` and `not-run`, including
report-only coverage commands that never started. This can change a local
lint/document failure from exit `1` to exit `2`. It cannot create exit `0`.
Command and aggregate deadlines and interruption retain their existing behavior.
They can prevent later work and must remain visible as incomplete verification.

Profiling, CI, audit and product verification retain the complete sequential
diagnostic pass. The optimization does not change the independent CI obligation
or the set of checks needed for a successful local commit.

## Concurrency decision

Keep sequential child execution. The smallest apparent overlap is mypy with
pytest. It would move mypy beyond the prerequisite barrier and let an already
invalid typing candidate start tests. Running two prerequisite commands in
threads also shares `GateOperation.current_stage`, its active-stage stack,
process-start observations and cleanup registrations.

A bounded two-thread probe of the existing stage owner entered two overlapping
process-required check stages and marked a process started in the first stage
only. Both stages were reported `executed`, including the stage whose process
never started. The supervisor intentionally treats its stage stack as nested
work. It is not an independent concurrent result owner. This probe establishes a
result-attribution hazard, not a failure in supported sequential execution.

Independent operation owners and isolated tool workspaces would require a larger
change to deadline, cancellation and result ownership. This ticket retains the
sequential fallback required by #195 and #200 rather than introducing that
scheduler. It claims no intra-Gate concurrency speedup. The four admitted Gates
per user and Ruff worker bounds are unchanged. Two independent Gate processes
remain supported and are included in the measurements below.

Concurrent component tests would also share the candidate working directory and
temporary HOME. Coverage commands share `COVERAGE_FILE`, and the report consumes
the collection command's output. Declared component paths alone do not establish
independence of test side effects or coverage data.

## Measurement method

Measurements use the public source CLI against alternate Git indexes under the
provider's `temp/`. The consumers' actual indexes and working files are untouched.
The baseline is an archive of post-#199 main `e698ab0`. The candidate changes the
Python orchestration described above. Both report engine version `4.9.0`, use
policy `v4.8.0`, and keep the prepared runtimes and native caches in place.
See [the #199 baseline](profile-baseline-199.md) for runtime keys and consumer
shapes. The runtime inventory was checked again before these samples.

For each consumer, populate an alternate index with `git read-tree HEAD`. The
green tree is the existing committed tree. For the failure tree, remove only
`README.md` from that alternate index with `git update-index --force-remove`.
Set `GIT_INDEX_FILE` only in the child environment. Invoke each engine with
`python -B -m quality_gate.cli --root <consumer> check --verbose`, from its own
source root. The measurement wrapper captures the rendered verdict as JSON and
redirects stdout/stderr to a log. No `--profile` is used in these comparisons,
because profiling intentionally disables early rejection.

| Consumer | Commit | Green staged tree | Missing-README staged tree |
| --- | --- | --- | --- |
| ARGUS_WEB | `79f21bc848ff2c601b23055ee6aa7dbfc32b2a6d` | `38bc5e8fe8afb9d7613b71af7dcc68e4e852ccb9` | `7d8594a7057927aef12a537ee180b8cb4e51abb6` |
| ARGUS_TG | `6261ffce944556fcd8e0e0e9844d008e291322ab` | `57a3ae7873c8b977301bf5257fa4c69a77de521b` | `be70f20deb31c4c49c1ed448350ef8261130df8f` |

Two single-Gate repetitions alternate engine order. The contention samples start
WEB and TG together in separate processes with independent snapshots and result
files. Times include CLI startup, admission, checks, report and cleanup. The OS
file cache, initial native-cache contents and other machine activity are not
controlled. The first baseline sample must not be treated as evidence of a green
speedup. Failing candidates intentionally perform less test work; the benefit is
earlier rejection, never a claim of faster equivalent full verification.

Pair wall times are observed when the wrapper joins each process, WEB first.
If TG finishes first, its wall value is an upper bound that includes waiting for
WEB. Use the independent per-operation active duration for that Gate and the
last join for the pair's total completion time.


## Observations

All times below are seconds except queue wait in milliseconds.

| Sample | Exit | Wall time | Queue wait, ms | Active including cleanup | Pytest outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| single-0-green-base | 0 | 28.734 | 0 | 28.093 | pytest totals: 504 passed |
| single-0-green-candidate | 0 | 20.046 | 0 | 19.750 | pytest totals: 504 passed |
| single-0-failure-base | 1 | 19.500 | 0 | 19.172 | pytest totals: 504 passed |
| single-0-failure-candidate | 2 | 9.188 | 0 | 8.875 | mandatory pre-test checks did not pass; test execution was not started |
| single-1-green-candidate | 0 | 19.953 | 0 | 19.656 | pytest totals: 504 passed |
| single-1-green-base | 0 | 19.062 | 0 | 18.766 | pytest totals: 504 passed |
| single-1-failure-candidate | 2 | 9.172 | 0 | 8.859 | mandatory pre-test checks did not pass; test execution was not started |
| single-1-failure-base | 1 | 19.500 | 0 | 19.186 | pytest totals: 504 passed |
| pair-green-base-ARGUS_WEB | 0 | 21.203 | 0 | 20.890 | pytest totals: 504 passed |
| pair-green-base-ARGUS_TG | 0 | 93.593 | 15 | 93.266 | pytest totals: 776 passed, 3 skipped, 29 warnings |
| pair-green-candidate-ARGUS_WEB | 0 | 22.156 | 0 | 21.811 | pytest totals: 504 passed |
| pair-green-candidate-ARGUS_TG | 0 | 89.297 | 0 | 88.983 | pytest totals: 776 passed, 3 skipped, 29 warnings |
| pair-failure-base-ARGUS_WEB | 1 | 29.516 | 0 | 29.203 | pytest totals: 504 passed |
| pair-failure-base-ARGUS_TG | 1 | 89.922 | 110 | 89.500 | pytest totals: 776 passed, 3 skipped, 29 warnings |
| pair-failure-candidate-ARGUS_WEB | 2 | 12.094 | 0 | 11.796 | mandatory pre-test checks did not pass; test execution was not started |
| pair-failure-candidate-ARGUS_TG | 2 | 12.109 | 0 | 9.561 | mandatory pre-test checks did not pass; test execution was not started |

The two WEB failure samples reject in about 9.18 s instead of 19.50 s. The
candidate deliberately omits the 504-test run after the staged document failure,
while completing all prerequisite checks. Green samples retain identical check
IDs, statuses and pytest totals, compared directly from the captured verdicts.
WEB executes 504 tests with no skips. TG executes 779 tests, with 776 passed and
3 skipped. Failure samples in the baseline still execute these tests; candidate
failure samples execute none and explicitly report the test command unchecked.

The first baseline green run is slower than its repeat. Cache conditions were
not reset, so this difference is not attributed to scheduling. The warm green
comparison is approximately 19.06 s baseline versus 19.95 s candidate. The
contended green pair also varies by consumer. These small samples establish no
green speedup or reliable green regression. There is only one contended pair per
engine and candidate shape, and there are no CPU or memory savings claims.

The measured source blobs are `15418f218643ddf33324af0a824a1a34625368cb` for
`runner.py` and `b07ba4b6cd6d9f9a8d5652e6a5c9537a2181f377` for
`runner_python_checks.py`. Subsequent source changes only apply the pinned
formatter. Windows measurements use the prepared Python 3.12.10 interpreters
recorded in #199. Linux execution is left to the existing platform checks and
is not claimed as a local measurement.
