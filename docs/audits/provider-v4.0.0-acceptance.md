# Provider v4.0.0 release acceptance

This record covers the immutable v4.0.0 provider release. Current provider self-policy activation is tracked in the v4.1.0 acceptance record. This audit does not certify installed host event handling.

## Published release evidence

GitHub identifies `v4.0.0` as an immutable, non-draft, non-prerelease release. The tag and retained publisher receipt identify source commit `dadd56ff25fa1f021160fff477b9ec5988108464`. The release came from merged PR 97 at `2026-10-25T07:12:54Z` and was published at `2026-10-25T07:27:09Z`. It was superseded as the latest stable release by `v4.1.0`, published on 2026-10-25T22:44:52Z; see [the v4.1.0 acceptance record](provider-v4.1.0-acceptance.md).

The successful [release run 40171936949](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/40171936949) verified the exact merged source. The `Prepare release / Prepare candidate`, `Build linux`, `Build windows`, and `Publish release / Publish candidate` jobs succeeded. Both platform jobs passed `Verify package` and uploaded their retained release evidence. The receipt binds provider revision R to the same source commit and records source-notes SHA-256 `02b4ff588a11111bb28edae0bed24c4cdc9019f9a38857e19bf562b18c580010`.

| Platform asset | Size in bytes | GitHub and receipt SHA-256 |
| --- | ---: | --- |
| `quality-gate-v4.0.0-Linux.zip` | 54,564,182 | `4944845a5c3e43c00b3adcb2ba6fa1c994bc661ab92c152b5b390bd7e4a773da` |
| `quality-gate-v4.0.0-Windows.zip` | 50,585,978 | `f5bd05cc90093b084dd2c1ce6ff86f997f8ae90c2f21be1ed274a709d7fdede3` |

The published release notes define v4.0.0 as MAJOR relative to published v3.2.0. Normal `check` and `audit` output summarizes passed results; `--verbose` retains the complete status map. The release also supports policy v4.0.0 in the edit commands and prints a safe release-audit report in CI.

## Provider self-policy activation

The planned separate activation to `v4.0.0` was superseded before integration by publication of `v4.1.0`. The current no-release activation selects the latest published `v4.1.0` policy in the provider manifest and parity fixture and updates the active CI and self-policy assertions. Product version projections and published assets remain unchanged by that activation. The commit hook has no independent version pin: it reads the staged manifest and uses that exact cached policy release. The emergency CI bootstrap in `.release/release-tools.toml` remains separate from normal selection and is not advanced by activation.

This audit records the v4.0.0 release evidence only. Current activation status and post-merge qualification are tracked in the v4.1.0 acceptance record.
