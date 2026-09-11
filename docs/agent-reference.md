# Quality Gate agent reference

Use this page as the entry point for work on Quality Gate. The project keeps contracts in one
place and uses pointers here instead of copying configuration rules.

## Authority map

- [Architecture map](architecture.md) is the route through modules and execution flows.
- [Coding standards](coding-standards.md) is the review standard for implementation changes.
- [Quality policy change guide](quality-policy-changes.md) applies to the runner, shared tools,
  manifest contract, and reusable workflow.
- [Lesson format](lessons.md) is the source of truth for escaped-defect lessons.
- [Release procedure](release.md) is the source of truth for release validation and publication.
- `quality_gate/contracts.py` is the source of truth for schema 2, verdicts, and waivers.
- `quality-gate.toml` declares this repository's policy release, documents, components, and limits.

This page is the single manifest-maintenance guide. Read it before changing declared components,
test paths, dependency inputs, required documents, workflows, waivers, or supplemental tests.

## Normal operation

Run commands from the repository root. `check`, `audit`, `validate`, `doctor`, and `version` do
not change repository files. `sync`, `setup`, rollback, confirmed prune, and `format` are the
explicit mutation commands.

1. Run `quality-gate validate` after editing `quality-gate.toml`.
2. Run `quality-gate doctor` to inspect the selected release and component runtimes.
3. For changes to this repository's source or policy, run the current-source `quality-gate check`
   before committing. For a consumer implementation, complete staged verification through its
   active workflow; its commit-hook Quality Gate satisfies this step. For standalone verification,
   run `quality-gate check` directly. Use `--verbose` only when the compact report is not sufficient.
4. Use `quality-gate format <explicit-path>...`, stage the result, and rerun the current-source
   `quality-gate check`
   for Quality Gate changes or return to the active workflow for consumer changes.
5. Use `quality-gate version` to confirm which installed policy engine is executing.

The native pre-commit wrapper reads the staged manifest, verifies its exact cached v2 wheel, and
calls the release-backed gate without importing mutable repository source. The native pre-push
wrapper blocks updates and deletion of the remote default branch when Git can prove its name.

## Manifest maintenance

Schema 2 accepts optional repository-owned supplemental tests in the canonical template:

```toml
[[supplemental_tests]]
name = "node-functional"
runner = "node-test"
targets = ["tests/node/**/*.test.js"]
```

The declaration has one unique name, the only supported runner is `node-test`, and every target is
a safe repository-relative path or glob that matches at least one path. Validation bounds the
manifest to 32 declarations, 32 target patterns per declaration, 1,024 matches per target, 4,096
matches total, and 256 KiB of matched path names. Arbitrary commands are not part of the manifest
contract. Quality Gate validates these declarations during `validate`, `check`, `audit`, native
hooks, and the reusable workflow, but does not execute them. The typed test-runner expands
validated globs without shell expansion, passes concrete paths to Node, and owns their execution
during `scope: full`; repository CI remains the enforcement owner.

After every `quality-gate.toml` edit, run `quality-gate validate`. Use
[templates/quality-gate.toml](../templates/quality-gate.toml) as the canonical shape; do not create
a project-local manifest copy in an agent skill.

## Setup and unavailable verification

Network access is allowed only in the explicit sync workflow:

```text
quality-gate sync --source <release-directory-or-zip> --version <vMAJOR.MINOR.PATCH>
quality-gate setup
quality-gate doctor
```

Use `quality-gate sync --prune` to preview cleanup and add `--confirm` only after reviewing the
preview. A missing release, runtime, tool, Python version, unreadable tree, timeout, or shallow
history is `unchecked`; it blocks completion and requires the recovery action in the report.
Never treat an `unchecked` result as a pass.

## Consumer release update

Keep the consumer on its current release while downloading the target immutable asset:

```text
quality-gate sync --url <target-release-asset-url> --version <target-version>
```

Then change these two independent pins in one feature-branch candidate:

1. Set `quality.policy_release` in `quality-gate.toml` to the target release. This selects the
   local wheel, policy files, scanner, and component tools.
