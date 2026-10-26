# Edit integrity provider preparation

## Ownership and scope

The provider portion supports `python-quality-hooks` ticket 03. Its public
contract is [edit-integrity](../edit-integrity.md). The implementation reuses
the Gate's marker and waiver logic through `git_conflict_marker_results` and only
reads an explicit bounded set of complete working-tree files. The host owns event
attribution, the final post-Ruff snapshot, feedback, registration and rollback.

This record describes the v3.2.0 candidate that added `edit-integrity`; published
v3.1.0 had delivered `edit-types`. During candidate verification, the provider source
remained pinned to v3.1.0. The later v4.1.0 self-policy activation is recorded in
[the provider v4.1.0 acceptance record](provider-v4.1.0-acceptance.md). The provider
selection binds an immutable policy pin; engine and policy versions remain distinct.

## Acceptance map

| Contract | Implementation seam | Evidence |
| --- | --- | --- |
| Same marker and exact waiver semantics | `integrity.git_conflict_marker_results` | Public CLI compared with normal Gate results for managed files |
| Explicit paths, non-Git baseline and nested files | `edit_inputs.sources`, `edit_integrity.execute` | Public CLI fixture; no Git enumeration |
| NUL and invalid UTF-8 behavior | Shared Gate helper | Binary and undecodable inputs preserve normal Gate skip semantics |
| Release and manifest freshness | `edit_policy.select_release`, `Configuration` | Immutable v2.4.2 fixture; public CLI corrupt-policy and worker post-analysis release-identity cases return unchecked with no partial coverage |
| Bounded source-free worker result | `edit_protocol`, `edit_worker`, `edit_cli` | Public CLI asserts no stderr and no workspace residue |
| Host JSON/exit route and post-Ruff event seam | `file_integrity.check`, `provider_bridge`, coordinator, and candidate public CLI | `tests/test_edit_integrity_host.py` covers managed/unmanaged conflict and clean results, malformed input, post-Ruff final bytes, rename destination and deletion omission; native-wheel activation is substituted and the coordinator fixture controls Ruff's write and the unrelated legacy result |
| Shared transport and packaged CLI | `edit_cli`, `edit_protocol`, `edit_worker` and wheel entry point | Integrity rejects typing's schema 2; real typing schema 2 still binds events; the built wheel exposes both commands without extraction |
| Installed native release and live host activation | Native launcher and registered Codex events | Pending owner merge, immutable publication, same-release installation and real Codex event acceptance |
| Aggregate hook latency and rollback | Host coordinator/install route | Pending host verification after provider publication |

## Development evidence

The host feedback regression was first red in
`integrity-host-dedupe-red-20261021-m`: repeated Integrity feedback hid a new
types finding. The candidate now disables compaction whenever a different
mechanism has a failed result. The follow-up request
`integrity-host-dedupe-green-20261021-n` passed with 13 tests and 8 subtests.
The Integrity-only repeat test also verifies that the failed status and
location remain visible.

The provider host-boundary regression now captures `sys.stdout.buffer` through
a byte-backed text stream and expects all conflict locations in its fixture
(lines 1, 3 and 5). It exercises the installed host JSON entrypoint and provider
bridge against this checkout's public `quality_gate.cli.main` and integrity
implementation. The immutable native-wheel launcher is substituted while
release `v3.2.0` remains unpublished. The coordinator case separately simulates Ruff's
post-write bytes, checks the rename destination and omits the deleted path; the
test also covers malformed input and source-free feedback.

The provider scanner streams one file at a time, limits each file to 8 MiB, the
aggregate candidate to 256 MiB and findings to 4,096, and returns `unchecked`
with the affected path on read or limit errors. The public route maps its
finding limit to the explicit `limits` recovery reason. The earlier Spec gaps
remain corrected: the consumer template matches v3.2.0, installation checks
only selected mechanisms, recovery reasons reach feedback, and the Linux path
uses platform-safe reparse-flag lookup.

Provider request `integrity-provider-full-final-20261021-j` was red before the
last corrections: four files needed formatting and Ruff reported C901 in
`_git_conflict_result`. The implementation extracted bounded per-file reading
and streaming into helpers, then formatted the affected source. The later
targeted host request `integrity-provider-host-seam-targeted-20261021-l` did not
reach behavior assertions because its environment had Ruff 0.15.12 while the
fixtures require 0.16.4. Request `integrity-provider-fixed-full-20261021-o`
returned `incomplete_execution`; its still-running Gate process tree was
terminated. Request `integrity-provider-fixed-full-20261021-p` failed to start
any stage. The pre-review candidate passed both manifest preflight and its
staged Quality Gate in `integrity-provider-fixed-full-20261021-q`; the manifest
declared no supplemental tests. After the standards follow-up removed the
duplicate marker scan, correction request
`integrity-provider-corrected-full-20261022-a` timed out in the staged Gate
after a successful preflight, and request
`integrity-provider-corrected-full-20261022-b` failed before launching a stage.
Both were runner errors, not product verdicts. Fresh request
`integrity-provider-corrected-full-20261022-c` passed manifest preflight and the
staged Quality Gate; the manifest again declared no supplemental tests.

