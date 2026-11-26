# Quality Gate profile baseline for #199

These local Windows 11 Pro build 26100 measurements show where the shared Gate spent
time on two prepared consumers. Each consumer ran once with an empty native tool
cache and once with the cache populated by the first run.

- The profiling branch starts at `7d3b599abd67d2e4f47cfbf0a11579788c8e94a5`,
  the post-#207 main commit. Main pointed to `7e90db1c1107817cc1bb4eeafe4cf81978b61cd3`
  at measurement time.
- The measured engine came from source tree `e51a03c29cfd3aa2f55fc8a84185c4853088bf27`
  and reported version `4.9.0`. Both consumers selected immutable policy `v4.8.0`
  and an existing Python `3.12.10` runtime.
- ARGUS_TG runs Python checks and a bot test suite, including backup tests that
  create and exercise Git repositories. The consumer commit was
  `6261ffce944556fcd8e0e0e9844d008e291322ab`, with staged tree
  `57a3ae7873c8b977301bf5257fa4c69a77de521b`. The runtime key was
  `6a2a2c4f94fa3b7960a92e1bcbdb49748f670bf494c1a404572fa4ffac8215e5`,
  with pytest `9.1.1`.
- ARGUS_WEB runs checks for the `hub` Python component and a separate web domain
  with Biome. Its commit was `79f21bc848ff2c601b23055ee6aa7dbfc32b2a6d`, with
  staged tree `38bc5e8fe8afb9d7613b71af7dcc68e4e852ccb9`. The runtime key was
  `8ba57607e032406cf4e02520f435b4976575291cdb30018850bce1c38f414437`,
  with pytest `9.0.3`.

## Reproduce

From the measured provider source checkout, set `$consumer` to the consumer's
absolute path. Check its commit and staged tree. Use the source engine to list
runtimes and verify Python and pytest with the reported executable.

```powershell
git -C $consumer rev-parse HEAD
git -C $consumer write-tree
$inventory = python -B -m quality_gate.cli --root $consumer runtimes --json | ConvertFrom-Json
if ($LASTEXITCODE -or !$inventory.components[0].available) { throw 'Runtime unavailable' }
$runtimePython = $inventory.components[0].python_executable
& $runtimePython -B -c 'import platform, pytest; print(platform.python_version(), pytest.__version__)'
```

An ordinary manual run uses `python -B -m quality_gate.cli --root $consumer check --profile`.
The installed test-runner's `quality-gate` scope runs the staged `check` without
`--profile` and has no option for forwarding it. The repository owner authorized
direct manual profiling for these two consumers. The commands below capture the
source CLI output and exit code. The test-runner owns the separate provider
full-suite and staged regression evidence.

For the controlled pair, the wrapper redirects the native tool cache to the
provider's `temp/` while retaining the installed policy and runtime. A temporary
pytest hook records collection from the same pytest process that emits durations.

```powershell
$bench = Join-Path (Get-Location) ('temp/profile-samples-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $bench | Out-Null
$cache = Join-Path $bench 'cache'
$plugin = @'
import json
import os
from pathlib import Path

def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["QG_BENCH_COLLECTION"]).write_text(
        json.dumps({"collected": session.testscollected, "pytest_exit": int(exitstatus)}),
        encoding="utf-8",
    )
'@
[IO.File]::WriteAllText((Join-Path $bench 'qg_bench_collection.py'), $plugin)
$profile = @'
from dataclasses import replace
from pathlib import Path
import sys

from quality_gate import runner
from quality_gate.cli import main

native_cache = Path(sys.argv.pop(1))
plugin_dir = sys.argv.pop(1)
collection_path = sys.argv.pop(1)
prepare = runner.prepare
run = runner.run

def prepare_for_sample(*args, **kwargs):
    return replace(prepare(*args, **kwargs), cache_root=native_cache)

def run_for_sample(command, root, environment, **kwargs):
    if command[1:3] == ["-m", "pytest"]:
        environment = environment | {
            "PYTHONPATH": plugin_dir,
            "PYTEST_PLUGINS": "qg_bench_collection",
            "QG_BENCH_COLLECTION": collection_path,
        }
    return run(command, root, environment, **kwargs)

runner.prepare = prepare_for_sample
runner.run = run_for_sample
sys.argv[0] = "quality-gate"
raise SystemExit(main())
'@
function Invoke-ProfileSample([string] $name, [int] $expectedGateExit) {
    $log = Join-Path $bench ($name + '.log')
    $collection = Join-Path $bench ($name + '.collection.json')
    python -B -c $profile $cache $bench $collection --root $consumer check --profile *> $log
    if ($LASTEXITCODE -ne $expectedGateExit) { throw "Unexpected Gate exit for $name" }
    Get-Content -Raw -LiteralPath $collection
}
Invoke-ProfileSample 'cold' 0
if (!(Test-Path -LiteralPath (Join-Path $cache 'native-tools'))) {
    throw 'The cold run must populate the native cache'
}
Invoke-ProfileSample 'warm' 0
git -C $consumer write-tree
```

