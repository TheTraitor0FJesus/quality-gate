# quality-gate

Shared, versioned quality checks for local Git hooks and GitHub CI.

`quality-gate.toml` describes project components. The runner owns the Ruff, mypy, pytest, temporary-directory, and failure-reporting policy.

## Commands

- `quality-gate validate` validates a repository manifest.
- `quality-gate install-dependencies` installs development dependencies declared by a manifest.
- `quality-gate check --changed` checks staged Python changes for a local commit.
- `quality-gate check` checks every declared component for CI.
