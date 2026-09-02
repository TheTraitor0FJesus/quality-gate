# Quality policy changes

## Scope

This repository is the source of truth for shared Python quality checks.

- `quality_gate/policy/ruff.toml` defines shared Ruff policy.
- `quality_gate/policy/mypy.ini` defines shared mypy policy.
- `quality_gate/runner.py` defines which checks run and how project manifests apply them.
- `quality_gate/integrity.py` defines repository Git, workflow, and mechanical documentation checks.
- `quality_gate/lessons.py` defines the escaped-defect lesson format and release learning gate.
- `quality_gate/release.py` validates the self-host source and exact release artifact before
  publication.
- `quality_gate/contracts.py`, `quality_gate/reporting.py`, and `quality_gate/migration.py` define the schema 2 manifest, verdict/reporting, waiver, and schema 1 migration contracts.
- `.github/workflows/quality.yml` is the reusable GitHub CI workflow.
- `.github/workflows/parity.yml` is the non-gating Windows/Linux parity workflow.
- `quality-gate.toml` in each consumer repository is the schema 2 contract: repository obligations,
  component metadata, limits, defaults, policy release identity, and typed waivers.

The reusable workflow and local launcher must consume an immutable policy release. CI selects a
platform-specific asset from that release so Windows and Linux receive compatible wheels and
scanner binaries. GitHub release
immutability and the GitHub-reported asset digest are the CI trust anchor; the workflow must reject
a mutable release or a digest mismatch before extraction. `quality_gate/ci_release.py` also checks
the release manifest, every declared artifact digest, safe relative paths, and regular extracted
tool files before chmod. The distribution layer then verifies the release inventory and synchronizes
it before running the CLI gate. The workflow first installs a fixed bootstrap Python, reads all
manifest component versions with `python`, and prepares the resulting multiline setup input. The
parity workflow is dispatch/schedule-only, publishes one machine-readable result per platform, and
compares the release, tools, check surface, history, redaction, and `unchecked` outcomes separately.

The global Git hook is a stable machine-owned wrapper. It reads the staged schema 2 manifest,
verifies the declared policy wheel in the immutable local cache, and launches that exact v2
release. It must not import mutable source from this repository or depend on Codex or an IDE.

## Change procedure

1. Identify whether the change affects a shared policy, the runner, the manifest contract, or CI behavior.
2. Update the source of truth in this repository. Do not add a project-local override to weaken a shared rule.
3. Update focused tests when runner behavior or manifest handling changes.
4. Run the relevant tests and `quality-gate check` from this repository.
5. Run `quality-gate audit` and the release controller before publishing a policy release.
6. Inspect the diff for unintended changes to the shared policy or CI workflow.
7. Commit and push to `main` only after the checks pass.

## Project-specific behavior

Each project may declare Python component paths, existing test directories, and dependency files in `quality-gate.toml`.

Each Python component with dependency inputs receives one `python.component_N.deptry` result. Deptry compares imports with the declared runtime and development dependencies. Custom requirements file names must be classified by `tool.deptry.requirements_files` or `tool.deptry.requirements_files_dev`; an unclassified input is `unchecked`. A component must use either one `pyproject.toml` or classified requirements files because deptry does not analyze both formats together; mixed formats are `unchecked`. A waiver target uses the exact `<path>::<DEP-code>::<module>` form so that dynamic, plugin, entry-point, and configuration-driven dependencies do not suppress unrelated findings. Native deptry ignore, per-rule-ignore, exclude, and inline-ignore mechanisms are rejected because the typed manifest waiver is the only suppression contract. Jupyter notebooks are explicitly outside this Python source contract and are not scanned. Missing tools, missing metadata, and invalid deptry reports are `unchecked`.

Projects may keep pytest markers, test discovery, package metadata, and dependency configuration that describe their own code. They must not keep separate Ruff or mypy policies, or CI jobs that rerun the shared quality tools independently.

