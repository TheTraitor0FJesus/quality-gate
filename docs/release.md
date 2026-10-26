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
3. Replace `.release/notes.md` with Russian user-visible changes and required adaptation for the
   candidate version only. Include accumulated unreleased changes selected for this candidate, but
   never copy or summarize notes from already published releases. Their release pages remain the
   history. Plain prose is sufficient; a repeated version, Impact field, and fixed compatibility
   headings are unnecessary.
4. Prepare exactly one `## Release` section using the shared contract. For internal-only work, use
   only `- No release: <Russian reason>` and keep the version unchanged relative to the PR base.
   Notes already present in the base do not request a release by themselves.
   The shared validator also recognizes an omitted section as no-release for a Dependabot bot PR
   only when the complete, stable changed-file list consists of workflow YAML files directly under
   `.github/workflows/`, including any previous paths for renames. Mixed or uncertain file lists
   still require an explicit decision.
5. Complete the current-source staged Quality Gate and required review. The read-only
   `.github/workflows/release-validation.yml` caller checks opened, synchronize, reopened, and edited
   PRs targeting `main`. Publication is authorized only by the owner's final default-branch merge.

After publication, update the provider's own `quality.policy_release` to the newly published release
in a separate no-release PR. This activates the new policy for subsequent provider checks without
making the release candidate depend on its own unpublished wheel.

## Product verification

The published v4.0.0 release changes normal `check` and `audit` output to summarize
passed statuses. Failed and unchecked statuses remain visible, and `--verbose` returns
the complete status map and safely collected findings. This is MAJOR relative to
published v3.2.0 because tools that parse individual success lines must use
`--verbose`. The checks, first `Quality Gate: <exit_code>` line, and exit-code
precedence stay the same.

The release also adds v4.0.0 policy support to the edit commands and prints the
release audit report in CI logs. The [provider v4.0.0 acceptance record](audits/provider-v4.0.0-acceptance.md)
records its immutable release evidence and the separate self-policy activation.

The v4.1.0 candidate adds optional `phase: write` and `phase: validate` requests to the working-tree
Ruff interface. Requests without a phase preserve the synchronous behavior. Phase requests require
engine v4.1.0 or newer; older exact policy pins and hosts that keep phase-less requests remain
compatible. Host activation stays separate. This is MINOR relative to published v4.0.0.

The v3.2.0 release added [read-only touched-file integrity](edit-integrity.md)
and retained the v3.1.0 typing interface, including its separately selected
background schema. This was MINOR relative to published v3.1.0. The
[integrity provider evidence](audits/edit-integrity-provider.md) records that
candidate's source verification; it does not certify host activation.

The v3.1.0 release added [whole-scope typing](edit-types.md), preserving schema 1
and adding the separately selected schema 2 background/cancellation envelope.
It also adds the default-true manifest typing opt-out to the new policy. This is
MINOR relative to published v3.0.0; older exact policy pins remain supported and
are not changed automatically. The [typing provider evidence](audits/edit-types-provider.md)
distinguishes source/parity preparation from immutable publication and host
acceptance. The normal Windows/Linux exact-source release builds remain required.
Installed active-turn delivery, Stop/drain and aggregate event profiles require
the published compatible engine and separate host activation.

The public [working-tree Ruff interface](edit-check.md) was introduced in v2.6.0. The v2.6.1
release repaired direct wheel execution through the native release launcher. The v2.6.2
release added transparent provider-owned reuse of complete unchanged-content results.
The v3.0.0 release added the read-only [secret interface](edit-secrets.md) and fixed
actual-secret fingerprints in the shared scanner transport. Persisted waivers for
the old redacted placeholder require explicit review and reissue, so this is MAJOR
relative to v2.6.2. Interface schemas and detector rules remain unchanged. Both edit
interfaces retain exact v2.4.2/v2.5.0/v2.6.0/v2.6.1/v2.6.2 policy compatibility and
support the published v3.0.0, v3.1.0, v3.2.0, and v4.0.0 policies.
The source policy pin stays on its current published release; activation uses the separate route above. Host
installation/activation and the initial v2.4.2 consumer's `edit-setup` remain separate post-publication
work; the provider acceptance record does not establish installed host-event acceptance.

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
platforms then run the complete repository test suite using the newly built release. Before local setup,
each product job syncs the platform archive for the source manifest's pinned policy into the same
isolated cache, preserving both the published baseline and the release candidate.
`quality_gate.temp_workspace` scopes check, parity, publisher, and release-process temporary files
under the checked repository's ignored `temp/` directory. A read-only checkout or candidate
snapshot uses a private hashed workspace per repository under the user-local system temporary
directory. Release cache staging and atomic replacement files remain beside their persistent
targets. Release verification defaults to this run workspace; the explicit `--workspace-parent`
option remains available when an operator needs a shorter path. On Windows, the release controller
measures the resolved final workspace path, including existing junctions and symlinks, and rejects
paths longer than 180 characters before artifact synchronization or audit. Its error reports the
actual length, limit, minimum shortening, and the option to shorten or move the worktree directory.
The provider's pytest collection preflight exercises the same nested temporary-workspace creation
for every release-test `tmp_path` before tests run. This limit applies to release workspaces only; it
is not a general checkout-length limit for consumers.

