# Supplemental performance and resource evidence

This document records observations for the
[test-proof specification](spec.md). Repository paths are relative to the Quality
Gate root. It distinguishes measurements, reproducible failures and proposals;
no production repair or speed-up is accepted by this evidence alone.

## Isolated baseline

On 2026-10-14, revision `5454af580629dd10484ca61281f7f5ab08b7fdd7`
on `feat/audit-test` had a clean tracked tree/index before profiling. The two
complete runs were serialized. Process inventory checks before and between runs
and during progress showed only the owned pytest and its test-created descendants.
No unrelated pytest or Quality Gate was observed. Code/index remained unchanged.

Environment: Windows, Python 3.12.10, pytest 9.1.1, pluggy 1.6.0. The direct
environment autoloaded anyio 4.14.0, pytest-cov 6.3.0 and typeguard 4.5.2.
Do not assume this environment is identical to the manifest-selected Gate runtime.

| Request | Pytest duration | Results | Exit |
| --- | --- | --- | --- |
| `audit-perf-isolated-20261014-02` | 303.01 s | 574 passed, 7 skipped, 581 collected | 0 |
| `audit-perf-isolated-20261014-03` | 301.46 s | 574 passed, 7 skipped, 581 collected | 0 |

These durations come from pytest's final summaries; XML retains per-test time.
The first command used `-v` and the second `-q` to retain useful streaming without
excessive tool output. All selection, cache, traceback, bytecode and duration
options were otherwise equivalent. The 0.51% difference is not an optimization:
the code did not change, and output modes were different.

| Group | First run | Second run |
| --- | --- | --- |
| `tests/test_contracts_cli.py` | 105.99 s | 107.66 s |
| `tests/test_cli_python_contract.py` | 55.86 s | 59.57 s |
| `tests/test_ci_workflow.py` | 36.33 s | 37.64 s |
| `tests/test_release.py` | 31.09 s | 28.88 s |
| `tests/test_native_git_hooks.py` | 24.01 s | 23.93 s |

Group totals sum JUnit test durations, including attributable setup/call/teardown.
The longest individual operations were:

| Operation | First run | Second run | Classification |
| --- | --- | --- | --- |
| `_release_gate` setup for `test_ci_and_local_cli_runs_match_on_a_release_backed_repository` | 20.87 s | 21.13 s | Immutable module-scoped infrastructure, already shared |
| Same test's CLI/parity call | 13.92 s | 14.75 s | Behavior under test |
| `test_real_git_pre_push_blocks_default_updates_and_allows_feature_pushes` call | 13.40 s | 14.27 s | Behavior under test |
| `test_check_enforces_default_web_file_and_total_boundaries` call | 10.44 s | 10.74 s | Behavior plus repeated release preparation; per-case split not measured |
| `test_check_excludes_oversized_web_assets_only_when_explicitly_configured` call | 9.46 s | 9.74 s | Behavior plus repeated release preparation; per-case split not measured |

Both runs passed existing active-process workspace synchronization. Neither normal
full run reported WinError 32/33. Both recreated five source-fixture `temp/` roots,
each containing only the one-byte `.quality-gate.lock`, confirming ticket 09.
Those run-owned directories were removed after verifying their targets/content.
No tracked source or test file was modified by profiling.

The exact seven skips were the same in both runs:

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

### Focused preparation attribution

Request `audit-cli-attribution-20261014-05` repeated the entire
`tests/test_contracts_cli.py` module under standard-library `cProfile`:
36 passed in 118.40 seconds. A command-local monotonic stopwatch recorded
120.164 seconds including interpreter/profiler startup and output capture;
the typed executor recorded 122.320 seconds for the surrounding shell command.
This is an instrumented diagnostic run, not a full-suite speed comparison.

| Parent-side cost | Calls | Cumulative duration |
| --- | --- | --- |
| `_run` helper, all arguments | 52 | 104.60 s |
| Direct child `subprocess.run` calls from `_run` | 52 | 70.95 s |
| Helper work outside those child calls | — | 33.65 s |
| `PolicyCache.sync` | 17 | 15.99 s |
| `verify_release` | 19 | 15.52 s |
| `Path.read_bytes` in the module profile | 37 | 14.94 s |
| `shutil.copy2` in the module profile | 68 | 2.34 s |

The helper-only remainder is calculated from its cumulative time minus its
direct subprocess caller cost in the profile. It includes preparation and
environment construction. Operation rows have nested costs and must not be added
to that remainder or to each other. The profile does not attribute child-process
internals or provide a maximum duration for each individual preparation.

The repeated scanner copy/hash/cache-sync boundary is a worthwhile candidate
for verified immutable reuse. Its 33.65-second measured remainder is not a promised
saving: preparation is still needed once, and mandatory per-command integrity
checks and mutable consumer state must remain. No sharing implementation or
before/after speed-up was tested here. The earlier attribution artifact showed
a 34.36-second helper remainder, but its executor's duration field was inconsistent
with pytest's summary; that response was rejected and replaced by this timed run.

Profiles and attribution inputs are retained under the same environment-resolved
artifact directory: `cli-setup-repeat.pstats`, `cli-attribution-repeat.{log,xml}`,
`cli-attribution-repeat-command.json` and `cli-cost-summary.json`. The selected
command was the same module's full pytest invocation through:

```powershell
python -B -m cProfile -o "$profileRoot/cli-setup-repeat.pstats" -m pytest tests/test_contracts_cli.py -p no:cacheprovider -q --tb=long -ra --durations=20 --junitxml="$profileRoot/cli-attribution-repeat.xml"
```

