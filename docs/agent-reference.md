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

## Normal operation

Run commands from the repository root. `check`, `audit`, `validate`, `doctor`, and `version` do
not change repository files. `sync`, `setup`, rollback, confirmed prune, and `format` are the
explicit mutation commands.

1. Run `quality-gate validate` after editing `quality-gate.toml`.
2. Run `quality-gate doctor` to inspect the selected release and component runtimes.
3. Run `quality-gate check` before committing. Use `--verbose` only when the compact report is
   not sufficient.
4. Use `quality-gate format <explicit-path>...`, stage the result, and run `check` again when
   source formatting needs correction.
5. Use `quality-gate version` to confirm which installed policy engine is executing.

The native pre-commit wrapper calls the stable launcher for staged commits. The native pre-push
wrapper blocks updates and deletion of the remote default branch when Git can prove its name.
Until ticket 18 is complete, the configured global commit path remains the v1 runtime. Do not
change that routing while working on tickets 10–17.

## Setup and unavailable verification

Network access is allowed only in the explicit sync workflow:

```text
quality-gate sync --source <release-directory-or-zip> --version <vMAJOR.MINOR.PATCH>
quality-gate setup
quality-gate doctor
```

Use `quality-gate sync --rollback [VERSION]` for a retained release. Use
`quality-gate sync --prune` to preview cleanup and add `--confirm` only after reviewing the
preview. A missing release, runtime, tool, Python version, unreadable tree, timeout, or shallow
history is `unchecked`; it blocks completion and requires the recovery action in the report.
Never treat an `unchecked` result as a pass.

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
- Repair a failed finding, then rerun the same command and the complete `check`.
- Restore an `unchecked` prerequisite, then rerun the command; do not add a waiver for missing
  verification.
- Secret reports contain locations and fingerprints only. Check normal, verbose, hook, CI, and
  migration output for redaction before sharing a report.
- A real credential found during migration must be rotated or revoked. History rewriting is a
  separate explicitly authorized operation.

The local pre-push policy cannot provide server-side enforcement on private GitHub Free
repositories. `git push --no-verify`, a changed global hooks path, or an unconfigured machine can
bypass local enforcement. Project policy prohibits those actions; CI detects direct default-branch
pushes but cannot undo them.

## Extension route

Read the architecture map and quality policy change guide before changing a shared check. Add a
check at the established `CheckResult` boundary, keep check IDs stable, add malformed-input and
failure-injection coverage at the public CLI or native Git seam, and update the architecture map
when a module, flow, or search route changes. Consumer configuration may describe structure and
narrow current waivers; it must not replace shared Ruff, mypy, runner, or CI policy.

## Release and rollback route

Run the complete test suite and `quality-gate audit` before building the exact platform artifacts.
Then run the release controller described in [Release procedure](release.md). It validates the
self-host manifest and lessons and verifies every declared artifact in a temporary cache before a
human publishes the immutable GitHub Release.

Retain the active and preceding policy releases. To recover a bad activation, run
`quality-gate sync --rollback`, verify with `doctor`, and run the complete `check`. Release
publication, ruleset changes, and policy updates remain human-reviewed operations.
