# Quality policy changes

Change shared behavior at its source: `quality_gate/policy/ruff.toml` and
`quality_gate/policy/mypy.ini` define tool policy; `quality_gate/runner.py` and
its `runner_*` modules own check orchestration and execution;
`quality_gate/contracts.py` owns the manifest contract; and
`.github/workflows/quality.yml` owns reusable CI. Keep consumer policy and
workflow pins on their existing immutable releases until an explicit update.

## Change procedure

1. Edit the authoritative policy, runner, contract, or workflow. Do not add a consumer-local Ruff
   or mypy override or duplicate the shared quality CI job.
2. For changes to Dependabot review or auto-merge workflows, inspect the exact
   accepted templates and checks in `quality_gate/integrity_workflows.py`.
   The active legacy route permits only non-draft patch updates without security
   advisories. The provider-only replacement in
   `templates/dependabot-auto-merge.yml` permits verified minor/patch update sets,
   including security fixes. Its reviewed fingerprint is owned by
   `quality_gate/integrity_workflows.py`; altered templates remain rejected.
   Follow the activation order below before replacing the active workflow.
3. Review the diff against the shared policy and workflow. Follow
   [the release procedure](release.md) when preparing a public release.

## Protected Dependabot activation

The replacement is prepared for this provider repository. It is not active merely
because the template or its policy approval is merged. Keep the existing active
workflow until a compatible immutable policy release is qualified and published.
Do not edit an old release or bypass the pinned policy to activate the template.

1. Run the focused legacy workflow tests and
   `tests/test_dependabot_auto_merge.py` through the installed test CLI. Qualify the
   complete CI candidate in its feature branch before release. Performance
   baselines for the separate Gate work remain deferred until the owner's work
   through #72 is merged. Keep pending verification separate from completed checks.
2. Qualify and publish a release that accepts the protected template, following
   [the release procedure](release.md). Advance the provider's policy through its
   reviewed activation route. Keep other consumer pins unchanged.
3. In a subsequent reviewed activation change, copy
   `templates/dependabot-auto-merge.yml` to
   `.github/workflows/dependabot-auto-merge.yml`. Remove the old
   `.github/workflows/dependabot-review.yml` route in that same change. The new
   controller requests owner review for manual updates; leaving both routes
   active would also request review for routine updates. Preserve the independent
   no-release decision workflow.
4. Confirm repository auto-merge is enabled, effective strict rules require
   `Quality Gate`, and the automation App has no protection bypass. Use the
   existing App variables and secret, scoped to this repository with Contents
   and Pull requests write permissions. The controller obtains read-only
   metadata with the built-in token and makes authorized writes with the App
   token so post-merge workflows can receive normal events.
5. Verify the activated route with permitted integration evidence. Cover routine
   and security minor/patch updates, mixed or major groups, unknown metadata,
   stale heads, draft/base changes, missing or failing required checks, and
   downstream push and closed-PR events. Never merge a real PR or dispatch a
   release solely to prove the workflow. Report unobserved live behavior.

The controller executes no repository checkout or PR code. It clears previous
auto-merge authorization on handled updates, verifies every dependency's update
type, re-reads the current PR and effective rules, then supplies
`expectedHeadOid` to GitHub's auto-merge mutation. Required checks and reviews
remain enforced by GitHub. Missing credentials, metadata, protection or head
identity stop authorization. Major and unclassified updates stay manual.

To roll back activation, restore the previous reviewed workflow and review route
in a new change. The new policy retains legacy-template acceptance, so rollback
does not require editing an immutable release or weakening protection.

## Ruff exclusions

The exclusions in `quality_gate/policy/ruff.toml` have calibration reasons that
are not apparent from the configuration alone. Reconsider them with evidence
from the supported repository fleet before adding these rules:

- `RUF001`–`RUF003` produce confusable-character findings in multilingual
  product text, comments, and docstrings without identifying an identifier
  defect.
- `RUF005`, `RUF007`, `RUF010`, `RUF015`, `RUF019`, `RUF021`–`RUF023`,
  `RUF026`, `RUF028`, `RUF030`, `RUF033`, `RUF034`, `RUF036`, `RUF037`,
  `RUF041`, `RUF046`, `RUF051`, `RUF057`, `RUF058`, and `RUF061` request style,
  ordering, syntax, or micro-optimization changes without an additional fleet
  safety or correctness invariant.
- `RUF013` and `RUF020` overlap the typed-component contract. They enforce
  annotation spelling rather than another runtime invariant.
- `RUF100` depends on the complete enabled-rule set and flagged intentional
  suppressions for external or migration-only rules. `RUF101`–`RUF104` already
  catch invalid or redirected suppressions.
- `S103` flags ordinary executable permissions such as `0o755`; `S104` treats
  binding to all interfaces as unsafe even where fleet deployment requires it.
- `S310` and `S311` need data-flow context to distinguish allowed URL schemes
  and non-security random selection.
- `S603`, `S606`, and `S607` flag ordinary argument-list and `PATH`-resolved
  tool launches without evidence of shell expansion or untrusted input. The
  shell and injection rules remain enabled separately.

Preview and removed Ruff rules are excluded because their contracts are
unstable or Ruff no longer implements them. The selected rules and scoped
exceptions remain defined by `quality_gate/policy/ruff.toml`.
