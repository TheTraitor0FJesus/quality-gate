# Publisher recovery

## Envelope retention, completion, and recovery

Preparation retains the original merged candidate before product builds or release writes. Recover
from that candidate; never reconstruct authorization from the PR body as it exists today. If body
edits conflict with the retained candidate, restore the original body. If the original candidate or
its evidence has expired or is missing, use an available rerun of the original workflow or prepare
a new explicitly reviewed release PR.

For transient failures, rerun the failed jobs in the original workflow. For repaired tooling, dispatch
from the default branch only when the original run cannot safely be rerun and no earlier image write
left a version tag without a trusted draft receipt:

```shell
gh workflow run release.yml --ref <default-branch> -f pr_number=<original-merged-pr>
```

If a version tag exists without a trusted writer receipt, stop for owner recovery. The publisher must
not infer its digest from the tag or image contents, and a new dispatch would run the image writer
again.

### Historical 403 requiring Workflows: write

Use this route only when the publisher job alone failed because GitHub requires `Workflows: write` to
create the historical tag. The original product source and successful build, producer, and writer
evidence must still be available and unambiguous. Check `v<version>` with an authenticated maintainer
Git client. If the tag is absent, create and push only that tag at the retained candidate's exact
`source_sha`:

```shell
git tag -a v<version> <source-sha> -m "Release v<version>"
git push origin refs/tags/v<version>
```

Then rerun only the failed **Publish release** job in the original Actions run. Do not dispatch a new
run or rerun the image writer. Stop and ask the owner to recover if another job failed, required
evidence or logs are unavailable, a publisher-only rerun is unavailable, or the tag exists at a
different source. Keep the candidate and do not change token permissions.

A matching completed release is read back before rebuild and can be verified after temporary Actions
artifacts expire. Publication does not move tags or replace existing assets. The release body retains
the completed candidate and delivery receipt for later verification.
