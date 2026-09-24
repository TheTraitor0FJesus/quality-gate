# Quality Gate release procedure

Paths on this page are relative to the repository root. The [shared publisher contract](publication.md)
owns the PR declaration, reusable interface, evidence schema, publication states, and recovery.
This page owns Quality Gate's product integration. Resolve the Git skill's preparation policy
under `CODEX_HOME/skills/git/references/release-policy.md`, or `.codex` under the OS user profile.
For this repository, use the executable declaration format in the shared publisher contract.

## Prepare a candidate

1. Read live stable releases and classify all accumulated unreleased product changes. A feature-target
   ticket PR carries impact forward; the final default-target PR prepares the publication candidate.
2. Set `.release/version.toml` to one normal next major, minor, or patch version. Update every product
   projection declared in `.release/publisher.toml` in the same candidate. Keep the provider's source
   `quality-gate.toml` on the latest published policy while preparing the candidate; the canonical
   template continues to project the target product release. The authority contains exactly one
   top-level version string.
3. Write Russian user-visible changes and required adaptation in `.release/notes.md`. Plain prose is
   sufficient; a repeated version, Impact field, and fixed compatibility headings are unnecessary.
4. Prepare exactly one `## Release` section using the shared contract. For internal-only work, use
   only `- No release: <Russian reason>` and keep the version unchanged relative to the PR base.
   Existing notes remain valid historical content and cannot request a release by themselves.
5. Complete the current-source staged Quality Gate and required review. The read-only
   `.github/workflows/release-validation.yml` caller checks opened, synchronize, reopened, and edited
   PRs targeting `main`. Publication is authorized only by the owner's final default-branch merge.

After publication, update the provider's own `quality.policy_release` to the newly published release
in a separate no-release PR. This activates the new policy for subsequent provider checks without
making the release candidate depend on its own unpublished wheel.

## Product verification

`.github/workflows/release.yml` connects shared preparation, the existing Windows/Linux package
verification, and shared publication. Preparation resolves the final merged commit and retains its
original candidate envelope before either platform starts. No-release and already-complete outcomes
skip both builds. The published baseline and source version remain separate from the helper revision.

`scripts/release_build.py` runs each exact-source platform build. `scripts/release_adapter.py` owns
package construction, inventory, SHA-256 checks, wheel installation, and isolated runtime validation.
`scripts/release_metadata.py` supplies product metadata and bounded timeout helpers. The adapter calls
`quality_gate.release` for source/self-host validation, learned lessons, a disposable installation,
and audit. The isolated runtime setup and audit explicitly select the candidate policy release, while
the source manifest remains on the latest published policy until the post-publication pin update. Both
platforms then run the complete repository test suite using the newly built release.
`quality_gate.temp_workspace` scopes check, parity, publisher, and release-process temporary files
under the checked repository's ignored `temp/` directory. Release cache staging and atomic
replacement files remain beside their persistent targets. Release verification defaults to this
run workspace; the explicit `--workspace-parent` option remains available when an operator needs
a shorter path.

The `Verify package` step and artifact upload must succeed for each platform before publication.
The full-suite subprocess excludes the parent PR's history-selection variables so disposable
test repositories select their own history. The build process retains Actions context for evidence.

The build installs `.release/build-requirements.txt` and `.release/release-requirements.txt` with
hash verification and binary-only dependencies. The build-only backend lock remains outside the
package inventory. `.release/release-tools.toml` owns tool URLs/digests, operational timeouts, and
the existing self-host bootstrap policy selection. Gitleaks and policy-pinned Biome are verified
before packaging. `quality_gate/release_contract.py` remains the exact distribution inventory.

The product evidence contains the existing `quality-gate-v{version}-Linux.zip` and
`quality-gate-v{version}-Windows.zip` identities. The common publisher independently checks each
Actions run, producing attempt, job, required steps, upload receipt, artifact ID/digest, downloaded
content, and original candidate digest. Product code owns no second release parser or state machine.

## Publication and credentials

The shared `.github/workflows/release-publish.yml` serializes publication per repository without
cancelling an active publisher. It checks out the helper at the actual reusable job's revision and
runs `scripts/source_release.py`, a policy-free entry point to `quality_gate.publication_cli`.
The publisher imports no product adapter and installs no product dependencies.

Only the publication step receives the repository-owned `RELEASE_TOKEN`, through the shared
publisher's release API channel. It authorizes release-tag and GitHub Release mutations and needs
`Contents: write`; creating a tag containing workflow changes may also require `Workflows: write`.
GitHub metadata reads use the caller's short-lived `GITHUB_TOKEN`, with `Contents: read`,
`Pull requests: read`, and `Actions: read`. Shared publisher callers also grant `Packages: read` so
the same job token can authenticate GHCR readback through a temporary Docker configuration.

For a private GHCR package owned by another repository, grant the caller repository Read under that
package's **Manage Actions access** settings. This is a one-time package setting. Image-producing
jobs use their own job-scoped `GITHUB_TOKEN` with `Packages: write`; neither flow needs a separate
registry credential. Administrative access and native GitHub immutable-release settings are
optional publication conditions. Existing consumer installation and cache integrity contracts
remain separately owned.

The publisher creates or resumes an exact-source tag and draft, uploads missing matching files,
verifies draft readiness, and then publishes. It reads back source, tag, notes/receipt, both assets,
and digests. Conflicts fail without moving a tag or overwriting an asset. A completed matching
candidate is verified before any nondeterministic rebuild, including after Actions artifacts expire.

## Retry and provider handoff

Use an ordinary failed-jobs rerun for transient failures. Successful producers from an earlier
attempt of that same run remain eligible; a later failed producer prevents publication.
For repaired tooling, dispatch `release.yml` from the default branch with `pr_number` set to the
original merged PR. Follow the envelope retention, lookup, and refusal rules in
[the recovery contract](publication.md). The product source remains the original merged SHA.

The provider self-hosts both reusable entry points through same-repository references. Record the
actual `job.workflow_sha` after the final owner merge. That immutable source is consumer revision R
only after the real release, both platform assets, and readback pass. Consumers pin both reusable
entry points to that same full SHA; their policy/runtime versions change only through their own
explicit rollout. A ticket PR or feature merge alone is not a completed provider handoff.

## Verification and delivery boundaries

`tests/test_publication*.py` covers shared decisions, provenance, archive/image/package evidence,
retry, conflicts, and completed readback through the public boundary. `tests/test_release_metadata.py`
retains projection and timeout checks. Existing adapter, distribution, installation, audit, and
failure-injection tests remain the product evidence; test doubles never replace the actual platform
acceptance in the final release workflow.

The release preserves the schema 2 CLI, v2 hook/runtime contract, platform ZIP delivery, consumer
activation, rollback, cache retention, and backup responsibilities. Publishing the provider does
not automatically change consumer workflow pins or deploy services.
