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

The v5 engine runs quality checks only. `check`, `check --profile`, `audit` and the native
commit hook never execute pytest or Coverage. They retain dependency analysis of the declared
test paths and check runtime readiness. A successful Gate is evidence of code quality checks;
it does not establish that tests passed. Run behavior tests through the independent test runner.
The shared CI and release routes are described in [independent testing](testing.md).

Run `quality-gate check --profile` from the repository root after staging the candidate. Use
an engine that lists the flag in `quality-gate check --help` and the runtime selected by the
staged manifest. The report shows local admission wait separately from elapsed time after
admission. If admission fails, the profile shows the wait and indicates that verification
did not start. Elapsed time includes cleanup. The value `command_ms` measures time spent
supervising subprocess calls within a stage. Stage timings can overlap because some
stages contain others.

The profile reports the quality checks executed in this operation. The normal check and commit
hook omit detailed timings. Pytest outcome totals, test phase timings and Coverage reports belong
to the separate test execution result.
