# Quality Gate

Python 3.11+ CLI providing a shared quality contract for local Git hooks and GitHub CI.

## Project map

`quality_gate/` contains the CLI, runner, release machinery, and shared `policy/`; `tests/` contains tests; `templates/` contains consumer configuration; `.github/workflows/` contains CI; `docs/` contains project contracts.

### When answering codebase questions, designing, or changing code

Before a non-trivial codebase question, design task, or code change, read [the architecture map](docs/architecture.md). Use it to navigate, then verify relevant claims in code, the source of truth. In the same change, update the map when code makes one of its facts, links, or search routes inaccurate.

## When changing shared quality behavior

Before changing the runner, a shared tool policy, the manifest contract, or the reusable CI workflow, read [the quality policy change guide](docs/quality-policy-changes.md). Consumers select an immutable policy release and a pinned reusable-workflow SHA; activation requires an explicit consumer update, as described in [the agent reference](docs/agent-reference.md).

This repository's commit hook runs the pinned released Quality Gate. Source or policy changes also require a current-source `quality-gate check` before committing; see [verification details](docs/agent-reference.md#normal-operation).

Before full-suite, audit or release verification, size test, Gate, runner, hook and CI timeout budgets for the operation, allowing outer budgets to cover inner work and cleanup; edit-hook deadlines retain their separate contract.

## When planning, publishing, or starting issue work

Read [the local issue tracker procedure](docs/agents/issue-tracker.md) before this work.

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
