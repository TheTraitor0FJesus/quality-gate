# Quality policy changes

## Scope

This repository is the source of truth for shared Python quality checks.

- `quality_gate/policy/ruff.toml` defines shared Ruff policy.
- `quality_gate/policy/mypy.ini` defines shared mypy policy.
- `quality_gate/runner.py` defines which checks run and how project manifests apply them.
- `quality_gate/integrity.py` defines repository Git, workflow, and mechanical documentation checks.
- `quality_gate/lessons.py` defines the escaped-defect lesson format and release learning gate.
- `quality_gate/contracts.py`, `quality_gate/reporting.py`, and `quality_gate/migration.py` define the schema 2 manifest, verdict/reporting, waiver, and schema 1 migration contracts.
- `.github/workflows/quality.yml` is the reusable GitHub CI workflow.
- `quality-gate.toml` in each consumer repository is the schema 2 contract: repository obligations,
  component metadata, limits, defaults, policy release identity, and typed waivers.

The reusable workflow and local launcher must consume an immutable policy release. The distribution
layer owns artifact retrieval and checksums; the reusable workflow must derive its release URL from
the manifest and synchronize it before running the CLI gate.

The global Git hook currently uses the transition runtime at
`S:\GITHUB-REPOSITORIES\code_projects\quality-gate-v1-runtime`. Release and sync work must
replace this temporary routing with an explicit cached v2 release selection only at ticket 18.

During the migration, the global commit hook uses the v1 runtime at
`S:\GITHUB-REPOSITORIES\code_projects\quality-gate-v1-runtime`, including for this repository.
The v2 runner remains available for explicit development checks but must not become the commit
blocking path before ticket 18 completes. Ticket 18 owns the final hook switch to v2.

## Change procedure

1. Identify whether the change affects a shared policy, the runner, the manifest contract, or CI behavior.
2. Update the source of truth in this repository. Do not add a project-local override to weaken a shared rule.
3. Update focused tests when runner behavior or manifest handling changes.
4. Run the relevant tests and `quality-gate check` from this repository.
5. Inspect the diff for unintended changes to the shared policy or CI workflow.
6. Commit and push to `main` only after the checks pass.

## Project-specific behavior

Each project may declare Python component paths, existing test directories, and dependency files in `quality-gate.toml`.

Projects may keep pytest markers, test discovery, package metadata, and dependency configuration that describe their own code. They must not keep separate Ruff or mypy policies, or CI jobs that rerun the shared quality tools independently.

## Rollback

If a policy change blocks correct projects, revert the responsible `quality-gate` commit and push the revert to `main`. This restores the prior shared behavior for both the global hook and consumer CI.
