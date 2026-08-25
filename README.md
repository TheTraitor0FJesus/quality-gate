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
- `quality-gate sync --source <release-directory-or-zip>` installs an integrity-checked immutable policy release.
- `quality-gate setup` prepares the isolated runtime for every declared Python component.
- `quality-gate doctor` reports missing or stale policy and runtime prerequisites as unchecked.
- `quality-gate sync --rollback [VERSION]` selects a retained release without network access.
- `quality-gate sync --prune` previews old cache entries; add `--confirm` to perform explicit cleanup.

Schema 2 does not expose changed-only checking or generic dependency installation. Verification
is read-only and uses the exact repository root supplied by `--root` or discovered from Git.
`sync`, `setup`, rollback, and confirmed prune are explicit mutation boundaries. Release
installation verifies every declared artifact and external tool before atomic activation.
Consumer runtimes live below the user cache and are fingerprinted by the policy release, Python
version, component contract, and declared dependency inputs; a changed input creates a new
runtime instead of reusing an older one.
