# Shared publisher contract

Paths in this contract are relative to the provider repository root unless labelled caller-owned.
The public workflow interface and evidence schema are version 1. Consumers select one accepted
full provider commit SHA, **R**, for both reusable workflows. A later R requires an explicit rollout
and verification of affected behavior; it does not change product versions or Quality Gate pins.

## Entry points and ownership

- `.github/workflows/release-prepare.yml` owns PR validation and original-candidate preparation.
- `.github/workflows/release-publish.yml` owns serialized provenance verification and publication.
- `scripts/source_release.py` executes `quality_gate.publication_cli` from its own checkout.
- `quality_gate.publication_flow.Publisher` is the shared preparation/publication interface used by
  behavioral tests with a controlled GitHub service. The other `publication_*` modules implement
  transport, bounded artifact reads, and provenance; product adapters contain no release parser.
- The caller owns `.release/version.toml`, `.release/notes.md`, `.release/publisher.toml`, exact-source
  build/check jobs, registry/package credentials, and existing deliverable retention.

Each wrapper checks out `job.workflow_repository` at `job.workflow_sha`, disables persisted checkout
credentials, and verifies that the helper checkout is clean and identifies that exact commit before
using publication credentials. External caller references must end in the same full R. Actions API
`referenced_workflows` must agree with the executed helper. A pinned wrapper with a floating or
different helper is rejected. The provider self-hosts through same-repository references: its
executed shared-code identity is the final merged source containing both workflows and helper.
Record that source as consumer R only after provider publication/readback and acceptance.

## Caller wiring

An open-PR caller targets the repository's live default branch and handles `opened`, `synchronize`,
`reopened`, and `edited`. It calls preparation with `validation-only: true`, read-only permissions,
and no publication secret. Validation reads exact-source metadata through the GitHub contents API;
it does not execute PR product code or retain a candidate envelope.

The publication caller handles `pull_request: closed`, filters its default branch, and guards
preparation with `github.event.pull_request.merged == true`. It also exposes `workflow_dispatch`
with a required `pr_number` for recovery. Name its preparation call **Prepare release**; the resulting
producer job identity is **Prepare release / Prepare candidate**. Product jobs depend on preparation
and run only when `status == 'build-required'`. Publication depends on every required product job.
The Quality Gate caller is the complete self-hosting example in `.github/workflows/release.yml`.

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

Publication takes required string inputs `pr-number` and `candidate-id`, and the required secret
`publication-token`. Its successful CLI result is `published` or `already-complete`. It exposes no
product-execution hook. Supply a repository-scoped token with Contents write, Pull requests read,
and Actions read; repository/workflow protection may additionally require Workflows write.
Both entry points accept the optional caller-owned `registry-auth` secret for private image
readback, including already-complete preparation. Its Docker configuration contains only
`{"auths":{"<registry>":{"auth":"<base64 credentials>"}}}`. The helper writes it to a temporary
restricted configuration and removes it on exit; executable credential helpers are rejected.
Open-PR validation callers omit this secret.
Administration access and native Immutable Releases are not required. The built-in job token
remains read-only. The publication job installs no dependencies and starts no product code.

The common publication workflow serializes each repository with `cancel-in-progress: false`.
Preparation/build runs have distinct concurrency groups so another candidate cannot cancel an
active run. GitHub can replace a pending publication; its retained PR candidate can be recovered
with dispatch and is never recorded as released merely because it was queued.

## PR and source metadata

A default-target PR has exactly one top-level second-level `## Release` section. Its only nonempty
lines are these exact Markdown list fields:

```markdown
## Release

- Version: 2.1.0
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
Evidence and completed receipts must retain the source-configured names and kinds. Configuration
allows at most 32 producers and 32 unique check/deliverable names per producer; names and keys are
bounded to 256 characters, and deliverable filenames must be safe flat identifiers.

After all required checks, each producer uploads one artifact named
`release-evidence-<producer-name>-<run-attempt>` from a step named **Upload release evidence** using
the provider's pinned upload-artifact action. Include only flat regular files: `evidence.json` and
the declared archive/package files. The producer job and every named check/upload step must finish
successfully. Keep the upload logs; their GitHub-generated artifact ID and ZIP digest bind the
actual producing job to the artifact API record.

`evidence.json` has these fields:

| Field | Type and contract |
| --- | --- |
| `schema` | Integer `1` |
| `repository`, `pr` | Caller repository and positive original merged PR number |
| `source_sha`, `version` | Exact product source and plain product version |
| `provider_sha` | Current preparation's `provider-sha` output |
| `run_id`, `attempt` | Actual verification run and producing attempt |
| `candidate_sha256` | Preparation's `candidate-digest` output |
| `deliverables` | Required unique deliverable objects: `kind`, `name`, `sha256`; images also have `reference` |

Kinds are `archive`, `package`, and `image`. Archives/packages become GitHub release assets without
repackaging. Their bytes must match the declared SHA-256. An image reference identifies its registry
repository and `@sha256:<digest>`; the publisher resolves that digest with `docker buildx imagetools
inspect --raw`, without running the image. Docker's raw output preserves the original manifest
bytes, including their digest, as defined by its [printer implementation](https://github.com/docker/buildx/blob/master/util/imagetools/printers.go).
Supply the caller's separate `registry-auth` secret when registry reads require authentication;
the existing product owner remains responsible for registry retention. Do not replace registry
credentials with the publication token or install product dependencies in the publisher job.

The publisher cross-checks actual run repository/path/reusable revision, latest producing job
attempt and result, required step results, artifact run/source, timestamps, ID, archive digest, and
producer upload logs. It downloads by verified artifact ID and validates transfer integrity before
reading evidence JSON. Reused names and self-declared JSON alone are insufficient. A successful
producer from an earlier attempt of the same run remains valid when only failed jobs are rerun;
a newer failed/pending producer supersedes it and blocks publication. A recovery run verifies new
product evidence while retaining the original candidate separately.

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

Dispatch recovery from the repository's default branch:

```shell
gh workflow run release.yml --ref <default-branch> -f pr_number=<original-merged-pr>
```

Before publishing, the helper creates or resumes the exact-source tag and draft, checks every
existing identity, uploads only missing matching files, and reads back complete readiness before
promotion. It never moves a tag or replaces an existing asset. Completed releases retain the full
candidate and deliverable/provenance receipt inside the release body alongside the exact notes.
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
