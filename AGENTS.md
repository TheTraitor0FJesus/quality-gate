# Quality Gate

Python 3.11+ CLI providing a shared quality contract for local Git hooks and GitHub CI.

## Project map

`quality_gate/` contains the CLI, runner, release machinery, and shared `policy/`; `tests/` contains tests; `templates/` contains consumer configuration; `.github/workflows/` contains CI; `docs/` contains project contracts.

### When answering codebase questions, designing, or changing code

Before a non-trivial codebase question, design task, or code change, read [the architecture map](docs/architecture.md). Use it to navigate, then verify relevant claims in code, the source of truth. In the same change, update the map when code makes one of its facts, links, or search routes inaccurate.

## When changing shared quality behavior

Before changing the runner, a shared tool policy, the manifest contract, or the reusable CI workflow, read [the quality policy change guide](docs/quality-policy-changes.md). Consumers select an immutable policy release and a pinned reusable-workflow SHA; activation requires an explicit consumer update, as described in [the agent reference](docs/agent-reference.md).

This repository's commit hook runs the pinned released Quality Gate. Source or policy changes also require a current-source `quality-gate check` before committing; see [verification details](docs/agent-reference.md#normal-operation).

## When planning, publishing, or starting issue work

Read [the local issue tracker procedure](docs/agents/issue-tracker.md) before this work.

When preparing a pull request, changing a public contract, or configuring release setup, read
[the local release contract](docs/release.md). Resolve the Git skill's release policy under
`CODEX_HOME/skills/git/references/release-policy.md`, falling back to `.codex` under the OS
user profile when `CODEX_HOME` is unset. The local contract owns this repository's executable
declaration format. Before changing shared publication, consumer wiring, or recovery, read
[the shared publisher interface](docs/publication.md); retain product builds in the local adapter.

## When maintaining the quality manifest

Read [the single manifest-maintenance guide](docs/agent-reference.md) before changing declared components, test paths, dependency inputs, required documents, workflows, waivers, or supplemental tests. After every `quality-gate.toml` edit, run `quality-gate validate`.
