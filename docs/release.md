# Quality Gate release procedure

This repository implements the shared owner-merged release policy described in the [shared release policy](C:/Users/Traitor/.codex/skills/git/references/release-policy.md). This page is the local contract; it records the Quality Gate adapter and points to the policy instead of redefining it.

## Version authority

`.release/version.toml` contains exactly one top-level `version` string in plain `MAJOR.MINOR.PATCH` form. It is the only product-version authority. The native package version in `pyproject.toml`, runtime `quality_gate.__version__`, `quality-gate.toml`'s `quality.policy_release`, the consumer template, the reusable workflow's policy selection, and the unified distribution inventory are validated projections.

The CLI compatibility boundary is the existing schema 2 `quality-gate` command surface and v2 native-hook/runtime contract. A patch release keeps that public surface compatible. The policy/manifest boundary is the immutable `quality.policy_release` selection and its exact release inventory. The reusable-workflow boundary is the caller's full commit SHA; this repository's post-merge workflow does not change consumer triggers or pins. The distribution boundary is the existing platform-specific ZIP containing the policy wheel, dependencies, policy files, and pinned tools.

## Release intent and notes

`.release/notes.md` is the reviewed release intent. It must declare the authoritative version, impact (`MAJOR`, `MINOR`, `PATCH`, or `NONE`), user-visible changes, required adaptation, and the common compatibility headings in this order: `Interface`, `Integrations`, `Configuration`, `Persisted data`, and `Delivery/runtime`. For `NONE`, the merged PR contains one reasoned `No release:` line and the post-merge workflow verifies the decision without publishing an artifact. A release PR body repeats the exact version and impact, includes the user-visible changes and required adaptation in the policy's `Changes:` line, and states that owner merge authorizes publication after the merged commit passes the required checks.

Opening or approving a PR never creates a tag or release. For a release intent, the controller accepts only a merged PR targeting the default branch, merged by the repository owner, whose declared version matches the source-controlled intent and whose merge SHA is the source being released. A `NONE` intent accepts the owner's matching no-release declaration and leaves the published baseline unchanged.

## Required checks

`.github/workflows/release.yml` first verifies the exact merged SHA's owner authorization and required `Quality Gate` check, then runs the complete repository test suite and `quality-gate audit` on both Linux and Windows through `scripts/release_adapter.py` when the reviewed intent is a release. A `NONE` intent verifies owner authorization and required checks, then exits successfully without building or publishing an artifact. The adapter calls the existing `quality_gate.release` controller for wheel installation, isolated runtime setup, audit, unified inventory, checksums, and failure-injection coverage. An absent, failed, stale, or source-unbound result is unchecked and prevents publication.

The build installs the tracked `.release/build-requirements.txt` hash lock for its no-build-isolation backend, then consumes `.release/release-requirements.txt` with binary-only installation. The build-only backend lock is not copied into the platform package inventory. `.release/release-tools.toml` also owns the bounded GitHub API, download, subprocess, required-check wait, and required-check poll timeouts. Its explicit `bootstrap.policy_release` is used only when this repository's own not-yet-published policy asset is absent; the reusable workflow then runs the current checkout's CLI against that previous immutable release. The adapter verifies the policy-pinned Biome digest and the SHA-256 digest recorded for the pinned Gitleaks asset before either tool enters a package.

The common controller is `scripts/source_release.py`. Its `verify --source-sha <SHA> --authorization-only` command performs the pre-build authorization, version/baseline, projection, and check validation without publishing. Its `verify --source-sha <SHA> --artifact-manifest <PATH>` command repeats those validations and adds artifact content/readiness validation without publishing; a `NONE` intent may omit the manifest and returns `no-release` after authorization and checks. Its `publish --source-sha <SHA> --artifact-manifest <PATH>` command repeats those validations and requires the adapter-produced identities for both supported platforms. The adapter's private bootstrap commands select `bootstrap.policy_release` only for this repository; the normal consumer CLI has no policy override and policy selection remains the manifest's immutable release.

## Publisher

The only publisher is `.github/workflows/release.yml` after the owner-merged PR has passed the required checks. It invokes `source_release.py publish` through an injected GitHub API boundary. The workflow is serialized per release unit and has a manual retry input for the exact source SHA of an unpublished attempt. It never publishes from an open PR, PR head, synthetic merge, unrelated direct push, or a moving branch tip.

Before creating or resuming a draft, the controller checks GitHub's immutable-releases repository setting and fails closed when it is unavailable or disabled. The setting must be enabled by repository administration before the first publication.

## Artifacts and source identity

`scripts/release_adapter.py` is the only Quality Gate-specific seam. It builds the existing Linux and Windows ZIP assets, preserves wheel installation and the unified inventory, and returns the common identity shape: source SHA, plain version, platform, asset name, and SHA-256 digest. `verify` and `runtime-check` run the existing release controller against that exact source and artifact. Fixed Gitleaks URLs/digests are recorded in `.release/release-tools.toml`; the publisher records the merged source SHA and both platform asset digests in the immutable GitHub Release.

## Retry and conflict rules

Tags and published assets are never replaced. A matching completed immutable release is verified and returned as `already-complete`. A draft or otherwise unpublished attempt may upload only missing assets whose names and digests match the verified adapter output; an existing conflicting asset, tag, source SHA, version, check result, or release notes body fails closed. A stale version proposal requires a corrective PR. A missing platform result prevents completion.

The release controller and tests cover no-release intent handling, unauthorized or unmerged PRs, source/version mismatch, failed or missing checks, artifact/source mismatch, stale versions, conflicting publications, matching reruns, and recoverable unpublished attempts through the mocked GitHub API boundary. No real release is deleted or overwritten to exercise these failures.

## Deployment and data boundaries

This package release does not deploy services, alter consumer workflow pins, change runtime rollout or rollback behavior, migrate persistent data, or replace backups. The existing local policy-cache retention and consumer synchronization procedures remain authoritative. Required package assets remain attached to the immutable release for the supported recovery lifetime.