## Ruff policy selection

The shared policy uses the stable Ruff 0.16.4 surface. It enables all stable Bugbear rules (`B`),
keeps `C901` at 10 and `PLR0915` at 50, and adds these calibrated rule sets:

- Ruff-native correctness: `RUF006`, `RUF008`, `RUF009`, `RUF012`, `RUF016`, `RUF017`,
  `RUF018`, `RUF024`, `RUF032`, `RUF040`, `RUF043`, `RUF048`, `RUF049`, `RUF053`,
  `RUF059`, `RUF060`, `RUF063`, `RUF064`, and `RUF068`.
- Ruff suppression and configuration integrity: `RUF101`, `RUF102`, `RUF103`, `RUF104`, and
  `RUF200`.
- Security: `S101`, `S102`, `S105`-`S108`, `S110`, `S112`, `S113`, `S201`, `S202`,
  `S301`-`S308`, `S312`-`S319`, `S321`, `S323`, `S324`, `S501`-`S509`, `S601`, `S602`,
  `S604`, `S605`, `S608`-`S612`, `S701`, `S702`, and `S704`.

Only test files under a `tests` directory or named `test_*.py` or `*_test.py` may use assertions
and literal fixture credentials without an `S101` or `S105`-`S107` finding. The equivalent
production patterns remain blocking.

Stable candidates excluded after fleet calibration are:

- `RUF001`-`RUF003`: multilingual product text, comments, and docstrings produce widespread
  confusable-character false positives without evidence of an identifier defect.
- `RUF005`, `RUF007`, `RUF010`, `RUF015`, `RUF019`, `RUF021`-`RUF023`, `RUF026`, `RUF028`,
  `RUF030`, `RUF033`, `RUF034`, `RUF036`, `RUF037`, `RUF041`, `RUF046`, `RUF051`, `RUF057`,
  `RUF058`, and `RUF061`: these request style, ordering, syntax, or micro-optimization changes
  without identifying a fleet safety or correctness invariant.
- `RUF013` and `RUF020`: these overlap the typed-component contract and would enforce annotation
  spelling rather than an additional runtime invariant.
- `RUF100`: its result depends on the complete enabled-rule set and it reported existing
  suppressions for intentionally external or migration-only rules; invalid and redirected
  suppressions remain covered by `RUF101`-`RUF104`.
- `S103`: normal executable permissions such as `0o755` are reported as permissive.
- `S104`: binding a service to all interfaces is a deployment decision and was valid in the fleet.
- `S310` and `S311`: these rules cannot distinguish allowlisted URL schemes or non-security random
  selection, so they require data-flow context that Ruff does not have.
- `S603`, `S606`, and `S607`: they report ordinary argument-list and `PATH`-resolved tool launches
  without evidence of shell expansion or untrusted input; shell and injection cases remain covered
  by `S601`, `S602`, `S604`, `S605`, `S608`, and `S609`.

Preview rules are excluded because their contracts can change. Removed rules are excluded because
Ruff no longer implements them. A diagnostic run with Ruff 0.16.4 against each declared Python
component produced this migration surface after applying the test-only exceptions:

| Repository | New findings |
| --- | --- |
| `quality-gate` | none after local remediation |
| `ARGUS_BRAIN` | none |
| `ARGUS_TG` | `B008` (1), `B904` (1), `B905` (2), `S101` (2) |
| `ARGUS_tracker_multiuser` | `B905` (1) |
| `ARGUS_TRADING` | `S608` (2) |
| `ARGUS_WEB` | `S105` (2) |

## Rollback

Do not replace an immutable release asset or move an existing tag. Publish a corrected patch
release when the shared policy is wrong. For an urgent consumer rollback, restore both the
manifest `quality.policy_release` and the reusable-workflow commit SHA to their last known-good
pair through a feature branch and pull request. The native hook follows the staged manifest and
CI follows the workflow SHA; changing local cache selection alone changes neither contract.
