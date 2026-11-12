# Quality Gate

Python 3.11+ CLI providing a shared quality contract for local Git hooks and GitHub CI.

## Project map

`quality_gate/` contains the CLI, runner, release machinery, and shared `policy/`; `tests/` contains tests; `templates/` contains consumer configuration; `.github/workflows/` contains CI; `docs/` contains project guidance and release contracts.

At the start of a task, from the Quality Gate repository root, run `quality-gate --help` once.
Before first using a command, read its `--help`. When applicable, locate existing Python
environments with `quality-gate --root <repository> runtimes --json`. Existing verification and
authorization rules still apply.

## Agent skills

### Quality contract

When adding, moving, or removing a Python component, its test directory, or its development requirements file, update `quality-gate.toml` in the same change. Keep shared Ruff and mypy policy and quality CI in Quality Gate; do not add project-local overrides or duplicate quality CI jobs.

### Architecture map maintenance

Update [the architecture map](docs/architecture.md) when code changes make its navigation facts inaccurate.

## When changing shared quality behavior

Before changing the runner, a shared tool policy, the manifest contract, or the reusable CI workflow, read [the quality policy change guide](docs/quality-policy-changes.md). Consumers select an immutable policy release and a pinned reusable-workflow SHA; activation requires an explicit consumer update, as described in [consumer setup](docs/consumer-setup.md).

This repository's commit hook runs the pinned released Quality Gate against staged changes. See
[operations guidance](docs/operations.md) for recovery and verification routes.

## Release and public contract changes

When preparing a pull request, changing a public contract, or configuring release setup, read
[the local release contract](docs/release.md). The local contract owns this repository's executable
declaration format. Before changing shared publication, consumer wiring, or recovery, read
[the shared publisher interface](docs/publication.md); retain product builds in the local adapter.

## Pull request and local release follow-through

- Track every pull request this agent opens or actively handles through merge or closure. Register it
  with the current T3 thread immediately, then use the available pull request watcher and respond to
  review comments, failing checks, or branch conflicts within the authorized scope. Resume watching
  after each update; do not poll manually. If event-based watching is unavailable, report that the
  pull request is not being monitored.
- After a Quality Gate pull request is merged and its immutable release is published, update this
  machine's local gate to that newest release and verify the active version. Do not update on pull
  request creation. Keep CI on its existing release unless the user asks to change it. Before
  changing a local selector, confirm that it is separate from the manifest and workflow pins used by
  CI; if no local-only selector exists, report the gap instead of changing shared pins.
