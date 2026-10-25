# Provider v4.0.0 acceptance and self-policy activation

This record covers the immutable provider release and its separate in-repository self-policy activation. It does not certify installed host event handling.

## Published release evidence

The latest stable release was checked on 2026-10-26. GitHub identifies `v4.0.0` as immutable, non-draft, and non-prerelease. The tag and retained publisher receipt identify source commit `dadd56ff25fa1f021160fff477b9ec5988108464`. The release came from merged PR 97 at `2026-10-25T07:12:54Z` and was published at `2026-10-25T07:27:09Z`.

The successful [release run 40171936949](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/40171936949) verified the exact merged source. The `Prepare release / Prepare candidate`, `Build linux`, `Build windows`, and `Publish release / Publish candidate` jobs succeeded. Both platform jobs passed `Verify package` and uploaded their retained release evidence. The receipt binds provider revision R to the same source commit and records source-notes SHA-256 `02b4ff588a11111bb28edae0bed24c4cdc9019f9a38857e19bf562b18c580010`.

| Platform asset | Size in bytes | GitHub and receipt SHA-256 |
| --- | ---: | --- |
| `quality-gate-v4.0.0-Linux.zip` | 54,564,182 | `4944845a5c3e43c00b3adcb2ba6fa1c994bc661ab92c152b5b390bd7e4a773da` |
| `quality-gate-v4.0.0-Windows.zip` | 50,585,978 | `f5bd05cc90093b084dd2c1ce6ff86f997f8ae90c2f21be1ed274a709d7fdede3` |

The published release notes define v4.0.0 as MAJOR relative to published v3.2.0. Normal `check` and `audit` output summarizes passed results; `--verbose` retains the complete status map. The release also supports policy v4.0.0 in the edit commands and prints a safe release-audit report in CI.

## Provider self-policy activation

The separate no-release activation selects `v4.0.0` in the provider manifest and parity fixture, updates the active CI and self-policy test assertions, and retains only the approved non-gating parity waiver. Product version projections and the published release assets remain unchanged. The commit hook has no independent version pin: it reads the staged manifest and uses that exact cached policy release. The emergency CI bootstrap in `.release/release-tools.toml` remains separate from normal selection and is not advanced by this activation.

This audit records the release evidence, not an owner-merged activation. The activation takes effect for `main` after its no-release change merges and the final-main Quality Gate passes. Any post-merge qualification result must be added only after it has run.
