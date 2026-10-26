# Architecture map

Use this map before non-trivial codebase questions, design work, or code changes. Verify relevant claims in code.

## Navigation

- **CLI and orchestration** — `quality_gate/cli.py` parses commands; `quality_gate/contracts.py` validates schema 2 and defines verdicts; `quality_gate/migration.py` prints read-only schema 1 proposals; `quality_gate/snapshot.py` materializes the read-only Git index candidate; `quality_gate/integrity.py` checks Git hygiene, workflow hygiene, and mechanical documentation health; `quality_gate/lessons.py` validates escaped-defect lessons and release readiness; `quality_gate/runner.py` executes checks and audits against that candidate; `quality_gate/launcher.py` selects a pinned release and prepares consumer runtimes; `quality_gate/hooks.py` exposes the native Git launch and push protocol; `quality_gate/hook_setup.py` installs managed wrappers safely; `quality_gate/release.py` validates a self-host source and exact release artifact before publication and checks the resolved Windows workspace path against its 180-character limit.
- **Verdict reporting** — `quality_gate/reporting.py` summarizes every observed status, keeps every failed/unchecked result visible, and bounds per-check and overall redacted finding details; `quality_gate/contracts.py` carries known and unknown upstream finding-omission metadata.
- **Policy distribution** — `quality_gate/release_contract.py` defines the exact unified-release inventory; `quality_gate/distribution.py` validates release manifests, that inventory, and SHA-256 digests, installs releases through a locked same-volume replacement, quarantines corrupt entries, and retains active/previous selections.
- **Temporary workspace** — `quality_gate/temp_workspace.py` clears stale inactive run directories under the checked repository's ignored `temp/`, routes Python and child-process temporary files into a per-run subdirectory, and removes that subdirectory at exit. Read-only checkouts and candidate snapshots use a private hashed workspace per repository under user-local system temp. Staging beside persistent caches and atomic replacement targets stays beside those targets.
- **Local runtime** — the stable native wrapper reads the staged schema 2 manifest, verifies the declared wheel in the immutable user cache, and launches that exact release without importing mutable repository source; `quality_gate/runtime.py` creates a separate component runtime per repository and fingerprint of policy, component, Python, and dependency inputs.
- **CI integration** — `.github/workflows/quality.yml` is the reusable GitHub workflow boundary; it checks out `job.workflow_repository` at `job.workflow_sha`, bootstraps Python before reading component versions, selects a platform-specific immutable release asset, and uses `quality_gate/ci_release.py` plus `quality_gate/release_contract.py` to verify GitHub release immutability, the GitHub-reported asset digest, the exact unified inventory, every declared release file, and safe extraction paths; `.github/workflows/parity.yml` runs the release-backed Python/web/supplemental fixture on Windows and Linux from dispatch or schedule, uploads machine-readable results, and compares them in a separate job; every external Action is pinned to a full commit SHA, and pull requests pass explicit range refs to the same CLI gate.
- **Project contract** — `quality_gate/contracts.py` is the source of truth for schema 2 repository, Python/web component, supplemental-test, verdict, and waiver models; `quality_gate/policy/biome.json` and `quality_gate/policy/biome.toml` are the pinned web-tool policy and platform inventory; `templates/quality-gate.toml` is the manifest shape and `templates/quality.yml` plus `templates/dependabot.yml` are the consumer CI shapes generated for a project by `$setup-repo`.
- **Learning contract** — `docs/lessons.md` defines the English Markdown lesson format; `quality_gate/lessons.py` validates lessons and provides the audit and release readiness results.
- **Shared publication** — `quality_gate/publication.py` owns PR decisions, version rules, and the bounded lowercase GHCR repository contract; `publication_flow.py` owns source authorization and the publication state machine; `publication_config.py` owns declarative product requirements; `publication_evidence.py` and `publication_artifacts.py` verify attempt-bound Actions provenance and retained envelopes; `publication_github.py` provides bounded GitHub API and registry access; `publication_writer.py` inspects one-image Docker-save-compatible archives and checks their source revision before push. Publisher readback binds image references to the source-configured GHCR package. The prepare, image-writer, and publish reusable workflows call the policy-free `scripts/source_release.py` entry point. Read [the shared interface](publication.md) before changing consumer wiring, evidence, or recovery.
- **Product release** — `.release/version.toml` is the product-version authority; `.release/notes.md` contains reviewed changes; `.release/publisher.toml` declares product projections and required producers. The provider source manifest stays pinned to the latest published policy through candidate verification, while the consumer template projects the product release; the provider activates its new policy after publication. `scripts/release_build.py` drives the existing platform checks; `scripts/release_adapter.py` owns package construction, installation, and isolated runtime/audit validation; `scripts/release_metadata.py` supplies product metadata. Follow [the local release contract](release.md) when preparing a candidate.

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
  `edit_reuse.py` stores bounded authenticated complete results; fresh provider selection,
  artifact/configuration checks and `edit_inputs.py` import-layout facts govern reuse.
  This path uses schema validation through `contracts.parse_manifest` without expanding
  supplemental test targets. It creates no Git-index candidate and runs no full checks or setup.
  The supervisor imports the explicit engine directory or wheel and starts the writer in
  its private same-volume workspace; read-only validation uses a temporary workspace outside
  the project. An archive path is never used as a working directory.
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
  environment inventories and host-owned cancellation signals. Schema 1 consumes
  the remaining synchronous deadline; schema 2 binds one supervised background
  invocation to an event and cancellation path. The host owns scheduling,
  replacement, active-turn delivery and Stop/drain. `contracts.py`, `runner.py`
  and `runtime.py` share the new policy's optional `typecheck` opt-out while
  retaining default-true older runtime identities. See the [wire contract](edit-types.md)
  and [provider preparation evidence](audits/edit-types-provider.md).