Run the pair serially, with the same staged tree and a new cache directory for
each consumer. Cold and warm refer to the persistent Ruff and mypy caches.
Both runs use prepared policy and runtime files. The isolated cache has no
edit-tool installation for working-tree Ruff evidence reuse, so both runs
execute the required Ruff checks. The operating-system file cache and other
machine activity are uncontrolled.

For the failure sample, point `GIT_INDEX_FILE` to a new file under the provider's
`temp/`, populate it from ARGUS_WEB's `HEAD` with `git read-tree HEAD`, then run
`git update-index --force-remove -- README.md` against that alternate index.
The staged tree should be `7d8594a7057927aef12a537ee180b8cb4e51abb6`. Run the
profile with the warm ARGUS_WEB cache using `Invoke-ProfileSample 'failure' 1`,
restore the previous `GIT_INDEX_FILE` value,
and remove the alternate index. The missing README and its inbound links produce
document findings. The profile runs all 504 tests and returns exit `1`.

## Observations

Times are seconds except for queue wait, which is in milliseconds. Total measures
elapsed time after admission, including cleanup. Stage time can include cache
lookup and preparation.

| Workload | Cache | Exit | Queue wait (ms) | Total | Snapshot | Policy/runtime | Ruff lint | Ruff format | mypy | deptry | pytest | Biome lint + format | Cleanup | Collected / passed / skipped |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| ARGUS_TG | Cold | 0 | 0 | 84.88 | 3.75 | 0.09 | 0.92 | 0.95 | 4.56 | 0.33 | 70.98 | N/A | 1.56 | 779 / 776 / 3 |
| ARGUS_TG | Warm | 0 | 0 | 80.50 | 2.95 | 0.09 | 0.83 | 0.77 | 0.95 | 0.33 | 71.38 | N/A | 1.54 | 779 / 776 / 3 |
| ARGUS_WEB | Cold | 0 | 16 | 22.61 | 3.38 | 0.11 | 0.88 | 0.83 | 3.45 | 0.30 | 10.69 | 0.11 | 0.91 | 504 / 504 / 0 |
| ARGUS_WEB | Warm | 0 | 0 | 18.25 | 2.95 | 0.09 | 0.81 | 0.75 | 1.00 | 0.28 | 9.47 | 0.12 | 0.91 | 504 / 504 / 0 |
| ARGUS_WEB, missing README | Warm | 1 | 0 | 18.22 | 2.95 | 0.11 | 0.78 | 0.75 | 0.91 | 0.30 | 9.36 | 0.11 | 1.11 | 504 / 504 / 0 |

Each sample has a Gate log with its timings and pytest outcomes and a companion
collection JSON from that invocation. All five pytest commands exited with
code `0` and reported zero failures and errors. Each profile ran one pytest
command. Neither pinned policy enabled the report-only coverage pass.
Deptry ran in all five samples. Its check outcome was `waived` in both
ARGUS_TG runs and `passed` in all three ARGUS_WEB runs.
These logs used a single limit across pytest phases. The current profile keeps a
separate limit for setup, call and teardown.

An earlier isolated run reached pytest but failed to load mypy's native DLL
from a relocated copy of the TG runtime. The table excludes that attempt. The
wrapper keeps both prepared interpreters at their original installed paths.
There is one controlled cold/warm pair per consumer, which is enough to locate
large stages. Repeated pairs would be needed to estimate expected savings.

Pytest was the longest stage in both projects. TG's cold run included a 4.94 s
backup test call and a 3.62 s test of backup path configuration. Snapshot creation
took about 3 to 4 s per run. Mypy took 4.56 s cold and 0.95 s warm in TG, and 3.45 s
cold and 1.00 s warm in WEB. Pytest took 0.40 s longer on the warm TG run and
1.22 s less on the warm WEB run, so the total time differences include variation
in the test runs.

For [#200](https://github.com/TheTraitor0FJesus/quality-gate/issues/200), the
failing sample gives the cost of continuing after a cheap document failure.
Use the same alternate tree and warm cache when comparing early rejection.
For [#201](https://github.com/TheTraitor0FJesus/quality-gate/issues/201), preserve
the collection and skip counts and measure shared orchestration separately
from consumer fixture and call time.
