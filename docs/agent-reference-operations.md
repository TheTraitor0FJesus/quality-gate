# Quality Gate agent reference: maintenance and recovery

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
[interface verification and activation prerequisites](release.md#working-tree-interface-verification).

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
hooks, and the reusable workflow, but does not execute them. The installed test CLI's current
`full` scope validates a manifest and runs pytest only for applicable Python components' declared
`test_paths`. A supplemental runner it does not support makes full verification return an error;
do not claim a partial run as complete or replace it with arbitrary commands. Repository CI remains
the enforcement owner.

After every `quality-gate.toml` edit, run `quality-gate validate`. Use
[templates/quality-gate.toml](../templates/quality-gate.toml) as the canonical shape; do not create
a project-local manifest copy in an agent skill.

For each Python component, set `tests_applicable` explicitly. When it is false, keep
`test_paths` empty and provide a non-empty `tests_reason`; when it is true, declare at least one
`test_paths` entry and omit `tests_reason`. The validator rejects a false setting without a reason.

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

## Test verification

Resolve `CODEX_HOME` from the environment, or use `.codex` under the OS user profile when it is
unset. From a checked Git root, run full verification with:

```text
python <CODEX_HOME>/tools/test_runner/cli.py run --repo <repo> --scope full --json
```

Use `--scope path --value <repository-relative-path>` or
`--scope nodeid --value <repository-relative-nodeid>` for a focused pytest selection owned by one
declared Python component. Use `--scope quality-gate` only when the task or user explicitly requests
the staged Gate; it runs the installed engine and derives the staged snapshot automatically. Read
top-level and command `--help` before use.

Accept a result only when `schema_version`, canonical `repo`, normalized `scope`, and `snapshot`
match the request; `capture_complete` is true; each required stage is `completed` with an exit code
and repository-relative `log_path`; and `candidate.state` is `unchanged`. A run ID, launcher return,
or empty output does not establish completion. Read retained results under the repository's ignored
`temp/` directory and their referenced logs. If a run reports `run_in_progress`, wait for the
existing run to finish before requesting another run for that working copy. If a manifest declares
an unsupported supplemental runner, stop with that error and request an owner-approved CLI
extension. Do not run only a supported subset and call it full verification.

## Consumer release update

After verified publication, `consumer-release-rollout.yml` opens or refreshes consumer pull requests
from `.github/quality-gate-consumers.json`. The registry also lists `quality-gate` with
`activation: "provider"`; `release-rollout.yml` handles that entry through the existing provider
activation route, so the generic consumer job creates no duplicate provider PR.
Consumer PRs update the manifest policy release and every active
Quality Gate reusable-workflow reference together. They also update setup templates such as
`skills/setup-repo/assets/quality-workflow.yml`. Review the complete diff and merge it only after the
consumer checks pass. Rollout PRs are never auto-merged, and the bot never pushes to a default branch.

For a private GitHub Free consumer, required status checks cannot be enforced by branch protection.
Inspect its Actions results manually before merge. See [integration behavior](integrations.md) for the
private-repository limitation and service setup.

If the bot misses a release event, first confirm the release was fully published and verified. Then
run `consumer-release-rollout.yml` manually with the published version and verified helper SHA.
This recovers the rollout for that release and does not publish another release.

Each matrix job obtains a token scoped to the provider and one consumer. Token creation or rollout
failure affects only that target; other jobs still run. Each target validates the publication receipt
and tag before writes, then rejects dynamic or malformed workflow references and missing configured
pins. An existing branch without its managed open PR requires owner recovery and is never overwritten.

Use the following manual procedure only when rollout automation cannot update a consumer. Keep the
consumer on its current release while downloading the target immutable asset:

```text
quality-gate sync --url <target-release-asset-url> --version <target-version>
```

Then change the policy pin and all active caller references in one feature-branch candidate:

1. Set `quality.policy_release` in `quality-gate.toml` to the target release. This selects the
   local wheel, policy files, scanner, and component tools.
2. Set every caller `uses` value for Quality Gate reusable workflows to the same full provider
   commit SHA that contains the compatible workflow implementation. Obtain the SHA from the verified
   release record; do not use a tag or branch in a caller.

Run `validate`, `setup`, and `doctor`, then stage the manifest and workflow together. If an
authorized commit is next, rely on its hook for the staged Gate. Before pushing, run full tests with
the command above, or verify that a separate required CI status runs declared supplemental tests.
The `Quality Gate` status alone does not execute or verify supplemental tests. Push the feature
branch, require the `Quality Gate` pull-request status and any separate required supplemental-test
status, and leave approval and merge to a human. Use a narrower test scope for focused checks.

For rollback, restore both pins to the last known-good pair in a new feature-branch candidate and
run `doctor`; rely on the next commit hook for its staged Gate and run declared supplemental tests
locally or verify their separate required CI status as above. `quality-gate sync --rollback` only changes cache retention state; it does not
override `quality.policy_release` or the workflow SHA, so it is not a consumer rollback by itself.

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
- Repair a failed finding, then rerun the check that reported it when its procedure requires a
  rerun. For a staged Gate failure, retry the authorized commit and use its new hook result.
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

Run the complete declared test suite with the `full` test CLI command above, and run
`quality-gate audit` before building the exact platform artifacts. The release audit remains a
separate required command. Then follow
[Release procedure](release.md).
Shared preparation preserves the original owner-merged candidate; product jobs validate its exact
source, checks, self-host manifest, lessons, and every declared artifact in a temporary cache before
shared publication. Completed readback skips builds.

Retain the active and preceding policy releases. To recover a bad activation, run
the consumer rollback procedure above: restore the manifest release and reusable-workflow SHA,
then verify the complete candidate. Ruleset changes, consumer pin updates, and rollback remain
human-reviewed operations. Recover an unpublished attempt by its original merged PR number using
the retained envelope and [shared recovery contract](publication.md).