- **Dependabot owner review** — `.github/workflows/dependabot-review.yml` handles non-draft
  Dependabot PRs targeting `main` from trusted base metadata. Its only job grants
  `pull-requests: write`, requests the repository owner's review, and skips duplicate requests
  when the owner is already requested or has reviewed. It checks out and executes no PR code.
  The v2.5.0 rule in `quality_gate/publication_flow.py` interprets a missing `## Release` section as
  `No release` only for Dependabot bot PRs whose complete, stable API file list contains workflow
  YAML files directly under `.github/workflows/`; it checks old names on renames and skips mixed or
  incomplete changes. This read-only path leaves the generated PR description untouched.
  `.github/dependabot.yml` assigns issues separately; provider calls here use same-repository
  references, so external-provider update grouping is unnecessary.
  [The v2.4.2 acceptance audit](audits/provider-v2.4.2-acceptance.md) records the immutable
  provider handoff, activation reruns and remaining consumer/live-event acceptance.

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
- **Local and CI checking** — `quality-gate check` → `quality_gate/temp_workspace.py` → `quality_gate/snapshot.py` → `quality_gate/runner.py` → validated manifest → structured verdict → Git hygiene, Gitleaks, workflow/documentation checks, staged web asset budgets, standalone Biome lint and formatting, Ruff, mypy, deptry dependency hygiene, pytest, and report-only coverage when pinned. Default reports summarize observed outcomes and retain every failed/unchecked result; `--verbose` lists all statuses and available redacted findings. `.github/workflows/quality.yml` verifies and installs the release wheel, syncs the manifest release, prepares declared Python versions, and passes pull-request base/head SHAs; `quality_gate/ci_parity.py` requests verbose output from that installed console entry point so its parser receives the complete status map, then emits the comparison surface; `quality-gate audit` adds full reachable-history secret scanning and complete lessons.
  Provider self-checks also prepare the hash-locked release build backend for real-wheel
  test fixtures; `tests/conftest.py` checks the nested release-test workspace path before any tests
  run; consumer runs keep their own dependency preparation contract. This 180-character bound is
  specific to release workspaces and does not limit consumer checkout paths.
- **Release and runtime preparation** — `quality-gate sync` → verified immutable release cache → `quality-gate setup` → repository-keyed runtime fingerprint → isolated Python environment; `quality-gate doctor` reports missing prerequisites as unchecked.
- **Project bootstrap** — `$setup-repo` → `quality-gate.toml` and a CI caller → reusable `quality.yml`.
- **Unified agent verification** — typed test-runner `scope: full` → repository-owned supplemental tests when declared → one staged Quality Gate; without a manifest, ordinary complete-suite discovery remains in use.
- **Policy rollout** — validated `quality-gate` change → immutable release artifact → explicitly synchronized local and CI runtimes.
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
