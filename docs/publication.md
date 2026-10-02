# Shared publisher contract

Paths in this contract are relative to the provider repository root unless labelled caller-owned.
The evidence schema is version 1. Consumers select one accepted full provider commit SHA, **R**,
for all three reusable workflows. A later R requires an explicit rollout and verification of affected
behavior; it does not change product versions or Quality Gate pins. Each R may update caller
permissions and one-time package access requirements, which are documented before rollout.

## Entry points and ownership

- `.github/workflows/release-prepare.yml` owns PR validation and original-candidate preparation.
- `.github/workflows/image-writer.yml` validates data-only Docker-save-compatible image archives
  and pushes them to GHCR.
- `.github/workflows/release-publish.yml` owns serialized provenance verification and publication.
- `scripts/source_release.py` executes `quality_gate.publication_cli` from its own checkout.
- `quality_gate.publication_flow.Publisher` is the shared preparation/publication interface used by
  behavioral tests with a controlled GitHub service. The other `publication_*` modules implement
  transport, bounded artifact reads, and provenance; product adapters contain no release parser.
- The caller owns `.release/version.toml`, `.release/notes.md`, `.release/publisher.toml`, exact-source
  build/check jobs, job-scoped token permissions, package access, and deliverable retention.

Each wrapper checks out `job.workflow_repository` at `job.workflow_sha`, disables persisted checkout
credentials, and verifies that the helper checkout is clean and identifies that exact commit before
using publication credentials. External caller references must end in the same full R. Actions API
`referenced_workflows` must agree with the executed helper. A pinned wrapper with a floating or
different helper is rejected. The provider self-hosts through same-repository references: its
executed shared-code identity is the final merged source containing all three workflows and helper.
Record that source as consumer R only after provider publication/readback and acceptance.

A later provider self-policy activation keeps the accepted published R and product version.
External consumers continue pinning the accepted release helper; the provider's same-repository
calls execute the activation's merged source. The acceptance handoff records these identities
separately, together with the policy release and platform asset digests.

ARGUS_BRAIN consumes the Quality Gate for checks only. Its required application and isolated
service checks use the accepted policy/helper pair without release validation, preparation,
writer, publisher, or recovery callers. Historical releases and backup responsibilities remain
unchanged. TG and Tracker retain image releases, Trading retains ZIP, and Web retains tar.gz;
their product adapters use the declaration and publication protocol below.

## Caller wiring

An open-PR caller targets the repository's live default branch and handles `opened`, `synchronize`,
`reopened`, and `edited`. It calls preparation with `validation-only: true`, read-only permissions,
and no publication secret. Validation reads exact-source metadata through the GitHub contents API;
it does not execute PR product code or retain a candidate envelope.

The publication caller handles `pull_request: closed`, filters its default branch, and guards
preparation with `github.event.pull_request.merged == true`. It also exposes `workflow_dispatch`
with a required `pr_number` for recovery. Name its preparation call **Prepare release**; the resulting
producer job identity is **Prepare release / Prepare candidate**. Product jobs depend on preparation
and run only when `status == 'build-required'`. An image product uses one dedicated **Write image**
job after its verified producer jobs; that job has no steps and only calls the exact full-SHA
`image-writer.yml`. The **Publish release** job depends on preparation, every product job, and the
writer when images are configured. The writer receives `pr-number` from the closed PR or recovery
dispatch input and `candidate-id` from preparation. All three shared calls derive `pr-number` from
the closed PR or recovery dispatch input. The Quality Gate caller is the archive-only self-hosting
example in `.github/workflows/release.yml`.

Preparation inputs:

| Input | Type | Meaning |
| --- | --- | --- |
| `pr-number` | Required string | Positive PR number in the caller repository |
| `validation-only` | Boolean, default `false` | Read-only open-PR validation; never valid for recovery dispatch |

Preparation outputs are strings:

| Output | Meaning |
| --- | --- |
| `status` | `validated`, `no-release`, `build-required`, or `already-complete` |
| `version` | Source authority's plain semantic version |
| `source-sha` | Final product source for post-merge/recovery; empty for open validation |
| `candidate-id` | GitHub artifact ID of the retained original envelope; empty when no build is needed |
| `candidate-run-id` | Run that owns that envelope, including recovery from another run |
| `candidate-digest` | SHA-256 of canonical `candidate.json` bytes, distinct from its uploaded ZIP digest |
| `provider-sha` | Actually executed shared helper/workflow revision for this verification run |
| `provider-repository` | Provider repository owning that revision |