2. Set the caller workflow `uses` value to the full commit SHA that contains the compatible
   reusable `.github/workflows/quality.yml`. This selects the CI implementation. Obtain the SHA
   from the verified release tag or publication record; do not use a tag or branch in the caller.

Run `validate`, `setup`, and `doctor`, then stage the manifest and workflow together. Use one
typed test-runner `scope: full` request: it runs every declared supplemental test and then the
staged `quality-gate check`, while a repository without a manifest keeps ordinary complete-suite
discovery. Commit only after it passes; the native commit hook repeats the staged gate. Push the
feature branch, require the single `Quality Gate` pull-request status, and leave approval and merge
to a human.

For rollback, restore both pins to the last known-good pair in a new feature-branch candidate,
then run `doctor`, the complete tests, and the staged gate again. `quality-gate sync --rollback`
only changes cache retention state; it does not override `quality.policy_release` or the workflow
SHA, so it is not a consumer rollback by itself.

## Migration, audit, and lessons

`quality-gate migrate` prints a schema 2 proposal for a schema 1 manifest and does not edit the
repository. Review the proposal, apply the manifest change in the consumer ticket, run `sync`,
`setup`, `check`, and then `audit`. `audit` includes every implemented domain, a full reachable
history secret scan, and lesson completion.

Record an escaped defect as one English Markdown file under `lessons/` using the format in
`docs/lessons.md`. An open lesson remains visible during remediation. The Quality Gate release
controller rejects malformed or unlearned lessons; consumer policy sync does not use that
release-only gate.

## Troubleshooting and security

- Use `validate` for a manifest contract error and `doctor` for a release or runtime error.
- Repair a failed finding. For Quality Gate source changes, rerun the current-source check; for
  standalone verification, rerun the failed command; for consumer implementations, return to the
  active workflow.
- Restore an `unchecked` prerequisite, then rerun the command; do not add a waiver for missing
  verification.
- Add a typed waiver only for one reviewed current finding. Keep its exact check ID and target,
  record approver, reason, review date, and expiry, and remove it when the exception ends.
- Secret reports contain locations and fingerprints only. Check normal, verbose, hook, CI, and
  migration output for redaction before sharing a report.
- A real credential found during migration must be rotated or revoked. History rewriting is a
  separate explicitly authorized operation.

The local pre-push policy cannot provide server-side enforcement on private GitHub Free
repositories. `git push --no-verify`, a changed global hooks path, or an unconfigured machine can
bypass local enforcement. Project policy prohibits those actions; CI detects direct default-branch
pushes but cannot undo them.

## Extension route

Read the architecture map and quality policy change guide before changing a shared check. Use the
authority files listed at the top of this page; do not move shared policy into a consumer manifest.
Add results at the established `CheckResult` boundary, keep published check IDs stable, and
preserve the meanings of `passed`, `failed`, `unchecked`, `not_applicable`, and `waived`.

Cover malformed input and injected failures at the public CLI, native Git, or CI seam affected by
the change. Preserve secret redaction in normal, verbose, hook, CI, audit, and migration output.
Update the architecture map when a module, flow, or search route changes. Release shared behavior
only as a new immutable version, then update each consumer's synchronized release and CI SHA as one
reviewed change. Owner merge authorizes the post-merge publisher; consumer activation, rollback,
and external rule changes remain human-reviewed decisions.

## Release and rollback route

Run the complete test suite and `quality-gate audit` before building the exact platform artifacts.
Then run the release controller described in [Release procedure](release.md). The owner-merged
post-merge workflow validates the exact source, checks, self-host manifest, lessons, and every
declared artifact in a temporary cache before publishing the immutable GitHub Release.

Retain the active and preceding policy releases. To recover a bad activation, run
the consumer rollback procedure above: restore the manifest release and reusable-workflow SHA,
then verify the complete candidate. Ruleset changes, consumer pin updates, and rollback remain
human-reviewed operations; an unpublished release attempt is retried only for its exact source
SHA.