Provider verification budgets are nested: the Python test stage is limited to 20 minutes, the
complete Quality Gate to 23 minutes, and the reusable CI job to 25 minutes including bootstrap and
cleanup. The provider full-suite test runner keeps its independent aggregate 25-minute deadline.

The `Verify package` step and artifact upload must succeed for each platform before publication.
The artifact audit streams its redacted report to the job log, including when it fails.
Candidate compatibility tests exercise the current product policy as well as historical pins;
the source manifest's published pin alone cannot establish candidate-policy compatibility.
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

The shared publisher uses the caller's job-scoped `GITHUB_TOKEN` for GitHub Release API reads and
mutations. Only its `publish` call gets `Contents: write`; it also receives the read permissions
needed for pull requests, Actions artifacts, and GHCR readback. A separate reusable image writer
call is the only release-flow job with `Packages: write`. Its consumer call job has no additional
steps. Neither path uses `RELEASE_TOKEN`, a GHCR PAT, or another long-lived credential.

Recovery may fail closed when the release target changes a workflow file and GitHub requires
`Workflows: write`. That permission cannot be assigned to the built-in `GITHUB_TOKEN`; the original
candidate remains available for the [original-run publisher recovery](publication.md#historical-403-requiring-workflows-write).
For a private GHCR package owned by another repository, grant the caller repository access under
that package's **Manage Actions access** settings. This is a package ACL setting. Administrative
access and native GitHub immutable-release settings are optional publication conditions. Existing
consumer installation and cache integrity contracts remain separately owned.

The publisher creates or resumes an exact-source tag and draft, uploads missing matching files,
verifies draft readiness, and then publishes. It reads back source, tag, notes/receipt, both assets,
and digests. Conflicts fail without moving a tag or overwriting an asset. A completed matching
candidate is verified before any nondeterministic rebuild, including after Actions artifacts expire.

## Retry and provider handoff

Use an ordinary failed-jobs rerun for transient failures. Successful producers from an earlier
attempt of that same run remain eligible; a later failed producer prevents publication.
For repaired tooling, dispatch `release.yml` from the default branch with `pr_number` set to the
original merged PR only when no earlier image write left a GHCR version tag without a trusted draft
receipt. For the historical 403 case, rerun only the failed publisher job in the original run after
the exact missing Git tag is created; do not dispatch a new run or repeat the image write. Follow the
envelope retention, lookup, and refusal rules in [the recovery contract](publication.md). The
product source remains the original merged SHA.

The provider self-hosts all three reusable entry points through same-repository references. Record the
actual `job.workflow_sha` after the final owner merge. That immutable source is consumer revision R
only after the real release, both platform assets, and readback pass. Consumers pin all three
reusable entry points to that same full SHA; their policy/runtime versions change only through their own
explicit rollout. A ticket PR or feature merge alone is not a completed provider handoff.

The provider's `quality-gate.toml` stays on the latest published policy until the candidate's policy
asset is published. After publication, advance the self-policy pin and remove any temporary provider
workflow waivers used to validate the candidate in a separate no-release PR.
Update the active provider pin assertion in `tests/test_ci_workflow.py` and waiver assertion
in `tests/test_integrity.py` in the same candidate. Select the accepted published policy in
`tests/fixtures/release-parity/quality-gate.toml` as well, so platform parity exercises that
policy. Preserve the comparator's expected outcomes and the non-gating parity waiver. This
activation and its fixture update keep the published helper R and product version unchanged.
Do not make the provider depend on an unpublished policy asset.

For an existing qualified release, verify its complete retained receipt, exact-source platform
verification jobs, tag, notes, helper R, and both downloadable ZIP byte digests before recording
acceptance. Reuse the existing release when only activation, fixtures, tests, or instructions
change. Record affected qualification reruns and consumer handoff in a provider acceptance audit
under `docs/audits/`, as in [v2.3.0 acceptance](audits/provider-v2.3.0-acceptance.md); an open
correction PR remains pending owner integration. The shared
[caller contract](publication.md#entry-points-and-ownership) also defines Brain's checks-only role.

## Verification and delivery boundaries

`tests/test_publication*.py` covers shared decisions, provenance, archive/image/package evidence,
retry, conflicts, and completed readback through the public boundary. `tests/test_release_metadata.py`
retains projection and timeout checks. Existing adapter, distribution, installation, audit, and
failure-injection tests remain the product evidence; test doubles never replace the actual platform
acceptance in the final release workflow.

The release preserves the schema 2 CLI, schema 2 hook/runtime contract, platform ZIP delivery, consumer
activation, rollback, cache retention, and backup responsibilities. Publishing the provider does
not automatically change consumer workflow pins or deploy services.
