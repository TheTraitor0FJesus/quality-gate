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

## Local Gate wait settings

Local full `check` and `audit` runs share a machine-wide queue. To change how long a run waits for an
available slot, set `QUALITY_GATE_QUEUE_TIMEOUT_SECONDS` to a whole number from 0 through 240; the
default is 120 seconds. Zero makes one immediate attempt. GitHub Actions skips this local queue and
uses its job deadline. To cap Ruff's worker threads during a full Gate run, set
`QUALITY_GATE_RUFF_THREADS` from 1 through 64; the default is 4. The standalone `format` command
does not use this thread setting.
