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

For Dependabot review and patch auto-merge flows, the `repository.workflow` check accepts only these
two exact templates:
the existing `.github/workflows/dependabot-review.yml` owner-review template and
`.github/workflows/dependabot-auto-merge.yml` Dependabot patch auto-merge template. The owner-review
template keeps its request-review behavior. The auto-merge template accepts only non-draft
Dependabot bot pull requests targeting `main`. It uses a full-SHA-pinned metadata action and checks
every entry in `updated-dependencies-json`. Each entry must be an object with `ghsaId` exactly equal
to the empty string. Missing, null, malformed, or nonempty values block auto-merge. The merge step
also requires the action's highest `update-type` output for the PR to be
`version-update:semver-patch`, so a grouped PR with a larger update remains manual.
`alert-lookup: true` uses `DEPENDABOT_ALERTS_TOKEN`, which needs read access to Dependabot alerts
and pull requests. It does not need write access. The workflow passes it only to the pinned
metadata action and reads metadata without checking out or executing pull-request code. Its only
write permissions are job-scoped
`contents: write` and `pull-requests: write`. Findings for this auto-merge path cannot be waived.
Any change to the trigger, guard, metadata check, patch condition, permissions, environment, action,
command, secret, or steps requires a separately reviewed policy change. Do not add a broad
exception or modify a cached release to bypass this template rule.

The provider's `dependabot-release-decision.yml` uses the rollout App key on `pull_request_target`
to write a no-release decision. The integrity check accepts only the exact approved workflow and
provider repository for that use of the App key. It checks out the trusted base, verifies the PR
author, and scopes the App token to pull-request writes. Its App-originated edit allows the existing
release validation workflow to run again.

Release validation also accepts an absent `## Release` section as an implicit no-release decision
only when the PR author is `dependabot[bot]` with type `Bot` and the complete, stable GitHub API file
list contains only `.yml` or `.yaml` files directly under `.github/workflows/`. Both current and
previous names are checked, and the API count must match the PR's changed-file count. This route
uses read-only metadata access; non-bot PRs, mixed changes, renames from outside that directory,
incomplete lists, and malformed explicit decisions still fail closed.

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
4. Run the relevant tests and the current-source `quality-gate check` from this repository.
5. Run `quality-gate audit` and the release controller before publishing a policy release.
6. Inspect the diff for unintended changes to the shared policy or CI workflow.
7. Commit and push to `main` only after the checks pass.

## Project-specific behavior

Each project may declare Python component paths, existing test directories, and dependency files in `quality-gate.toml`.
For working-tree typing, a Python component may also declare `stub_roots` as unique component-relative directories
inside its captured tree. This field requires policy v3.2.0 or newer; the edit-types provider hashes their `.pyi`
contents and passes the approved roots to isolated mypy.
It may also declare repository-owned supplemental tests with `[[supplemental_tests]]` entries using
the `node-test` runner and repository-relative targets. The manifest validator checks their
identities, matches, and bounded expansion size; the Quality Gate never executes them. The typed
test-runner expands validated targets without shell expansion and executes concrete paths as part
of its `scope: full` orchestration. Repository CI remains responsible for their normal enforcement.

Each Python component with dependency inputs receives one `python.component_N.deptry` result. Deptry compares imports with the declared runtime and development dependencies. Custom requirements file names must be classified by `tool.deptry.requirements_files` or `tool.deptry.requirements_files_dev`; an unclassified input is `unchecked`. A component must use either one `pyproject.toml` or classified requirements files because deptry does not analyze both formats together; mixed formats are `unchecked`. A waiver target uses the exact `<path>::<DEP-code>::<module>` form so that dynamic, plugin, entry-point, and configuration-driven dependencies do not suppress unrelated findings. Native deptry ignore, per-rule-ignore, exclude, and inline-ignore mechanisms are rejected because the typed manifest waiver is the only suppression contract. Jupyter notebooks are explicitly outside this Python source contract and are not scanned. Missing tools, missing metadata, and invalid deptry reports are `unchecked`.

Each web component declares a repository-relative root plus JavaScript and CSS glob patterns relative to that root. Explicit exclusions are the only way to remove generated or vendor assets from the boundary. The runner measures regular staged files in bytes and emits `web.component_N.javascript_budget` and `web.component_N.css_budget`; default limits are 100/50 KiB per file and 250/100 KiB per component respectively. Missing roots, empty declared matches, unreadable assets, and symbolic-link assets are `unchecked`. The same component emits `web.component_N.biome_lint` and `web.component_N.biome_format` using the exact standalone binary and policy in `quality_gate/policy`; the commands are read-only and pass only JavaScript and CSS assets. Biome's stable recommended rules are enabled by the version-locked 2.2.6 policy, which explicitly enables CSS formatting and accepts files up to the repository's default 5 MiB blob limit; a larger custom web/blob limit produces `unchecked` before Biome invocation. The config has no nursery or experimental rule selection, and no project-wide file discovery, SCSS, or embedded-language inputs are passed. An absent, corrupt, or inventory-mismatched binary is `unchecked`.

Projects may keep pytest markers, test discovery, package metadata, and dependency configuration that describe their own code. They must not keep separate Ruff or mypy policies, or CI jobs that rerun the shared quality tools independently.

## Working-tree policy selection

The additive [edit-integrity interface](edit-integrity.md) shares the normal
Gate's conflict-marker and exact standard-waiver resolver on explicit complete
bytes. NUL and invalid UTF-8 retain the Gate's non-text treatment. Its bounded
touched-file coverage does not expand into a Git or history scan. The normal
Gate marker scan also returns `unchecked` for unreadable or oversized input;
its streaming limits are recorded in the [architecture map](architecture.md).

The additive [edit-types interface](edit-types.md) runs whole affected typed
components against the exact released mypy policy. Its bounded source,
configuration, dependency, runtime and tool evidence must remain fresh at
completion and host delivery. The new policy's optional `typecheck` boolean
defaults to true; false is an explicit mypy opt-out in full Gate and edit typing.
Older selected policies reject the field, including true. No mypy manifest-waiver
semantics are added. Schema 2 background ownership does not alter schema 1's
deadline or authorize installing packages during editing.

The additive [edit-secrets interface](edit-secrets.md) reuses the exact release's
Gitleaks detector and working-tree root configuration on an explicit partial
candidate. Its bounded coverage failures do not redefine full Gate scope. The
shared scanner transport now hashes actual in-memory `Secret` bytes instead of
the historical redacted placeholder and persists no raw report/log. Existing
typed-waiver matching remains exact; placeholder-digest waivers require explicit
review and reissue, without a compatibility alias. Publication/consumer rollout
must carry that correction explicitly; immutable old releases remain unchanged.

The additive working-tree [edit-check interface](edit-check.md) reuses the exact released Ruff
policy and tool without changing ordinary Gate scope or rule selection. Its explicit automatic
fix allowlist contains safe `I001` import ordering and `F401` unused-import removal; Ruff's
`--no-unsafe-fixes` remains enabled, so unsafe removals in `__init__.py` are not applied. Intentional
public re-exports should use a redundant alias or `__all__`. The explicit `phase: "write"` runs only
those fixes and formatting; `phase: "validate"` runs the read-only final lint and format checks after
the writer. Omitting `phase` preserves the earlier synchronous writer-and-diagnostics behavior. The
final validation checks every released enabled rule. Interface-engine updates do not activate new
consumer policy pins, and unsupported pins fail closed rather than falling back to baseline. Host use
of these phases requires an immutable engine release that implements them; keep activation pending
until that release is published and verified.

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
