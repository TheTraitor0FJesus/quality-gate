# quality-gate

Shared, versioned quality checks for local Git hooks and GitHub CI.

`quality-gate.toml` is a schema 2 repository contract. It declares repository obligations,
Python components, immutable policy release identity, limits, and typed waivers. The runner
owns the Ruff, mypy, pytest, isolated-environment, timeout, and structured-reporting policy.

## Commands

- `quality-gate validate` validates a repository manifest.
- `quality-gate migrate` prints a read-only schema 1 migration proposal.
- `quality-gate check` checks the complete declared contract and returns a structured verdict.
- `quality-gate check --verbose` prints complete redacted diagnostics.

Schema 2 does not expose changed-only checking or generic dependency installation. Verification
is read-only and uses the exact repository root supplied by `--root` or discovered from Git.