## Continuation evidence (2026-10-23)

The recovered dirty candidate matched Git tree
`8c01995f78452b3ac627006c1b718016facc9a93`, exactly the tree of the retained
historical commit `da4ffa62fc46c39d6cb27fbfe501aadd445a9fb1`.
No candidate bytes were discarded to obtain a clean checkout. The reviewed
pre-integration tree `f70ddef0241758773bddf9d92d899237c668f7be` was then saved
as `abbb7516c57d87d76eb0e78410be64d5249fb1e0` with the ordinary hook enabled.
Published v3.1.0 already contains typing, so this feature prepares the next
normal minor version, v3.2.0, while preserving all current `main` behavior.

The historic `integrity-provider-final-full-20261022-h` failure occurred at
the first fresh Ruff invocation in the prerequisite test, before the missing
Python mutation. Its standalone follow-up used a different launcher/scope;
11.34 seconds covered the entire test's multiple invocations, not one edit's
deadline. The retained component runtime metadata selected policy v2.5.0,
Python 3.12 and the historical `pyproject.toml` SHA-256
`140d4031097ce787bbd25b57b62c4f81406b6fd61f90cc2c3bafaae51332c078`.
That digest matches the recovered candidate. Current inspection
confirmed the same declared Ruff 0.16.4, mypy 2.3.1, pytest 9.1.1 and deptry
0.25.1 versions. Preparing version 3.2.0 changed the dependency-input fingerprint;
merging published policy v3.1.0 required its own fresh setup and readiness check.
Historical timings are not a benchmark: the reported 430-second Gate record is
shorter than its reported 446.56-second pytest duration.

Accepted request `integrity-provider-preserved-full-20261023-j` ran manifest
preflight and the staged source Gate with zero supplemental declarations;
both passed and the Gate process took 389,531 ms. The first checkpoint hook
later hit the same explicit deadline category in a different existing Ruff
test, with 723 passed, 20 skipped and one failed. A complete ordinary hook
retry passed on the unchanged tree. These observations did not establish a
deterministic provider defect; neither product fixes nor a longer edit deadline
were introduced to turn the failures green. Earlier orchestration errors in
requests `...-g`, `...-h` and `...-i` were retained as errors and not accepted
as successful full verification.

The accepted Spec finding about release freshness was closed by
`test_corrupt_release_discards_public_batch` and
`test_release_change_before_delivery_discards_worker_batch`.
The latter changes valid inventory bytes after analysis; actual selection,
freshness verification and worker error handling remain real. Both cases
require exit 2, an explicit unchecked reason and empty partial coverage.
Initial Standards and Spec follow-ups certified the pre-integration tree.

During integration, request `integrity-provider-main-route-red-20261023-k`
first proved the missing public route in `main`'s CLI. After shared transport
integration, request `...-l` exposed a stale host-fixture expectation:
the real host returns empty `findings` on malformed input. Only that expected
JSON was corrected; host implementation and provider-call suppression remained
unchanged. Request `integrity-provider-main-modes-green-20261023-m` then passed
all 25 selected cases, with no skips. They include all four real host-boundary
fixtures, the built wheel, marker/waiver/non-text parity, source/release freshness,
Ruff and secrets compatibility, and typing's background event identity.

Full Gate uses a sanitized environment with a temporary user profile and no
`CODEX_HOME`; installed-host fixtures consequently skip there. The separate
targeted host run is required evidence and does not replace the final full Gate.
Its 31 production Python files under `CODEX_HOME/hooks/{file_integrity,_shared,python_quality}`,
excluding tests and bytecode caches, have aggregate SHA-256
`ff9583bab19ceef8ff76b09f4e6a5f178dd080e2aeeceab7e02c81d3daf3c0db`.
This is SHA-256 of compact, sorted-key JSON mapping hook-relative paths to raw
file SHA-256 values; it binds development sources, not installed release acceptance.

For final provider verification only, the owner approved an isolated typed
executor using Codex `gpt-6.1-sol`, `reasoningEffort=high`, with an outer
1,500,000 ms orchestration deadline. This covers `main`'s existing 900-second
pytest and 1,200-second Gate budgets plus cleanup. Global runner configuration,
request/result schemas, stage completeness, identity checks, lifecycle and
read-only execution rules remain unchanged. The public synchronous edit
deadline remains ten seconds; typing's separately selected background contract
also remains unchanged. The final default full result and fresh Standards/Spec
pair against the fixed `main` base are recorded with the PR's final candidate
identity; the pre-integration checkpoint alone does not certify that merge.

These checks certify the staged source candidate only. Immutable publication,
same-release installed-source parity, real clean/conflict/fix Codex events,
the four required latency profiles, disable/rollback/reactivation and final
identity/regression/cleanup evidence remain pending. Final Standards and Spec
review results accompany the implementation handoff. Keep ticket 03
`in-progress` and the host mechanism disabled until those live acceptance cases
pass.
