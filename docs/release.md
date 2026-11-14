# Quality Gate release procedure

This page covers Quality Gate's product release. The [shared publisher guide](publication.md)
describes reusable caller wiring and publication responsibilities; its implementation owns the exact
declaration and evidence formats.

## Prepare a candidate

1. Review published releases and classify the accumulated unreleased product changes for the
   candidate. A feature-target PR carries its impact forward; prepare the release on the final
   default-target PR.
2. Update `.release/version.toml` and the product projections declared in `.release/publisher.toml`
   together. Keep the provider's `quality-gate.toml` on the latest published policy while preparing
   the candidate. The canonical consumer template projects the product release.
3. Replace `.release/notes.md` with Russian user-visible changes and any required adaptation for this
   candidate. Include only accumulated changes selected for the candidate; published release pages
   remain the history.
4. Declare the release decision in the PR using the [shared publication guide](publication.md).
   Internal-only changes need an explicit no-release decision. The owner authorizes publication by
   merging the final default-target PR.

## Product verification

`.github/workflows/release.yml` calls preparation, builds and verifies the exact merged source on
Windows and Linux, then calls publication. No-release and already-complete outcomes skip product
builds. `scripts/release_build.py` coordinates each platform; `scripts/release_adapter.py` owns package
construction and isolated installation/runtime validation; `scripts/release_metadata.py` owns
product metadata and release timeouts. The adapter calls `quality_gate.release` to check the source,
release lessons, and built artifact. Both platform jobs run the complete repository test suite against
the candidate release.

The installed-artifact Gate owns the full test run. It uses the candidate policy and its bundled
tools, a prepared component runtime, and the source snapshot whose Python component declares
`test_paths = ["tests"]`. Pytest's `pythonpath = ["."]` selects that snapshot's source. A successful
PR check does not replace this merged-source verification. The build rejects a manifest without
the Python domain or an applicable component covering the complete `tests` directory before it
starts building. Narrowing the manifest cannot silently remove release regression coverage.

After artifact verification, the build still syncs the candidate and the source manifest's pinned
published policy into a separate cache. It runs `setup` and `doctor` for that manifest before writing
publication evidence. This checks pinned-policy and runtime readiness without running a second full
suite in the build interpreter. The old direct run used the build interpreter's release-tool lock;
it was a different environment from the candidate's bundled tools. The readiness check does not
claim a second full regression pass under that interpreter or the older policy.

| Obligation | Required verification |
| --- | --- |
| Archive integrity, candidate version, installed engine and complete tests | Adapter `runtime-check` and the installed-artifact Gate |
| Source manifest's published policy and component runtime availability | Candidate and pinned-policy sync, `setup`, then `doctor` |
| Exact merged source and both supported platforms | Existing Windows and Linux product jobs |
| Publication evidence after successful required commands | Release build failure propagation and final evidence write |

The release build prints `release-timing:` JSON records with a stage name, monotonic elapsed seconds
and completion status. `package-finalization` covers package copying and artifact/evidence writes.
`workspace-cleanup` covers the outer temporary workspace's exit. `artifact-verification` includes the
adapter's own verification and nested cleanup. These records help distinguish build work from the
Actions transition to upload. An incomplete or failed cleanup still fails the build. Historical
post-test delays cannot be attributed to cleanup or counted as savings without a matching platform
run.

The provider's source manifest stays on its latest published policy until publication succeeds.
After release, advance the provider policy pin in a separate no-release PR. This prevents release
verification from depending on the candidate's unpublished policy asset.

On Windows, `quality_gate.release` checks the resolved final release workspace path before syncing
artifacts or auditing. If it exceeds 180 characters, shorten or move the worktree, or pass an existing
shorter directory with `--workspace-parent`. This limit applies only to release workspaces.

## Retry and provider handoff

Rerun failed jobs for transient failures. For repaired tooling, dispatch `release.yml` from the
default branch with `pr_number` set to the original merged PR only when the original run cannot be
safely rerun and no earlier image write left a version tag without a trusted draft receipt. For the
historical GitHub 403 case that requires `Workflows: write`, rerun only the failed publisher job in
the original run after creating the exact missing tag. Do not start a new run or repeat the image
write. Follow [publication recovery](publication-recovery.md) for the exact recovery conditions and
commands. Keep the product source at the original merged SHA.

After publication and readback, consumers use the verified shared-helper commit SHA for all three
reusable workflows. Consumers change that pin and their policy release only through an explicit
rollout. The provider activation workflow opens a reviewed no-release PR to advance its own policy
pin; a feature PR or source merge alone does not complete publication or activation.