Publication takes required string inputs `pr-number` and `candidate-id`. The publisher performs all
GitHub Release reads, tag/release/asset mutations, and readback with the same automatically issued
`GITHUB_TOKEN`. The `publish` reusable-call job grants `contents: write`, `pull-requests: read`,
`actions: read`, and `packages: read`; no `RELEASE_TOKEN` or other long-lived publisher credential is
used. GitHub can still refuse recovery if a release target needs `Workflows: write`, which
`GITHUB_TOKEN` cannot receive. Recovery then fails closed with the original candidate preserved.

The preparation, image writer, and publisher use job-scoped `GITHUB_TOKEN` permissions. The image
writer is the only release-flow job that receives `packages: write`; its caller has no steps and
only invokes the exact shared full-SHA writer. The publisher and image-readback paths use
`packages: read` and a temporary Docker auth file from that same short-lived token. The helper
removes the auth configuration on exit. If a GHCR package is owned by another repository, its owner
grants the caller repository access under the package's **Manage Actions access** settings. This is
a package ACL setting, not a token or repository secret. No GHCR PAT or other GHCR credential is
used.

The GHCR tag is checked before a write. A retained draft must match the exact candidate and source,
and its archive and immutable image digest must still match the current image. An existing tag is
reusable only when a retained draft writer receipt supplies its expected raw registry-manifest
SHA-256 and that digest matches exactly; the writer then checks config and layer identities. If a
partial push left a tag before a writer receipt was retained, the writer fails closed for owner
review. It does not infer a trusted digest from config or layers, overwrite or retag the image, or
automatically delete the tag. Matching retries with a retained exact digest do not push again. Each
publisher CLI process has one monotonic 1500-second Docker/GHCR phase budget, starting with its first
image command and shared by every later image preflight, push, and readback command. Successes,
errors, and HTTP 404 responses do not reset it; an exhausted budget blocks the next process. GitHub
API commands keep their separate 60-second per-command timeout. The Docker archive and expanded
contents are limited to 512 MiB. The image-writer and release-publisher jobs each have a 30-minute
job timeout. That job limit includes setup before the first image command, so the 25-minute phase
budget is not a reservation from CLI start and leaves no guaranteed five-minute setup window.

Administration access and native Immutable Releases are not required. The publisher job installs no
dependencies and starts no product code.

The common publication workflow serializes each repository with `cancel-in-progress: false`.
Preparation/build runs have distinct concurrency groups so another candidate cannot cancel an
active run. GitHub can replace a pending publication; its retained PR candidate can be recovered
with dispatch and is never recorded as released merely because it was queued.

## PR and source metadata

A default-target PR has exactly one top-level second-level `## Release` section. Its only nonempty
lines are these exact Markdown list fields:

```markdown
## Release

- Version: 2.3.0
- Changes: Описание изменений и необходимой адаптации.
- Publication: owner merge authorizes publication after the merged commit passes the required release checks.
```

Alternatively, the section contains only `- No release: <nonempty Russian reason>`. Duplicate
sections/fields, quoted or fenced declarations, mixed modes, and fields under another heading
cannot authorize publication. Write changes, reasons, and notes in Russian. The version field is
only plain `MAJOR.MINOR.PATCH`; transitions and impact labels are not part of this schema.

Caller-owned `.release/version.toml` contains exactly one top-level version string. Configured native
projections must agree. `.release/notes.md` is nonempty source-controlled prose, with required
adaptation when applicable; it has no mandatory version, Impact field, or compatibility headings.
Old notes do not request a release. No-release leaves the version unchanged relative to the PR base.
A release proposes one normal next major, minor, or patch increment from the applicable published
stable baseline. Prepare the classification from all unreleased changes before owner review.

The PR API supplies the final `merge_commit_sha` after merge, squash, or rebase-and-merge. The initial
event must identify the same owner-merged default-target PR and final source. Read product metadata
from that source, never from a moving branch. GitHub's REST Actions `head_sha` can remain the PR head
even for a merged `closed` event; it is a separate run identity, not the published source. The
envelope retains both, and provenance checks bind run/job/artifact API identities to the original
PR head while product checks and release tags bind to the exact final source.

## Product configuration and evidence

