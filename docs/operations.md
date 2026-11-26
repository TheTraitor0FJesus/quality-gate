# Quality Gate operations

This guide summarizes the regular CLI choices. Read each command's `--help` for its exact options.
The manifest and current immutable release determine which checks and tools are available.

## Choose a command

| Command | Use it for |
| --- | --- |
| `quality-gate validate` | Check the repository manifest after editing it. This does not prepare the selected release or component runtimes. |
| `quality-gate doctor` | Check that the manifest's selected policy release and declared component runtimes are ready. It inspects prerequisites without preparing them. |
| `quality-gate setup` | Prepare the selected policy and declared Python runtimes. Run it after selecting a release or changing Python components. |
| `quality-gate --root <repository> runtimes --json` | Inspect existing Python runtimes when the installed engine supports this command. See [Runtime inventory](runtimes.md). |
| `quality-gate check` | Check the exact staged candidate, as the normal local commit hook does. |
| `quality-gate check --profile` | Check the staged candidate and show timings for this run. |
| `quality-gate audit` | Run the complete onboarding audit, including all implemented domains, a full reachable-history secret scan, and lesson completion. |
| `quality-gate version` | Print the installed engine package version. It does not report the policy release selected in `quality-gate.toml`. |

Use `quality-gate sync --source <archive> --version <release>` to install a local immutable policy
release. The `--url` form downloads that exact release. `sync --rollback` changes local cache
selection; a consumer rollback must also restore its manifest policy release and reusable workflow
SHA. See [consumer setup and updates](consumer-setup.md).

## Interpret and recover an unavailable result

`unchecked` means verification could not complete. It is not a pass. Restore the missing or stale
prerequisite named in the result, then repeat the affected operation. Common examples are a
missing policy release, an unavailable component runtime, an invalid manifest, an unreadable
candidate, or exhausted resources. Do not replace unavailable verification with a waiver.

`check` operates on the staged candidate. If that candidate changes, the old result does not cover
the new candidate. Agent execution and authorization follow the global instructions.

Snapshot preparation limits staged blob data to 1 GiB across the candidate and its temporary
object copies. Exceeding this limit produces an unchecked result. The Gate removes the object
copies with the snapshot during its cleanup phase.

## Local Gate wait settings

Local full `check` and `audit` runs share a machine-wide queue. To change how long a run waits for an
available slot, set `QUALITY_GATE_QUEUE_TIMEOUT_SECONDS` to a whole number from 0 through 240; the
default is 120 seconds. Zero makes one immediate attempt. GitHub Actions skips this local queue and
uses its job deadline. To cap Ruff's worker threads during a full Gate run, set
`QUALITY_GATE_RUFF_THREADS` from 1 through 64; the default is 4. The standalone `format` command
does not use this thread setting.

## Profile a staged check

An ordinary local `check` completes repository, security and web checks, then
dependency hygiene, Ruff lint/format and mypy for every Python component before
starting any pytest or report-only coverage command. A blocking prerequisite
prevents those test commands from starting. All completed findings remain in the
report. Prevented tests use the existing `unchecked` status with a reason and
`not-run` timing disposition. Consequently, a failed prerequisite with applicable
unexecuted tests returns exit `2`, rather than exit `1` for findings alone.

Once the test stage starts, a component test failure does not stop the remaining
test commands. The shared deadline, interruption and process cleanup still apply.
The local four-Gate admission limit and sequential child execution are unchanged.
CI, `audit`, product verification and explicit `check --profile` retain the full
diagnostic pass after a prerequisite finding. No successful commit can omit an
applicable mandatory check.

Run `quality-gate check --profile` from the repository root after staging the candidate. Use
an engine that lists the flag in `quality-gate check --help` and the runtime selected by the
staged manifest. The report shows local admission wait separately from elapsed time after
admission. If admission fails, the profile shows the wait and indicates that verification
did not start. Elapsed time includes cleanup. The value `command_ms` measures time spent
supervising subprocess calls within a stage. Stage timings can overlap because some
stages contain others.

The profile shows up to ten of the slowest entries per phase (setup, call and teardown)
from the same pytest run. It labels them by test path and name with parameter values
removed. The ordinary report shows pytest's outcome totals when available. For a failed run, the
Gate masks secrets before limiting failure details. It includes captured output for
failed tests according to pytest's capture settings and gives the reason for pytest
usage errors and interruptions when available. The profile marks phase timings
unavailable when pytest cannot provide them. It shows report-only coverage failures
in the timing outcome while preserving the Gate verdict. The normal check and commit
hook omit detailed timings.
