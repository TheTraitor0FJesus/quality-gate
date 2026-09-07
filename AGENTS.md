## Architecture map

Before a non-trivial codebase question, design task, or code change, read [the architecture map](docs/architecture.md). Use it to navigate, then verify relevant claims in code, the source of truth. In the same change, update the map when code makes one of its facts, links, or search routes inaccurate.

## Shared quality policy

Before changing the runner, a shared tool policy, the manifest contract, or the reusable CI workflow, read [the quality policy change guide](docs/quality-policy-changes.md). A change pushed to `main` becomes the active quality behavior for every connected project.

## Agent skills

### Issue tracker

Project plans, specifications, and issues are tracked as local files. See `docs/agents/issue-tracker.md` before planning, publishing, or starting issue work.

### Quality contract

`docs/agent-reference.md` is the single manifest-maintenance guide. Before changing declared components, test paths, dependency inputs, required documents, workflows, waivers, or supplemental tests, read it. In the same change that adds, moves, or removes a Python component, its test directory, or its development requirements file, update `quality-gate.toml`. After every manifest edit, run `quality-gate validate`. Keep shared Ruff and mypy policy in `quality-gate`; do not add a project-local override or duplicate quality CI job.
