# Publication recovery and verification

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