`profileRoot` is resolved as shown below; the actual command also saved output
and the stopwatch record. No project test/source or deadline was changed.

### Commands and artifacts

Logs/XML live under the directory resolved from `LOCALAPPDATA`:
`quality-gate/diagnostics/pytest-20261014/isolated-02.{log,xml}` and
`isolated-03.{log,xml}`. Run from the repository root:

```powershell
$profileRoot = Join-Path $env:LOCALAPPDATA 'quality-gate/diagnostics/pytest-20261014'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -u -m pytest tests -p no:cacheprovider -v --tb=long -ra --durations=30 --junitxml="$profileRoot/isolated-02.xml"
python -B -u -m pytest tests -p no:cacheprovider -q --tb=long -ra --durations=30 --junitxml="$profileRoot/isolated-03.xml"
```

The actual commands piped combined output through `Tee-Object` to the matching
log files and returned pytest's exit code. Follow the typed-runner workflow before
repeating these checks. The aborted request ending in `-01` has no accepted result
and is excluded from every timing table.

## Resource experiments

The following 2026-10-13 experiments used disposable roots and only owned
processes for the failure scenario. Logs/scripts remain under the directory
resolved from `LOCALAPPDATA`, `quality-gate/diagnostics/pytest-20261013/`.

1. `timeout_probe.py` / `timeout-lock-before.log`, request
   `timeout-lock-before-20261013-04`: start a parent which starts a child. The child
   enters the real `temporary_workspace` in a nested repository, records readiness
   and sleeps. Invoke real `_run_bounded_subprocess` with a five-second deadline.
   The immediate parent is killed, the child remains, and outer workspace removal
   raises WinError 32 on the nested `.active.lock`. Terminating the owned child
   permits deletion. Unrelated holders of that workspace are unnecessary.
2. `lock_probe.py` / `lock-before.log`, request `lock-before-20261013-03`: copy a
   repository while one real workspace is active, using `_build_wheel`'s existing
   exclusions. The read of `temp/<run>/.active.lock` fails with WinError 33.
   Workspace exit still cleans correctly. This is a separate source-copy defect,
   not evidence that copying caused the reported WinError 32.

`tests/test_runner.py::test_bounded_subprocess_kills_and_joins_after_timeout`
uses a fake process and BytesIO. It proves immediate kill/wait/join behavior,
but no descendant, inherited pipe or working-directory lifetime. Removing the
active lock or weakening stale-workspace protection would conceal the symptom
without repairing command ownership.

An unaccepted prototype added `taskkill /T /F`, a scratch-copy exclusion and three
regression cases. The focused log reports 50 passed, 1 skipped in 60.56 seconds,
but its typed-runner response was incomplete, so it is not accepted verification.
The complete current-source Gate returned exit 2 in a reported 241.200 seconds
with `candidate.snapshot` and temporary workspace cleanup errors on the candidate
directory; surviving pytest descendants were observed. Cleanup hid the underlying
check result. The precise additional failure cause was not established.
The user removed the prototype. Its patch is historical evidence, not a fix to
apply blindly or a current test/source change.

## Earlier noisy profiles and attribution

On predecessor revision `633c4c2`, direct full pytest reported 568 passed,
7 skipped in 349.38 and 338.58 seconds. Unrelated ARGUS checks were observed during
both attempts; neither establishes an isolated baseline. An additional Gate
attempt also overlapped another Quality Gate worktree.

`subprocess-before.json` records parent-interpreter `subprocess.run` invocations:
1009 Git calls totaling 98.84 seconds, 47 CLI calls totaling 104.13 seconds,
8 pip calls totaling 28.63 seconds and 11 venv calls totaling 7.63 seconds.
It does not trace every OS descendant, and invocation counts do not prove that
every attempted call created a process. Individual small-fixture `git add` calls
reached about seven seconds. These observations support further attribution,
not a claim that concurrency alone caused the duration.

The predecessor has six fewer test results than the current baseline. Revision,
output options and environment were not held constant between the noisy and
isolated profiles. Their difference cannot quantify a concurrency penalty or
prove a speed-up. The stable isolated pair instead establishes that the current
standalone suite itself exceeds 300 seconds slightly.

## Decisions supported by the evidence

- The resource failures justify tickets 10 and 11 independently of performance.
- `_run` repeats scanner copying, hashing and `PolicyCache.sync` for each
  check/audit. Focused attribution identifies 33.65 seconds outside its child
  commands. Verified immutable reuse is a supported optimization candidate;
  a safe sharing boundary and an actual speed-up still need implementation proof.
- Installed CLI setup is already module-scoped. Real native Git, package integrity
  and release-controller initialization remain valuable slow proof.
- A mandatory 240-second target has no supporting safe optimization. It would
  require about 63 seconds (21%) of reduction and is removed from acceptance.
- For the observed standalone suite, 300 seconds has no margin. A provisional
  360 seconds gives about 57 seconds (19%) over the slower sample. Two samples do
  not establish worst-case latency; measure the actual Gate command and preserve
  complete coverage before making a timeout change. No limit changed here.
- Ticket 01 already records historical canonical-path WinError 3 and extra skips
  under the Gate's isolated settings root. Those are distinct verification
  prerequisites, not established causes of the present standalone timing.
- Linux, live publication, separate Node-fixture execution and coverage were not
  newly validated by this profiling. Neither baseline is final post-repair Gate
  acceptance; completeness has priority over faster green output.
