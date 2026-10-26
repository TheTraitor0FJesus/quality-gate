# Provider v4.1.0 acceptance and self-policy activation

This record covers the immutable provider release and its separate in-repository self-policy activation. It does not certify installed host event handling.

## Published release evidence

GitHub identifies `v4.1.0` as immutable, non-draft, and non-prerelease, published at `2026-10-25T22:44:52Z`. The tag and retained publisher receipt identify source commit `41b30c8063f4c6ef29e2ecc11e29ad9ab87cbd68`, merged as PR 126 at `2026-10-25T22:28:44Z`. The receipt records source-notes SHA-256 `da2d89751c08d42bf72f286b47881ba0d21305fdffe1842ab28e8c1e4eb2532f`.

The successful [release run 40240377948](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/40240377948) and its retained publisher receipt bind the release assets to that exact merged source. The `Prepare release / Prepare candidate`, `Build linux`, `Build windows`, and `Publish release / Publish candidate` jobs succeeded. Both platform builds passed `Verify package`; GitHub's release page and publisher receipt agree on the uploaded assets and SHA-256 digests.

| Platform asset | Size in bytes | GitHub and receipt SHA-256 |
| --- | ---: | --- |
| `quality-gate-v4.1.0-Linux.zip` | 54,565,694 | `1e856dd77758dd059da56dd09a7d3c3ce0ef2058c10fbf47d17a61a4979a31eb` |
| `quality-gate-v4.1.0-Windows.zip` | 50,587,474 | `ded0e9912f1403821715dbe84fc539dca4957cc42e3d396029b75d8783831570` |

The release adds optional `phase: write` and `phase: validate` requests to the working-tree Ruff interface. It is MINOR relative to published v4.0.0. The source package version, canonical template, and published assets are already part of the base `main`; this PR does not prepare or publish another product release.

## Provider self-policy activation

This separate no-release change selects `v4.1.0` in the provider manifest and release-parity fixture, updates the active CI and self-policy assertions, and retains only the approved non-gating parity waiver. The commit hook has no independent version pin: it reads the staged manifest and uses that exact cached policy release. The emergency CI bootstrap in `.release/release-tools.toml` remains a separate fallback and is not advanced.

The activation takes effect for `main` after the owner merges this no-release PR and the final-main Quality Gate passes. Host installation and real Codex event acceptance remain separate work.
