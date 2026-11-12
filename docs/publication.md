# Shared publisher guide

The provider owns reusable release workflows and their publisher. The caller owns product versions,
release notes, product configuration, builds, checks, and artifacts. Exact input/output shapes,
declaration fields, and evidence formats belong to the workflow and code that implement them.

When preparing a PR's release decision, use `parse_decision` in
[`quality_gate/publication.py`](../quality_gate/publication.py).

## Entry points and ownership

- `.github/workflows/release-prepare.yml` validates a release decision and retains the original
  merged candidate before product builds or release writes.
- `.github/workflows/image-writer.yml` validates a data-only image archive and pushes it to GHCR.
- `.github/workflows/release-publish.yml` checks the caller's product evidence and publishes the
  release.
- `scripts/source_release.py` starts the shared publisher from its own checkout.
- `quality_gate/publication.py` and `quality_gate/publication_flow.py` own shared release decisions
  and orchestration. Product adapters do not implement another release parser or state machine.

All three reusable calls must pin the same accepted full provider commit SHA. Each wrapper checks out
the helper at GitHub's actual reusable-workflow revision and verifies that checkout before using
publication credentials. A floating caller reference or a helper checkout at a different revision
fails. The provider uses same-repository workflow references; consumers use the verified revision
from the release record.

## Caller wiring

For open-PR validation, call preparation against the default branch with read-only permissions. It
reads PR metadata and does not run product code or retain a candidate. For publication, gate
preparation on a merged PR targeting the default branch or an explicit recovery dispatch. Product
jobs run only after preparation says a build is required, and publication waits for every product job
and the image writer when configured.

The caller grants only the permissions each job needs. Product build jobs own their checks and
artifact uploads. The publisher uses the job-scoped `GITHUB_TOKEN`; its call needs `contents: write`
and read access to pull requests, Actions artifacts, and packages. Only the dedicated image-writer
job gets `packages: write`; its caller job has no extra steps. No long-lived release token or GHCR
PAT is needed. For a GHCR package owned by another repository, grant the caller under the package's
**Manage Actions access** settings.

The publisher may fail when GitHub requires `Workflows: write` to create a historical tag. The
built-in token cannot receive that permission. Keep the original candidate and follow the
[publisher recovery guide](publication-recovery.md#historical-403-requiring-workflows-write).

## Product and image boundaries

The caller declares its product version source, optional version projections, required producers,
checks, and deliverables in `.release/publisher.toml`. Use the provider's file as the archive and
multi-platform example. The caller builds and tests the exact merged source. Product jobs upload
their declared files and evidence; the shared publisher independently verifies the producing run,
jobs, steps, artifact records, and candidate identity before publication.

For images, the shared writer loads and inspects the caller's data-only Docker archive without running
it, then pushes the configured package. Publication reads back the immutable image digest before
changing the GitHub release. An existing version tag is reusable only when a retained writer receipt
supplies the exact expected digest. If a partial push left a tag without that receipt, stop for owner
recovery. Do not infer a trusted digest from image contents, overwrite or retag the image, or delete
the tag.

## Recovery

Use [publisher recovery](publication-recovery.md) for reruns, retained candidates, and tag recovery.
The implementation is in `quality_gate/publication*.py`.