Caller-owned `.release/publisher.toml` declares schema `1`, the caller's repository-relative release
workflow path, optional native version projections, and every required product producer. Use this
repository's file as the archive/multi-platform example. Projection formats are `toml`, `json`, or
`python`; dotted keys address TOML/JSON fields, and a Python key names one top-level literal
assignment. An optional prefix is removed before comparing versions. Python source is parsed,
never imported. Each producer names its exact Actions job, required successful step names, and
unique deliverable names; `{version}` in a name expands to the source authority.
Each producer's optional `kind` is `archive` by default, or explicitly `package` or `image`.
Image producers declare one deliverable and a lowercase `image_repository` such as
`ghcr.io/owner/project`; different image producers use different package paths. Evidence and
completed receipts must retain the source-configured names and kinds. Configuration
allows at most 32 producers and 32 unique check/deliverable names per producer; names and keys are
bounded to 256 characters, and deliverable filenames must be safe flat identifiers.

When `.github/workflows/release.yml` calls the exact pinned image writer, Quality Gate reads
`.release/publisher.toml` from that same candidate tree and requires its `workflow` to name the
release workflow. Each configured image producer's `job` is the Actions job `name`, not its YAML job
ID. The gate resolves it to exactly one local job with a unique explicit literal name, requires that
job's own permissions map to grant `contents: read` and `packages: read` with no write scopes, and
requires its YAML job ID as a direct member of `write-image.needs`. Missing or invalid configuration,
unresolved or duplicate names, dynamic producer names, missing read permissions, write permissions,
or missing direct dependencies fail closed. Matrix-expanded and nested reusable job names are not
resolved dynamically; image producers must use ordinary local jobs.

After all required checks, each producer uploads one artifact named
`release-evidence-<producer-name>-<run-attempt>` from a step named **Upload release evidence** using
the provider's pinned upload-artifact action. Include only flat regular files: `evidence.json` and
the declared archive/package files; an image producer also includes its data-only Docker image
archive as a flat file. The producer job and every named check/upload step must finish
successfully. Keep the upload logs; their GitHub-generated artifact ID and ZIP digest bind the
actual producing job to the artifact API record.

An image producer includes one flat, data-only Docker-save-compatible archive. Its `manifest.json`
must contain one image record with `Config`, one `RepoTags` entry, and nonempty `Layers`; archives
that contain only an OCI layout are unsupported. The writer also requires the image config's
`org.opencontainers.image.revision` label to match the approved source SHA.

`evidence.json` has these fields:

| Field | Type and contract |
| --- | --- |
| `schema` | Integer `1` |
| `repository`, `pr` | Caller repository and positive original merged PR number |
| `source_sha`, `version` | Exact product source and plain product version |
| `provider_sha` | Current preparation's `provider-sha` output |
| `run_id`, `attempt` | Actual verification run and producing attempt |
| `candidate_sha256` | Preparation's `candidate-digest` output |
| `deliverables` | Required unique deliverable objects. Archives/packages include `kind`, `name`, and `sha256`; images include `kind`, `name`, `archive`, and `archive_sha256` |

Kinds are `archive`, `package`, and `image`. Archives/packages become GitHub release assets without
repackaging. Their bytes must match the declared SHA-256. The shared writer locates the supported
Docker-save archive through candidate-bound producer evidence, validates its
`org.opencontainers.image.revision` label, loads and inspects the image without running it, then
pushes the configured package with the caller's `GITHUB_TOKEN` to `v<version>`. It retains an
`image-receipt.json` artifact containing candidate, source, provider, writer-run, producer-artifact,
archive-digest, tag, and immutable registry-digest identities. The publisher independently verifies
the successful writer job, artifact-upload logs and digest, exact writer workflow revision, and
candidate/producer bindings. Before GHCR readback, it requires every reference to equal the
source-configured `image_repository@sha256:<receipt digest>`. It reads each such reference back from
GHCR before it creates or changes a GitHub Release. Completed releases retain the verified writer
receipt with the deliverable receipt. The image is never run in either shared job.

The publisher cross-checks actual run repository/path/reusable revision, the latest producing job
attempt and result, required step results, artifact run/source, timestamps, ID, archive digest, and
producer upload logs. It downloads by verified artifact ID and validates transfer integrity before
reading evidence JSON. Reused names and self-declared JSON alone are insufficient. Evidence is
paired with the job record from its exact attempt. If GitHub reports a successful job reused by a
later run attempt, an earlier artifact is accepted only when both attempt records identify the same
execution; a newly executed producer must provide evidence for that attempt. Incomplete API lists,
run records, producer-job fields, and artifact metadata are retried for a bounded interval. Missing
or mismatched provenance then fails with a diagnostic before publication mutations. A newer failed
or pending producer blocks publication. A recovery run verifies new product evidence while retaining the
original candidate separately.

