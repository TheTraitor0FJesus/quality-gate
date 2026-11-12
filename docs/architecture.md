# Architecture map

Use this map to find subsystem owners and follow cross-component routes. Code and executable configuration define behavior.

## Subsystems

- **CLI and command routing** — `quality_gate/cli.py` owns the public command parser and dispatch. It routes working-tree edit commands through `edit_cli.py`, and runtime inventory through `runtime_cli.py`; the latter inspects prepared environments via `RuntimeManager` without provisioning. Full checks and audits enter `runner.py`.
- **Manifest and results** — `contracts.py` parses and validates `quality-gate.toml`; `contract_models.py` defines manifest and verdict data, with `contract_inputs.py` and `contract_supplemental.py` handling input validation. `reporting.py` renders the resulting verdict. Keep behavior details at these code owners.
- **Gate execution** — `runner.py` coordinates checks, audits, formatting, and result collection. `runner_context.py`, `runner_python.py`, `runner_web.py`, `runner_dependency.py`, and `runner_subprocess.py` own execution context and tool-specific work. `integrity.py` owns repository and workflow checks; `lessons.py` owns lesson validation and audit/release readiness. `snapshot.py` supplies the staged Git candidate, `admission.py` governs concurrent local Gate runs, and `temp_workspace.py` owns temporary execution workspaces. These are separate boundaries: the runner composes them, while snapshot and workspace modules own their respective lifetimes.
- **Policy and Python environments** — `distribution.py` verifies and selects immutable policy releases. `launcher.py` prepares the selected policy and component environments; `runtime.py` fingerprints, inspects, and creates those environments. `runtime_cli.py` reports their current state without calling the creation path. `hooks.py` implements the native Git hook protocol, and `hook_setup.py` safely installs managed wrappers. See [runtime command behavior](runtimes.md) when working on runtime discovery.
- **Working-tree edit commands** — `edit_cli.py`, `edit_protocol.py`, `edit_process.py`, and `edit_worker.py` form the shared request, supervision, and worker boundary. `edit_check.py`, `edit_types.py`, `edit_secrets.py`, and `edit_integrity.py` own the mode implementations. See the [Ruff](edit-check.md), [typing](edit-types.md), [secret](edit-secrets.md), and [integrity](edit-integrity.md) interfaces for caller contracts. These commands do not use the full Gate's staged-candidate orchestration; trace a mode from `cli.py` to its worker implementation before changing it.
- **CI release verification** — `.github/workflows/quality.yml` is the reusable workflow boundary. `ci_release.py` and `release_contract.py` validate the downloaded policy release before use. `.github/workflows/parity.yml` runs the platform parity jobs, and `ci_parity.py` compares their results.
- **Publication** — `publication.py` owns publication decisions and validation; `publication_cli.py` is the local entry point, and `publication_flow.py` composes preparation, producer, image, and release operations from the focused `publication_*` modules. Read [the shared publication interface](publication.md) and [publication recovery](publication-recovery.md) before changing those boundaries.
- **Product release** — `release.py` handles local candidate verification; `.release/` holds the version, notes, and publisher declarations. Read [the release contract](release.md) when changing release preparation or validation.

## Principal flows

- **Local check or audit** — `cli.py` → `runner.py` → local admission (skipped in CI) → `temp_workspace.py` → `snapshot.py` → manifest, repository, lesson, policy, web, and Python checks → `reporting.py`. `audit` uses the same runner path and requests complete-history and lesson-readiness checks.
- **Setup and runtime inspection** — `setup` → `launcher.py` → selected immutable release in `distribution.py` → component environments in `runtime.py`. `runtimes --json` → `runtime_cli.py` → release verification and `RuntimeManager.inspect`; it is read-only.
- **Reusable CI check** — `quality.yml` verifies and selects the manifest's immutable policy release, prepares declared Python versions, then invokes the installed Gate against the candidate commit. Provider bootstrap setup uses `scripts/release_adapter.py`.
- **Shared publication** — publication entry point → `publication_flow.py` → focused preparation and publication operations. Provider activation and recovery contracts are documented in [the release guide](release.md) and [publication recovery](publication-recovery.md).

## Related guidance

- [Coding standards](coding-standards.md) for implementation conventions.
- [Quality policy changes](quality-policy-changes.md) for changes to shared quality behavior.
- [Lessons](lessons.md) for the lesson format and maintenance rules.
