# Architecture flows

## Flows

- **Working-tree edit checking** — the public `edit-check` command and `quality-gate-edit`
  entry point use `edit_cli.py` and `edit_protocol.py` for bounded JSON and one caller deadline;
  `edit_process.py` owns bounded streams and the supervised process tree; `edit_worker.py`
  runs the source-free failure boundary. `edit_inputs.py` captures explicit files and
  configuration freshness; `edit_policy.py` verifies exact compatible releases and explicitly
  provisions native Ruff through `edit-setup`; `edit_check.py` selects only relevant prepared
  runtimes, preserves the legacy synchronous writer-and-diagnostics path, or separates the explicit
  `phase: write` safe-fix/formatting path from the read-only `phase: validate` final checks. Only the
  writer phases publish current bytes.
  Isolated Ruff trees preserve captured parent package-marker presence using empty
  markers; `ImportContext` includes these facts in freshness and reuse identities.
  `edit_reuse.py` stores bounded authenticated complete results; fresh provider selection,
  artifact/configuration checks and `edit_inputs.py` import-layout facts govern reuse.
  This path uses schema validation through `contracts.parse_manifest` without expanding
  supplemental test targets. It creates no Git-index candidate and runs no full checks or setup.
  The supervisor imports the explicit engine directory or wheel and starts the writer in
  its private workspace through `temp_workspace.temporary_workspace`, shared by all edit
  modes under the explicit root's `temp/qg-*`, with a system-temp fallback for read-only
  roots. Active locks protect concurrent runs and allow later stale-run reclamation.
  An archive path is never used as a working directory.
  See [the public protocol and compatibility contract](edit-check.md).

- **Working-tree secret checking** — public `edit-secrets` shares `edit_cli.py`,
  `edit_protocol.py`, `edit_worker.py` and `edit_process.py` transport/ownership
  with Ruff, while retaining its distinct baseline schema and read-only failure
  semantics. `edit_secrets.py` captures complete explicit bytes and root scanner
  configuration, selects the repository-wide exact release without Python
  runtimes, scans an isolated partial tree, resolves typed waivers and checks
  freshness. `secrets.py` owns shared native command flags and bounded in-memory
  report-to-fingerprint parsing for edit, candidate and history scans. Native
  processes start suspended into a Windows Job before running; POSIX uses process
  groups. See [the public secret protocol](edit-secrets.md) for coverage, policy
  compatibility and the historical redacted-fingerprint correction.

