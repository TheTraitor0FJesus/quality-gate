# quality-gate

Shared, versioned quality checks for local Git hooks and GitHub CI.

`quality-gate.toml` is a schema 2 repository contract. It declares repository obligations,
Python components, immutable policy release identity, limits, and typed waivers. The runner
owns the Ruff, mypy, pytest, report-only coverage, isolated-environment, timeout, and
structured-reporting policy.

## Commands

- `quality-gate validate` validates a repository manifest.
- `quality-gate migrate` prints a read-only schema 1 migration proposal.
- `quality-gate check` checks the complete declared contract and returns a structured verdict.
- `quality-gate check --verbose` prints complete redacted diagnostics.
- `quality-gate audit` runs every implemented domain, including a full reachable-history secret scan and lesson completion.
- `quality-gate audit --verbose` prints complete redacted audit diagnostics.
- `quality-gate check --base <ref> [--head <ref>]` adds a verified base-to-head history scan for CI.
- `quality-gate format <path>...` formats only the explicit Python paths with pinned Ruff; stage the changes and run `check` after formatting.
- `quality-gate sync --source <release-directory-or-zip>` installs an integrity-checked immutable policy release.
- `quality-gate setup` prepares the isolated runtime for every declared Python component.
- `quality-gate doctor` reports missing or stale policy and runtime prerequisites as unchecked.
- `quality-gate sync --rollback [VERSION]` selects a retained release without network access.
- `quality-gate sync --prune` previews old cache entries; add `--confirm` to perform explicit cleanup.

Schema 2 does not expose changed-only checking or generic dependency installation. Verification
is read-only and uses the exact repository root supplied by `--root` or discovered from Git.
`format`, `sync`, `setup`, rollback, and confirmed prune are explicit mutation boundaries. Release
installation verifies every declared artifact and external tool before atomic activation.
Consumer runtimes live below the user cache and are fingerprinted by the policy release, Python
version, component contract, and declared dependency inputs; a changed input creates a new
runtime instead of reusing an older one.
Secret checks use the integrity-checked Gitleaks binary declared by the selected policy release.
Candidate findings expose only a repository location and SHA-256 fingerprint; migration history
scanning is provided as a secret-domain seam for the repository audit command. Escaped-defect
lesson format and release learning rules are defined in [`docs/lessons.md`](docs/lessons.md).

## GitHub CI

`.github/workflows/quality.yml` is a reusable workflow and the self-check workflow for this
repository. It checks out the exact pull-request head with full history, reads the immutable
`quality.policy_release` from `quality-gate.toml`, verifies the release wheel checksum before
installation, synchronizes and prepares that release, then runs the same `quality-gate check`
used by the local gate. Pull requests pass their explicit base and head commit SHAs to the range
scan. A push to `main` repeats the gate and reports the private GitHub Free limitation: it detects
a direct default-branch push but cannot undo it.

Consumer workflows must call the reusable workflow by a full 40-character commit SHA. The
reusable workflow reads all declared component Python versions; an optional input can override
that list:

```yaml
jobs:
  quality-gate:
    uses: TheTraitor0FJesus/quality-gate/.github/workflows/quality.yml@<40-character-commit-sha>
    with:
      python-version: "3.12"
```

Dependabot checks GitHub Actions references weekly in `.github/dependabot.yml`. Policy and
workflow updates remain human merge decisions; no auto-merge rule is configured.
