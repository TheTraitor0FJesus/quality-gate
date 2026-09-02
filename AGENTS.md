## Architecture map

Before a non-trivial codebase question, design task, or code change, read [the architecture map](docs/architecture.md). Use it to navigate, then verify relevant claims in code, the source of truth. In the same change, update the map when code makes one of its facts, links, or search routes inaccurate.

## Shared quality policy

Before changing the runner, a shared tool policy, the manifest contract, or the reusable CI workflow, read [the quality policy change guide](docs/quality-policy-changes.md). A change pushed to `main` becomes the active quality behavior for every connected project.

## Agent skills

### Issue tracker

Project plans, specifications, and issues are tracked as local files. See `docs/agents/issue-tracker.md` before planning, publishing, or starting issue work.

### Quality contract

`quality-gate.toml` describes the repository components checked by the shared quality gate. In the same change that adds, moves, or removes a Python component, its test directory, or its development requirements file, update `quality-gate.toml` and run `quality-gate validate`. Keep shared Ruff and mypy policy in `quality-gate`; do not add a project-local override or duplicate quality CI job.
Before changing declared components, tests, dependency inputs, required documents, workflows, or waivers, read `S:\GITHUB-REPOSITORIES\code_projects\quality-gate\docs\agent-reference.md`; after changing the manifest, run `quality-gate validate`.
