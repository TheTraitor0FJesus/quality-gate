# Provider v4.2.0 acceptance and self-policy activation

This record covers the immutable provider release and the separate in-repository self-policy
activation candidate. It does not certify host edit-tool activation.

## Published release evidence

At `2026-10-26T09:25:20Z`, GitHub's direct latest-release API returned release ID `424009468`, tag
`v4.2.0`, `draft: false`, and `prerelease: false`. The tag-specific API record matched that ID and
tag. GitHub marked the release immutable and published it at `2026-10-26T08:38:35Z`.

The rendered `/releases/latest` page initially resolved to `v4.0.0`, while the live API returned
`v4.2.0`. The API record and tag-specific `v4.2.0` page agreed, so this activation used the API
response and recorded the stale page as a source discrepancy.

The tag resolves to source commit
`a6dcb9618cb22ecd8075a69fc8ff006168676993`. The retained publisher receipt identifies the same
source, version `4.2.0`, repository `TheTraitor0FJesus/quality-gate`, and release workflow
`.github/workflows/release.yml`. The source commit's `.release/version.toml` declares `4.2.0`, and
its `.release/publisher.toml` matches the local authority. The receipt records notes digest
`15f8b5fea5932e04c1903b4c358d85dad3a24b0ab5a1ec00ab4569a7e580dede`.

The successful [release workflow run 40291713021](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/40291713021)
reports `conclusion: success`. Its `Prepare release / Prepare candidate`, `Build linux`,
`Build windows`, and `Publish release / Publish candidate` jobs all succeeded. Both platform jobs
passed `Verify package`. The run's Actions `head_sha` is PR head
`91b28b0ae6f2fc4e6bf422f08ae094a217c44bb5`; the retained receipt and release tag bind the assets to
the final source commit above.

| Platform asset | Size in bytes | GitHub and receipt SHA-256 | Downloaded byte check |
| --- | ---: | --- | --- |
| `quality-gate-v4.2.0-Linux.zip` | 54,571,190 | `bdc7b458d03203e302c6c6d3791288f8a97a9507f4169139ff4e975df30fcbc8` | Matched |
| `quality-gate-v4.2.0-Windows.zip` | 50,593,067 | `dbe173099e3649188c4123cced0a1d6df125c807108f73e297df161aadaa100c` | Matched |

The GitHub asset digests matched the retained receipt. Both downloaded archives matched those
SHA-256 values and the GitHub-reported sizes.

## Provider self-policy activation

The candidate selects `v4.2.0` in `quality-gate.toml` and the matching release-parity fixture. It
updates the active policy assertion in `tests/test_ci_workflow.py` and the self-policy assertion and
name in `tests/test_integrity.py`. The single approved non-gating parity waiver and its reason remain
unchanged.

The product version projections were already aligned with `4.2.0`: `.release/version.toml`,
`pyproject.toml`, `quality_gate/__init__.py`, `templates/quality-gate.toml`, and
`quality_gate/release_contract.py`. The release's `quality_gate/edit_protocol.py` supports policy
`v4.2.0`; the local compatibility list already matched and needed no edit. Historical `v2.*` and
`v3.*` values remain in compatibility maps, fixtures, release history, and the emergency self-CI
fallback at `.release/release-tools.toml`.

The verified Windows asset was installed with
`quality-gate sync --source <verified-v4.2.0-Windows.zip> --version v4.2.0`. The local cache now
marks `v4.2.0` active and retains `v4.1.0` as the previous release. `quality-gate validate` passed,
`quality-gate setup` reported `ready - v4.2.0`, and `quality-gate doctor` reported `ready`.
`quality-gate version` reports the installed engine package, which remains `4.0.0`; it does not
report the selected policy release.

The configured native pre-commit route uses a 44-minute bridge timeout. The provider Gate budget is
40 minutes, and the current queue wait uses its 120-second default, so the bridge covers both.
No hook code change was needed for this activation.

## Pending integration

The provider self-policy candidate is unmerged. Its activation on the default branch and the final
main-branch Quality Gate remain pending.

The installed local edit-tool engine remains `4.0.0` and its supported-policy list ends at `v4.0.0`.
As a result, `quality-gate edit-setup --policy-release v4.2.0` returns `unchecked` even though the
published release supports `v4.2.0`. Updating that host-side engine is separate from this provider
repository and cache activation.

The full test-runner used the configured 25-minute aggregate budget at the user's direction,
although provider documentation specifies 50 minutes for a full local verification. Accepted run
`qg-provider-v4.2.0-full-20261026-rerun-5c0ee11d` returned `green` with no timeout: manifest
preflight exited 0 in 483 ms, the staged `quality-gate check` exited 0 in 674,120 ms, and the
manifest declared no supplemental tests. After recording that result, a second full verification
of the staged candidate also returned `green`: run
`qg-provider-v4.2.0-full-20261026-recovery-31f4c6aa` had a green manifest preflight (473 ms), a
green staged `quality-gate check` (681,100 ms), no supplemental declarations, and no timeout. A
timeout or incomplete result does not satisfy verification.
