# Quality policy changes

Change shared behavior at its source: `quality_gate/policy/ruff.toml` and
`quality_gate/policy/mypy.ini` define tool policy, `quality_gate/runner.py` owns
check orchestration, `quality_gate/contracts.py` owns the manifest contract,
and `.github/workflows/quality.yml` owns reusable CI. Keep
consumer policy pins and workflow pins on their existing immutable releases
until an explicit consumer update.

## Change procedure

1. Edit the authoritative policy, runner, contract, or workflow. Do not add a consumer-local Ruff
   or mypy override or duplicate the shared quality CI job.
2. For changes to Dependabot review or auto-merge workflows, inspect the exact
   accepted templates and checks in `quality_gate/integrity_workflows.py`.
   Auto-merge is limited to non-draft Dependabot PRs targeting `main`, with no
   security advisories in any grouped update and only a patch-level update.
   Changes to those guards or permissions need separate policy review.
3. Review the diff against the shared policy and workflow. Follow
   [the release procedure](release.md) when preparing a public release.

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
