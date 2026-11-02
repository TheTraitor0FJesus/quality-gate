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
- [Shared publisher interface](publication.md) owns reusable release callers, evidence, and recovery.
- `quality_gate/contracts.py` is the source of truth for schema 2, verdicts, and waivers.
- `quality-gate.toml` declares this repository's policy release, documents, components, and limits.

This page is the single manifest-maintenance guide. Read it before changing declared components,
test paths, dependency inputs, required documents, workflows, waivers, or supplemental tests.

## Normal operation

Run commands from the repository root. `check`, `audit`, `validate`, `doctor`, and `version` do
not change tracked project files. Quality Gate puts temporary work under the checked repository's
ignored `temp/` directory, removes stale inactive runs when it starts, and removes its own run
directory when it exits. Read-only checkouts and candidate snapshots use a private hashed workspace
per repository under the user-local system temporary directory. Every consumer repository must ignore its root
`temp/` directory in `.gitignore` (for example, with `/temp/`). Cache staging and atomic replacement
files remain beside their persistent targets. `sync`, `setup`, rollback, confirmed prune,
`format`, and `edit-setup` are explicit mutation commands. An `edit-check` request with
`phase: "write"` applies approved Ruff fixes and formatting only; `phase: "validate"` is
read-only. Omitting `phase` preserves the earlier synchronous writer-and-diagnostics behavior.

1. Run `quality-gate validate` after editing `quality-gate.toml`.
2. Run `quality-gate doctor` to verify the selected release and component runtimes are available.
3. For changes to this repository's source or policy, run the current-source `quality-gate check`
   before committing. For a consumer implementation, complete staged verification through its
   active workflow; its commit-hook Quality Gate satisfies this step. For standalone verification,
   run `quality-gate check` directly. The default report preserves every observed failed and
   unchecked check status while bounding finding details. Known and unknown upstream omissions are
   identified explicitly. Use `--verbose` for every status and all safely collected redacted
   findings.
4. Use `quality-gate format <explicit-path>...`, stage the result, and rerun the current-source
   `quality-gate check`
   for Quality Gate changes or return to the active workflow for consumer changes.
5. The `quality-gate version` command prints the installed engine package version. The repository's
   `quality-gate.toml` selects the policy release; `quality-gate doctor` checks that release and its
   runtimes but does not print its version. `quality-gate setup` prints the selected release when it
   prepares the policy and runtimes.

For this provider, the Python test stage has a 30-minute limit, the complete Gate has a 40-minute
limit, and the reusable CI job has a 50-minute outer limit. The job limit includes bootstrap and
cleanup. Local verification requires an explicit 50-minute aggregate executor budget as well.

The native pre-commit wrapper reads the staged manifest, resolves `quality.policy_release`, verifies
that exact cached release, and calls the Gate without importing mutable repository source. The
wrapper has no separate policy-version pin. If CI reports a v2 release, inspect the
`policy_bootstrap` output and the staged manifest. The bootstrap release is used only when this
repository's selected release API returns 404. The native pre-push wrapper blocks updates and
deletion of the remote default branch when Git can prove its name.

Queue-aware releases admit two independent local full Gate runs across repositories for the
current user. Waiting continues automatically when a slot opens and has a separate configurable
budget. Ruff children default to four worker threads. See [Local Gate queue](local-gate-queue.md)
for settings, nested execution, and the native-wrapper activation requirement. Source changes do
not activate this behavior in an older immutable consumer release.

## Manifest maintenance

For post-edit Ruff checks, use the separate [working-tree interface](edit-check.md). Its explicit
`phase: "write"` applies approved fixes and formatting synchronously to explicit complete
working-tree files, including unmanaged and non-Git projects, without running final diagnostics.
After those writes finish, `phase: "validate"` runs the final lint and format checks without changing
project files; the host may run this phase in the shared bounded background worker. It accepts the
shared 30-second work limit while the writer keeps its 10-second limit. Omitting `phase` preserves
the earlier synchronous writer-and-diagnostics behavior. Each phase returns a versioned JSON result
for the exact selected policy and content. The read-only phase requires a published, verified engine
release that implements it. Publication alone does not install the engine or accept host activation.
`edit-setup` explicitly provisions the selected release's native Ruff; neither phase downloads tools,
installs dependencies or creates runtimes. The supervisor removes its temporary workspace after
completion or timeout. These phases do not replace staged/full Gate verification. Keep the interface
engine identity separate from the consumer policy pin; consult its supported-release matrix before
activation, including when the selected policy predates the command.

For read-only touched-content secrets, use the separate
[edit-secrets interface](edit-secrets.md). It selects repository-wide policy and
exact waivers without a Python runtime, performs no setup/download/history scan,
and requires current raw-byte identities. Its local development seam is distinct
from installed immutable-release activation.

For read-only touched-file conflict markers, use the separate
[edit-integrity interface](edit-integrity.md). It shares the normal Gate's exact
marker and standard-waiver semantics, checks only explicit complete files, and
uses the selected immutable policy without a scanner or Python runtime. The host
must preserve its common post-Ruff deadline and validate every returned identity;
provider publication does not activate or enable the host hook.

For read-only whole-component typing, use [edit-types](edit-types.md). Schema 1
uses the remaining common synchronous deadline. Schema 2 supports an explicitly
owned background invocation with cancellation; the host still owns delivery and
drain. Managed setup prepares the declared component runtime. Unmanaged projects
explicitly prepare their own `.venv` with the exact released mypy and required
dependencies/stubs. Editing performs no provisioning. See the
[provider evidence and activation prerequisites](audits/edit-types-provider.md).

Policy v3.1.0 adds optional `python[].typecheck` (boolean, default true). False
produces an explicit mypy `not_applicable` result in full Gate and edit typing;
it does not disable other component checks. Older selected policy releases reject
this field, even when true. Omitted defaults retain existing runtime fingerprints.

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
preview. A missing release, runtime, tool, Python version, unreadable tree, exceeded scan limit,
timeout, or shallow history is `unchecked`; it blocks completion and requires the recovery
action in the report.
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
Then follow [Release procedure](release.md). Shared preparation preserves the original owner-merged
candidate; product jobs validate its exact source, checks, self-host manifest, lessons, and every
declared artifact in a temporary cache before shared publication. Completed readback skips builds.

Retain the active and preceding policy releases. To recover a bad activation, run
the consumer rollback procedure above: restore the manifest release and reusable-workflow SHA,
then verify the complete candidate. Ruleset changes, consumer pin updates, and rollback remain
human-reviewed operations. Recover an unpublished attempt by its original merged PR number using
the retained envelope and [shared recovery contract](publication.md).