- **Working-tree typing** — `edit-types` shares the bounded CLI, protocol, worker
  and process-tree supervisor. `edit_types.py` owns affected-component selection,
  isolated whole-component analysis and source-free diagnostics;
  `edit_type_environment.py` owns declared runtime selection and exact released
  mypy/installed-tool verification; `edit_type_inputs.py` owns bounded source and
  environment inventories and host-owned cancellation signals; `path_safety.py`
  supplies their shared symlink/reparse checks. Schema 1 consumes
  the remaining synchronous deadline; schema 2 binds one supervised background
  invocation to an event and cancellation path. The host owns scheduling,
  replacement, active-turn delivery and Stop/drain. `contracts.py` validates the
  optional `typecheck` opt-out, component-relative `stub_roots`, and policy-gated
  repository-relative `source_roots`; the provider resolves both path sets only
  from captured sources.
  See the [wire contract](edit-types.md)
  and [interface verification requirements](release.md#working-tree-interface-verification).

- **Dependabot review and patch auto-merge.** `.github/workflows/dependabot-review.yml` handles
  non-draft Dependabot PRs targeting `main` from trusted base metadata. Its only job grants
  `pull-requests: write`, requests the repository owner's review, and skips duplicate requests
  when the owner is already requested or has reviewed. It checks out and executes no PR code.
  The exact `.github/workflows/dependabot-auto-merge.yml` template is the other
  Dependabot-specific PR-triggered GitHub token write exception in `quality_gate/integrity.py`. It checks
  non-draft Dependabot bot PRs targeting `main`, obtains metadata with a full-SHA-pinned action,
  and fails closed unless
  every grouped update is an object whose `ghsaId` is exactly the empty string. It requests
  auto-merge only when the action reports the PR's highest update type as semver-patch. Workflow
  findings at that path cannot be waived. It has job-scoped write permissions and does not check
  out or execute PR code. See
  [the quality policy change guide](quality-policy-changes.md) for its required conditions.
  The v2.5.0 rule in `quality_gate/publication_flow.py` interprets a missing `## Release` section as
  `No release` only for Dependabot bot PRs whose complete, stable API file list contains workflow
  YAML files directly under `.github/workflows/`; it checks old names on renames and skips mixed or
  incomplete changes. This read-only path leaves the generated PR description untouched.
  `.github/workflows/dependabot-release-decision.yml` checks out the trusted base revision and
  uses a repository-scoped App token to add an explicit no-release section to Dependabot PRs;
  `scripts/dependabot_release.py` preserves existing decisions and validates current PR metadata.
  The App-originated edit triggers release validation again. The integrity check requires the exact
  approved workflow before a pull-request-target workflow can access the rollout App key.
  `.github/dependabot.yml` assigns issues separately; provider calls here use same-repository
  references, so external-provider update grouping is unnecessary.

- **Working-tree integrity checking** — public `edit-integrity` shares the bounded
  `edit_cli.py`, `edit_protocol.py`, `edit_worker.py` and `edit_process.py` transport.
  `edit_integrity.py` captures the manifest and explicit raw-byte inputs, selects
  the exact immutable policy and calls `git_conflict_marker_results`, which shares
  the full Gate's marker and waiver implementation and returns per-file status from
  one scan. It runs no scanner, runtime, Git command, history scan or full-tree
  enumeration. See [the public integrity protocol](edit-integrity.md) for marker,
  non-text, waiver and host-acceptance boundaries.
- **Gate conflict-marker scan** — the normal repository check reads one candidate file at a time.
  It stops with `unchecked` and the affected path when a file exceeds 8 MiB, the candidate exceeds
  256 MiB of candidate bytes, reading fails, or more than 4,096 markers are found. The public
  `edit-integrity` command has its separate explicit-file limits in
  [the integrity protocol](edit-integrity.md).
- **Local and CI checking** — `quality-gate check` → local admission → `quality_gate/runner.py` starts one `quality_gate/operation.py` work clock before repository discovery and candidate construction → `quality_gate/temp_workspace.py` and `quality_gate/snapshot.py` transfer owned roots → validated manifest and policy/runtime preparation → readiness, scanners, and checks under the remaining operation deadline → one shared bounded cleanup phase → finalized structured verdict and report. Checks cover Git hygiene, Gitleaks, workflow/documentation checks, staged web asset budgets, standalone Biome lint and formatting, Ruff, mypy, deptry dependency hygiene, pytest, and report-only coverage when pinned. Reports retain existing status lines and add actual scope, monotonic timings and executed/reused/not-run disposition; check outcome remains separate. Default reports retain every failed/unchecked result; `--verbose` lists all statuses and available redacted findings. `.github/workflows/quality.yml` verifies and installs the release wheel, syncs the manifest release, prepares declared Python versions, and passes pull-request base/head SHAs; `quality_gate/ci_parity.py` requests verbose output from that installed console entry point so its parser receives the complete status map, then emits the comparison surface; `quality-gate audit` adds full reachable-history secret scanning and complete lessons.
  Provider self-checks also prepare the hash-locked release build backend for real-wheel
  test fixtures; `tests/conftest.py` checks the nested release-test workspace path before any tests
  run; consumer runs keep their own dependency preparation contract. This 180-character bound is
  specific to release workspaces and does not limit consumer checkout paths.
  Snapshot construction batches cached-filter attribute queries and retrieves staged blob data in one
  bounded Git batch. It validates each response, then reconstructs every indexed path and mode.
  `candidate_snapshot` accepts one absolute work deadline and can transfer its owned staging root to
  a registered cleanup owner. The work deadline does not bound teardown.
- **Release and runtime preparation** — `quality-gate sync` → verified immutable release cache → `quality-gate setup` → repository-keyed runtime fingerprint → isolated Python environment; `quality-gate doctor` reports missing prerequisites as unchecked.
- **Project bootstrap** — `$setup-repo` → `quality-gate.toml` and a CI caller → reusable `quality.yml`.
- **Agent verification**: typed test-runner `scope: full` validates the manifest, runs pytest
  for each applicable Python component's declared test paths, and runs declared supplemental
  tests against the working tree. The separate `scope: quality-gate` runs the staged Gate only
  when explicitly requested by the task procedure or user. Without a manifest, ordinary
  complete-suite discovery remains in use.
- **Provider policy activation** — verified publication → `quality-gate-release-published` repository event → serialized provider activation PR → human review and merge. Consumers use the same event through their separate rollout workflow.
- **Owner-merged publication** — `release-validation.yml` validates open default-target PRs through
  `release-prepare.yml`; after owner merge, `release.yml` calls shared preparation to resolve the
  exact final source and save its original candidate envelope → Linux/Windows product jobs build,
  install, audit, and fully test the existing ZIP inventory → `release-publish.yml` verifies actual
  Actions provenance, prepares the exact-source draft, and publishes after readiness. Completed
  readback and no-release outcomes skip product work. Shared publication is serialized without
  cancelling an active publisher; native release immutability is optional.
- **Recovery** — default-branch dispatch by merged PR number → verified retained original envelope
  → current tooling verifies the original source → matching draft resumes missing assets. Completed
  receipt readback survives artifact expiry; unknown historical authorization fails visibly.