## Envelope retention, completion, and recovery

After initial merge authorization, preparation saves `candidate.json` before product builds or
release mutations. The immutable Actions artifact name is
`release-candidate-pr-<number>-<origin-run-id>-<origin-attempt>`, with 90-day requested retention;
repository limits, explicit deletion, and log retention can shorten availability. Canonical JSON
is UTF-8, sorted keys, no separator whitespace, unescaped Unicode, followed by one newline.

The envelope records schema, repository/PR, final source, PR base, merge time, original body,
version, exact notes and notes digest, product configuration, provider repository/revision, and
original run/attempt/REST head identity. Recovery locates envelopes through bounded Actions artifact
listing, verifies the original successful preparation job and upload receipt, and rejects missing,
expired, or conflicting original evidence. An ordinary original-event rerun reuses a matching
retained envelope. Unknown historical authorization is never reconstructed from today's PR body.
Conflicting body edits require restoring the original body. If no original evidence is available,
use an available original-event rerun or a new explicitly reviewed release PR.

For repaired tooling, dispatch recovery from the repository's default branch only when the original
run cannot safely be rerun and no earlier image write left a GHCR version tag without a trusted
draft receipt. A new dispatch reruns the image writer. When that tag already exists without a
trusted draft receipt, publication fails closed; its digest cannot be inferred from the tag or image
contents.

```shell
gh workflow run release.yml --ref <default-branch> -f pr_number=<original-merged-pr>
```

### Historical 403 requiring Workflows: write

If tag creation fails with HTTP 403 because the historical target requires `Workflows: write`, keep
the original candidate and its recorded `source_sha`. Check `v<version>` with an already-authenticated
maintainer Git client. If the tag is absent, create and push only that tag at the candidate's exact
source SHA. Then rerun only the failed **Publish release** job in the original workflow run. GitHub
keeps the original `GITHUB_SHA` and `GITHUB_REF` when rerunning a specific job, so the publisher can
reuse the successful producer and image-writer jobs and their original Actions evidence. See
[GitHub's workflow and job rerun documentation](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).

This route applies only when the publisher job alone failed and all successful producer/writer jobs,
artifacts, and upload logs remain available and unambiguous. If the publisher-only rerun is unavailable,
any other job failed, required evidence or logs are missing or expired, or the tag points elsewhere,
stop and ask the owner for manual recovery. Never rerun all jobs, dispatch a new workflow for this
403 case, or run the image writer again: an existing GHCR tag without a trusted draft receipt must
remain fail-closed. This recovery creates no source commit or Actions secret. Do not replace the
candidate or change `GITHUB_TOKEN` permissions.

```shell
git tag -a v<version> <source-sha> -m "Release v<version>"
git push origin refs/tags/v<version>
```

Before publishing, the helper creates or resumes the exact-source tag and draft, checks every
existing identity, uploads only missing matching files, and reads back complete readiness before
promotion. Recovery preserves a matching draft's receipt even when a fresh build produces different
archive bytes; existing assets must still match that receipt, and a missing asset is uploaded only
when the current verified bytes match its recorded digest. It never moves a tag or replaces an
existing asset. Completed releases retain the full candidate and deliverable/provenance receipt
inside the release body alongside the exact notes.
Matching completed preparation verifies source, notes, tag, and every required deliverable and
returns `already-complete` before any rebuild, including after temporary Actions artifacts expire.
Conflicting completed identities or duplicate drafts fail without mutation. Native immutability
may strengthen this storage but is not a prerequisite of the shared state machine.

## Verification and handoff

The public behavioral suite is `tests/test_publication*.py`; package/runtime failure protection
remains in `tests/test_release.py` and the existing distribution/runtime suites. Cover A01–A13 from
the migration specification through shared preparation/publication and the real caller. The final
provider handoff must identify both owner-merged PRs, final source, released tag/URL, accepted R,
executed self-host helper SHA, exact-revision behavioral results, and Windows/Linux asset digests
and verification jobs. A ready ticket PR is implementation readiness, not completed publication.
