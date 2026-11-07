# Connect a consumer repository

Use this guide to add Quality Gate to a repository or update an existing consumer. The manifest
template and reusable workflow template hold the setup shape; code and workflows remain the source
of truth for command behavior.

## Add Quality Gate

1. Copy [the manifest template](../templates/quality-gate.toml) to `quality-gate.toml`. Replace
   `REVIEW_REQUIRED`, declare the repository's applicable domains and required documents, and add
   each Python component with its dependencies, test paths, and explicit test applicability.
2. Copy [the workflow template](../templates/quality.yml) into `.github/workflows/quality.yml`.
   Replace its placeholder with the full 40-character commit SHA for the reusable workflow. Keep
   the workflow permissions read-only unless the consumer has a separately justified need.
3. Prepare and check the consumer from its repository root:

   ```text
   quality-gate --root <consumer> validate
   quality-gate sync --source <release-archive> --version <policy-release>
   quality-gate --root <consumer> setup
   quality-gate --root <consumer> doctor
   ```

   `sync --url` is also available when downloading a release is intended. Network use belongs to
   this explicit sync step. Use a published immutable policy release; the selected policy release
   and Quality Gate workflow SHA must form a compatible pair.
4. Review the first pull request's Quality Gate result and set the repository's merge protections
   according to the hosting plan. For a private GitHub Free consumer, the workflow reports that
   default-branch push detection is not server-side enforcement. Review Actions results manually
   before merging a pull request.

## Update or roll back a consumer

A consumer has two independent pins. `quality.policy_release` in `quality-gate.toml` selects its
immutable policy and tools. Each reusable Quality Gate workflow call uses a full provider commit
SHA that selects the workflow implementation. Update the policy release and every active Quality
Gate workflow SHA together in one reviewed consumer change. A policy change alone does not update
the CI workflow, and a workflow change alone does not select a new policy.

After a published release, the consumer rollout workflow can open or refresh a pull request for
registered consumers. Review the complete diff and checks, then merge it as a human-reviewed change.
If rollout automation missed the release, the provider's `consumer-release-rollout.yml` can be run
with the published version and verified helper SHA. See [rollout behavior](../.github/workflows/consumer-release-rollout.yml)
and [the consumer registry](../.github/quality-gate-consumers.json) for the configured targets.

To roll back, restore both pins to the last known-good policy and Quality Gate workflow SHA in a
new consumer change. Run `validate`, `setup`, and `doctor`, then verify the resulting CI candidate.
The local `sync --rollback` command changes cache selection only; it does not change either
repository pin.

## Migrate a schema 1 manifest

`quality-gate migrate` prints a schema 2 proposal without editing files. Review the proposal and
apply the intended changes to `quality-gate.toml` yourself. Then run `validate`, `setup`, and
`doctor` for the consumer. Run `audit` only when the task or applicable instructions separately
require it.

## Local tools and CI pins

`quality-gate version` reports the installed engine package version. The consumer manifest selects
the policy release, and each workflow call selects the CI implementation by its full commit SHA.
Updating a machine's local CLI installation or policy cache does not update either repository pin.
Keep local tool maintenance separate from consumer rollout changes.

For command meanings and recovery, see [Operations](operations.md). For Quality Gate's own product
release procedure, see [Release procedure](release.md).
