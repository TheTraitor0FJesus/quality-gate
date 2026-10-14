# Test-proof repairs

## Problem Statement

Nine confirmed audit findings show that selected tests can remain green after a
meaningful regression, miss a producer-to-consumer boundary, or write into shared
source fixtures. A maintainer needs a passing suite to distinguish the intended
guard or delivery contract from an unrelated failure or a fixture-supplied result.
The audit established weaknesses in regression proof, not current product defects.
Supplemental performance work established two temporary-resource defects and a
repeatable full-suite duration slightly above the configured pytest deadline.
See [performance evidence](performance-evidence.md) for measured results,
reproduction conditions and the limits of causal attribution.

## Solution

Strengthen the original nine proof groups and add four bounded performance and
reliability groups. Preserve supported verdicts, independent integration
protection, failure diagnostics, and deliberate temporary-workspace locking.

This specification is the technical source of truth. All source paths are relative
to the Quality Gate repository root; links are relative to this document. Ticket
IDs match the implementation groups below. The user approved the nine-ticket
breakdown and subsequently requested these performance additions. Tickets are
published under `issues/` according to the
[local tracker procedure](../../docs/agents/issue-tracker.md).

| Group | Audit finding | Outcome | Ticket |
| --- | --- | --- | --- |
| 01 | TST-01 | Exact secret exceptions and isolated admission rejections | [01](issues/01-secret-waivers.md) |
| 02 | TST-02 | Inner archive checksums rejected independently of outer identity | [02](issues/02-ci-member-integrity.md) |
| 03 | TST-03 | Preparation produces the actual candidate and Actions handoff | [03](issues/03-preparation-handoff.md) |
| 04 | TST-04 | Image-writer CLI forwards the exact approved identities and path | [04](issues/04-image-writer-arguments.md) |
| 05 | TST-05 | Biome guards are observed before any forbidden execution | [05](issues/05-biome-guards.md) |
| 06 | TST-06 | Real subprocess-result classification distinguishes unchecked from failed | [06](issues/06-pytest-classification.md) |
| 07 | TST-07 | Ambiguous history refs refuse scanning for the intended reason | [07](issues/07-history-ambiguity.md) |
| 08 | TST-08 | The actual workflow producer emits every declared Python version | [08](issues/08-workflow-python-versions.md) |
| 09 | TST-09 | Runner tests leave source fixtures unchanged | [09](issues/09-fixture-isolation.md) |
| 10 | PERF-01 | Source-wheel copies exclude active scratch without weakening builds | [10](issues/10-source-copy-isolation.md) |
| 11 | PERF-02 | Timed-out commands release descendant resources before cleanup | [11](issues/11-process-tree-cleanup.md) |
| 12 | PERF-03 | Measured setup cost determines whether immutable reuse is safe and worthwhile | [12](issues/12-cli-infrastructure.md) |
| 13 | PERF-04 | Complete coverage and resource recovery determine the time budget | [13](issues/13-complete-verification.md) |

Groups 01–12 can deliver without a later group. Shared files do not create
acceptance dependencies. Group 13 requires 09–12: final performance evidence
must include fixture isolation, scratch-copy protection, complete timeout cleanup,
and the measured infrastructure decision. Group 06 and group 09
touch one common declaration but protect different contracts.

## User Stories

1. As a maintainer, I want a secret waiver to suppress only its reviewed credential,
   so a new credential or invalid approval cannot silently inherit an exception.
2. As a CI maintainer, I want tests to reject inconsistent archive members before
   extraction, so a valid ZIP identity cannot conceal missing inner verification.
3. As a release maintainer, I want preparation tests to inspect the actual candidate
   file and Actions outputs, so retention and product-job handoff stay connected.
4. As a release maintainer, I want the writer CLI to forward the requested PR,
   candidate and receipt directory, so the approved release reaches the right owner.
5. As a policy maintainer, I want Biome negatives to identify their own guard,
   so a tool-launch error cannot hide missing size or integrity validation.
6. As a gate user, I want unavailable pytest collection to remain unchecked while
   an ordinary assertion failure remains failed, so recovery guidance stays correct.
7. As a security maintainer, I want ambiguous history references rejected before
   scanning, so the gate cannot silently select a different commit range.
8. As a multicomponent consumer, I want CI to prepare all declared Python versions,
   so each component has its required runtime.
9. As a contributor, I want successful tests to preserve source fixture contents,
   so verification leaves my checkout clean and repeated runs remain isolated.
10. As a contributor, I want a source build to copy only source inputs while a
    Gate workspace is active, so scratch files cannot break or inflate the build.
11. As a gate user, I want a timed-out command's descendants stopped before
    cleanup, so a failed check cannot leave locks that break later checks.
12. As a maintainer, I want repeated immutable setup reduced with independent
    integrity proof, so speed improvements retain the original test contracts.
13. As a contributor, I want complete isolated runs and a passing Gate on the
    same final candidate, so the timeout has a measured margin without dropping
    slow or environment-dependent proof.

## Implementation Decisions

Implement test changes using existing seams. Group 11 additionally repairs the
runner's process-lifecycle implementation; it must preserve the supported
timeout/verdict contract. No persisted format, dependency, policy pin or
reusable-workflow behavior change is planned. A manifest timeout adjustment is
only a separately justified outcome of group 13, never the sole remediation.
Test-owned helpers belong to the slice they enable; no prefactoring-only ticket is
needed. Before starting a group, read [repository instructions](../../AGENTS.md),
the [architecture map](../../docs/architecture.md),
[coding standards](../../docs/coding-standards.md), and the
[agent reference](../../docs/agent-reference.md). Read the
[publication contract](../../docs/publication.md) for groups 03/04 and the
[quality policy guide](../../docs/quality-policy-changes.md) before any change to
shared behavior. Group 11 is the separately scoped confirmed product defect;
other test repairs must not redefine supported production behavior.

### 01 — Exact secret waivers

**Evidence:** The original `test_exact_fingerprint_waiver_applies_to_one_candidate_finding`
(`tests/test_secrets.py:103`) supplied one exactly matching finding and checked
only WAIVED. Returning true instead of comparing fingerprints in `Waiver.matches`
(`quality_gate/contracts.py:184`) would still satisfy that proof. The composite negative
`test_invalid_typed_waiver_is_rejected_without_secret_value`
(`tests/test_contracts_cli.py:681`) includes unknown `value`, wildcard target, bot
approver and no fingerprint. `_parse_waiver` (`quality_gate/contracts.py:444`)
rejects the unknown field before the later guards; their removal is unobserved.
`_apply_waivers` (`quality_gate/secrets.py:158`) consumes the actual matching result.
Existing deptry waiver tests protect standard targets, not secret fingerprints.

**Change:** Keep the unknown-field/redaction case and add a one-finding positive
control that reaches WAIVED. Derive isolated invalid rows
from a valid waiver: wildcard target, bot approver, expired `expires_on`, future
`reviewed_on`, and malformed fingerprint. Observe each specific admission reason.
Use a controlled current date or dates relative to the test's current date. At the
existing fake-scanner seam, provide distinct secrets at the same target; the exact
reviewed fingerprint is waived and the other finding keeps the result FAILED.
The parser checks fingerprint shape; secret identity belongs at the scanner-result
boundary. Inspect surviving findings and redacted compact/verbose rendering.

**Preserve:** Valid waiver acceptance, unknown-field rejection, raw-secret
redaction, standard-waiver mixed-finding tests, and current date semantics.

**Proof and completion:** The one-finding positive control reaches WAIVED; each
isolated negative reaches its named guard. A
fingerprint-ignoring mutation and the individual parser-guard removals must make
the corresponding new cases red. Restore each mutation exactly. Focused suites:
`tests/test_secrets.py` and `tests/test_contracts_cli.py`, selecting waiver cases.
Attribution retained from A1-03.

### 02 — CI member integrity before extraction

**Evidence:** `_archive` and `_unified_archive` (`tests/test_ci_release.py:29,87`)
derive member digests from the bytes stored in the archive. The metadata negative
at `tests/test_ci_release.py:336` tests outer digest/immutability. None supplies
correct outer identity with a mismatching declared member. Bypassing the comparison
in `_extract_verified_archive` (`quality_gate/ci_release.py:144`, comparison at
line 171) therefore survives. Distribution tests execute a different verifier.
The quality workflow installs the returned wheel before cache sync.

**Change:** Use an otherwise valid exact-inventory archive and independently
invalidate one declared wheel, policy file or native tool member while retaining
its recorded digest. Compute GitHub metadata's outer digest from the final ZIP.
Assert the specific member-integrity error, no extraction into a new target, and
unchanged sentinel content in an existing target. Reuse current archive helpers;
parameterize only cases sharing the same seam and assertion shape.

**Preserve:** Outer archive identity, immutable-release trust, inventory, safe
paths, resource limits and valid extraction controls.

**Proof and completion:** Disabling only the inner digest comparison must make
each member-class negative red; corrected members pass the existing verifier.
Restore exact source. Focused suite: `tests/test_ci_release.py`.
Attribution retained from A1-04.

### 03 — Preparation envelope and Actions output handoff

**Evidence:** `test_post_merge_preserves_original_candidate_before_product_work`
(`tests/test_publication_flow.py:437`) observes only the in-memory result.
`GitHub.persist` at line 133 and `_ready` at line 512 manufacture the retained
artifact. The release-build fixture independently writes its candidate/digest.
No successful prepare/recover CLI path observes `_outputs`
(`quality_gate/publication_cli.py:101`). Dropping candidate persistence,
`save-required`, or a correct digest leaves those fixtures unaffected.
`release-prepare.yml` consumes the real file and output mapping for upload.

**Change:** Add a hermetic successful preparation CLI test with real `_main`,
`_outputs` and a controlled Publisher/GitHub service. Use temporary event,
candidate-directory and `GITHUB_OUTPUT` paths. Compare candidate bytes to an
independently specified canonical envelope, then verify their SHA-256, original
run identity, artifact name and `save-required=true`. Assert no leftover `.tmp`.
Cover reused candidate/recovery handoff and validated/no-release outcomes at the
same seam: existing IDs are forwarded and no fresh envelope is requested where
the current contract forbids one. Read the real generated outputs instead of
authoring a second transfer fixture. The exact test-host file is a local choice
within the existing publication suites; reuse their controlled service.

**Preserve:** Direct state-machine, attempt-bound provenance, retry, conflict,
completed-readback and build-evidence tests. The current file format and output
keys remain authoritative; no network or actual publication is needed.

**Proof and completion:** Removing persistence, changing candidate digest/run
identity, suppressing the upload flag or changing the filename must make the
corresponding handoff assertion red. Restore source. Focused suites:
`tests/test_publication_flow.py`, `tests/test_publication_github.py` and
`tests/test_release_build.py`. Attribution: A1-06 and A2-01.

### 04 — Image-writer argument forwarding

**Evidence:** `test_image_writer_cli_passes_the_original_pull_request_number`
(`tests/test_publication_github.py:939`) uses a double that ignores PR, candidate
ID and output directory, then checks its fixed successful status. Wrong values at
the CLI call (`quality_gate/publication_cli.py:166`) would survive. Direct writer
tests bypass that call; workflow source checks do not execute the Python dispatch.

**Change:** Strengthen this existing double to record exactly one invocation and
assert the full tuple `(7, 9, tmp_path)`. Retain return-code and stdout assertions.
These identities and the receipt path are the observable public handoff, rather
than an incidental private call shape. No additional seam is needed.

**Preserve and proof:** Direct writer identity validation and workflow pin/input
checks remain. Independently passing a wrong PR, candidate ID or output directory
at the dispatch must turn the existing strengthened test red. Restore source.
Focused suite: `tests/test_publication_github.py`, selecting image-writer CLI.
Attribution: A1-01 and A2-02.

### 05 — Biome guard-specific rejection

**Evidence:** Size and corrupt-binary negatives (`tests/test_runner.py:743,772`)
check only UNCHECKED. `_web_policy` at line 557 writes non-executable fixture bytes.
Removing the size guard in `_biome_asset_paths` (`quality_gate/runner.py:528`) or
digest comparison in `_biome_command` (line 490) still results in an unrelated
launch error and the same statuses. Positive routing tests replace command
selection, so they do not independently prove the checksum guard.

**Change:** Keep real selection/integrity/asset checks. Replace only the existing
downstream execution seam with a successful call recorder, assert the exact
rejection diagnostic and zero forbidden executions, and add corrected-digest and
at-limit controls that reach the recorder. Do not replace `_biome_command` in the
checksum case. The proposal uses existing monkeypatch seams, no runnable fake
native binary and no production injection export.

**Preserve and proof:** Missing-tool rejection and real subprocess asset/argument
tests remain. Bypassing either targeted guard must make its negative red, while
the relevant positive reaches downstream execution. Restore source. Focused
suite: `tests/test_runner.py`, selecting Biome cases. Attribution: A1-02.

### 06 — Pytest result classification

**Evidence:** The collection cases (`tests/test_runner.py:427` and
`tests/test_cli_python_contract.py:761`) replace `runner.run` and supply unchecked
themselves. They prove downstream routing/redaction, not the classification in
real `run` (`quality_gate/runner.py:805`, marker detection at line 846). Removing
`pytest_collection_error` from exit-code selection leaves those assertions green.

**Change:** Add focused cases using the existing controlled process/BytesIO seam
with the real `run`: collection output markers `ERROR collecting`,
`no tests collected`, and `no tests ran` at representative exit 2/5 produce
unchecked. An ordinary assertion failure remains failed, and a non-pytest command
does not gain pytest-specific classification merely from matching text. Keep
existing CLI propagation, redaction and test-path assertions. Clarify the old
runner test name only alongside retaining its actual unchecked behavior.

**Proof and completion:** Disabling marker classification makes the new pytest
cases red without changing the ordinary-failure control. Restore source. Focused
suites: `tests/test_runner.py` and `tests/test_cli_python_contract.py`, selecting
classification/collection cases. Attribution: A2-03.

### 07 — Ambiguous history reference refusal

**Evidence:** `test_history_requires_an_unambiguous_base`
(`tests/test_secrets.py:146`) uses a non-Git directory, missing base, unavailable
head/scanner and only checks unchecked. First-success selection instead of the
distinct-commit guard in `_resolve_commit` (`quality_gate/secrets.py:251`, guard
at line 287) would still satisfy it. Other history tests stub one SHA or use
explicit unambiguous SHAs. The actual owner is `secret_history_result` at line 313.

**Change:** Use a disposable Git repository with valid HEAD and distinct commits
for `refs/heads/topic` and `refs/remotes/origin/topic`. Provide a valid controlled
scanner boundary, assert the ambiguity diagnostic and no scanner invocation.
Add a same-commit/unambiguous positive control that reaches scanning. Keep the
missing-base refusal as a separate case. Prefer existing Git fixture patterns;
no live remote and no new resolver export are needed.

**Preserve and proof:** Missing/unresolvable base, shallow-history and redaction
controls remain. Replacing distinct-commit refusal with first-success selection
must make the ambiguous case red for its named reason. Restore source. Focused
suite: `tests/test_secrets.py`, selecting history cases. Attribution: A2-04.

### 08 — Execute the workflow Python-version producer

**Evidence:** `test_workflow_bootstraps_before_reading_a_multicomponent_manifest`
(`tests/test_ci_workflow.py:314`) computes versions inside the test and checks
workflow ordering/substrings. Replacing the real selection in
`.github/workflows/quality.yml`, step `Read declared Python versions` (line 56),
with a fixed one-version list leaves those assertions intact. The existing
release-backed/parity fixtures do not execute this multiversion producer.

**Change:** Execute the actual step's inline Python block against temporary
manifest and `GITHUB_OUTPUT` paths, then parse its multiline record and compare
to independently specified sorted distinct versions. Reuse the test-owned
actual-workflow-script pattern in `tests/test_integrity_dependabot_review.py`.
Keep ordering, setup-python input, override/fallback and immutable pin assertions.
Proposed boundary rows: two distinct versions, a duplicate version, no Python
components, and malformed version refusal without a versions record. These are
the current producer contract, not new workflow behavior. Extraction belongs in
test code; adding a YAML/runtime dependency or copying the producer algorithm is
unnecessary.

**Proof and completion:** A fixed single-version result or truncated multiline
record must make the observed-output test red. Restore source. Focused suite:
`tests/test_ci_workflow.py`, selecting bootstrap/version cases; retain its broader
release-backed protection. Attribution: A2-05.

### 09 — Isolate runner fixture ownership

**Evidence:** Eight declarations in `tests/test_runner.py` at lines
157, 273, 326, 381, 401, 427, 476 and 503 pass shared fixture roots to
`runner.check`. Candidate snapshot/inner temporary-directory doubles do not
intercept `_run_snapshot`'s outer real workspace (`quality_gate/runner.py:1911`).
`_create_run_directory` and `_remove_workspace`
(`quality_gate/temp_workspace.py:147,246`) deliberately retain the root lock.
The audit's clean checkout gained five fixture `temp/` roots, each with a one-byte
`.quality-gate.lock`, under `missing-test-path`, `multi-component`, `no-python`,
`python-tests` and `valid`. Root `/temp/` ignore does not hide these nested roots.

**Change:** Copy immutable source inputs to function-scoped `tmp_path` before
these calls; retain real outer workspace behavior and each current result,
continuation and routing assertion. Reuse the disposable-fixture ownership pattern
in `tests/test_cli_python_contract.py:72`. Copy only fixture inputs, excluding
generated temp/bytecode state. Pure fixture reads need no change. A test-owned
copy helper may accompany this slice; another ticket is not a prerequisite.

**Preserve and proof:** Keep production root-lock lifetime and active-process
synchronization, plus dedicated real workspace lifecycle tests. Record original
fixture contents, run the affected cases twice, and verify no source-fixture path
or content changed. Check the same property after combined verification; compare
against the user's pre-run state and clean up only run-owned artifacts. Before
changing ownership, capture focused timings under the applicable coding-standard
rule and classify operations over two seconds. No speed-up is claimed. Focused
scope: the eight named runner declarations. Attribution: A1-05 and A2-06.

The performance investigation reproduced these five generated lock roots in two
complete runs. Record focused setup/call/teardown timing before changing ownership,
exclude generated `temp/` and bytecode from disposable copies, and do not replace
the outer workspace with a double to obtain a faster result. This ticket protects
isolation; group 12 owns the optimization investigation and group 13 owns complete
verification and time-budget acceptance.

### 10 — Exclude active scratch from source-wheel copies

**Evidence and owner:** `tests/test_ci_workflow.py::_build_wheel` excludes the old
`.quality-gate-tmp` name but not current `temp/`. The single-workspace copy
experiment in [performance evidence](performance-evidence.md#resource-experiments)
fails with WinError 33 when reading the locked `.active.lock`.

**Change and proof:** Exclude generated scratch through the existing copy boundary.
Exercise the source-build fallback with an actual active workspace, including a
destination beneath scratch to expose recursive self-copy. Required source inputs
must still reach the real wheel build and installed-CLI controls. Assert no copied
scratch and unchanged active workspace; prove the old exclusions fail this case.
Keep the cached-wheel selection path and real artifact/install checks intact.

### 11 — Complete process-tree cleanup on timeout

**Evidence and owner:** `quality_gate/runner.py::_run_bounded_subprocess` kills only
its immediate `Popen` process. A controlled child retaining a nested workspace
reproduces WinError 32 after the parent's deadline. The existing fake-process
timeout test observes no descendants. A `taskkill /T /F` prototype passed a focused
case but failed full Gate cleanup; it is not an accepted implementation.

**Change and proof:** Trace all callers and restore complete timeout cleanup at
the shared execution boundary. Inspect the existing containment mechanism in
`quality_gate/publication_github.py::BoundedCommand` before duplicating process
management; reuse a compatible mechanism without importing publication policy
into runner decisions. Cover the Windows venv launcher, child/grandchild locks,
inherited output pipes, and a leader exiting before descendants. Termination must
complete before candidate/workspace cleanup, with bounded reader/reaping cleanup
and visible containment failures. Preserve the original check failure alongside
cleanup diagnostics. Dedicated active-workspace synchronization must still pass;
do not suppress WinError or change lock lifetime to conceal a surviving child.

### 12 — Investigate repeated immutable CLI preparation

**Evidence and owner:** Both isolated profiles put `tests/test_contracts_cli.py`
above 105 seconds. Its `_run` copies the same scanner, hashes it and synchronizes
a new release cache for every check/audit invocation. This repeated mechanism is
confirmed. Focused attribution measured 33.65 seconds inside `_run` outside its
direct child commands, including 17 release synchronizations. Nested operation
costs overlap; this remainder is not a promise that all preparation can disappear.

**Change and proof:** Profile preparation and child execution separately before
editing ownership. Share only verified immutable infrastructure when the focused
measurement supports a gain; keep consumer repositories, manifests, Git indexes,
mutable caches and failure injections function-scoped. Preserve real scanner
execution, digest selection and installation proof. `_release_gate` already shares
its installed CLI per module; its 21-second setup is not evidence for broader
scope. Release-controller initialization is behavior under test and stays real.
If no safe measurable improvement exists, retain code and record that decision.

### 13 — Complete verification and time-budget acceptance

**Evidence:** The two isolated complete runs in
[performance evidence](performance-evidence.md#isolated-baseline) passed at
303.01 and 301.46 seconds. A 300-second pytest budget has no margin. The earlier
240-second target would require about 63 seconds (21%) of reduction, for which no
safe method has been demonstrated. It is not an acceptance requirement.

**Completion:** After groups 09–12, record exact candidate identity, interpreter,
plugins, test counts, skip reasons and commands. Run two sequential isolated full
pytest suites and the complete current-source/staged Gate under the real Windows
workspace. Compare focused setup/call/teardown and full timings on equivalent
inputs; separate infrastructure reuse from changed collection or environment.
Preserve every existing test contract; no speed-up is required to close the
investigation when no safe measured opportunity exists. Justify the deadline
from complete-suite measurements, not a desired speed. A 360-second allowance
would give about 19% margin over the slower standalone baseline, but two samples
do not establish a tail distribution or the installed Gate's duration. Measure
that actual command before changing its budget. The Gate
must pass, timeout recovery must leave no owned descendants or locks, source
fixtures must remain unchanged, and no worker from the verification batch may
remain active. Revalidate whenever implementation or the index changes.

The [ticket 01 execution record](issues/01-secret-waivers.md#execution-record)
also documents historical WinError 3 in nested release-backed/install tests,
despite focused passes and a Gate pass from a short checkout. Revalidate the
canonical nested path before acceptance. Record any failing path and its owner;
shortening the checkout or skipping installation proof cannot establish that the
canonical scenario works. A newly required path repair must be scoped from a
current reproduction rather than an assumed cause of that historical failure.

## Testing Decisions

### Shared proof standard

Use independent expected values and observe both the specified result and absence
of forbidden side effects. A negative earns its guard only when valid unrelated
prerequisites and a positive control distinguish that guard from another failure.
Preserve useful mocks at I/O boundaries; keep the real decision/producer under
test. Do not replace independent package, native Git, provenance, process-tree or
workflow-permission proof to make these additions faster.

The group-specific seams and mutation checks above are proposed implementation
verification, not executed audit evidence. The original test-repair groups need
no new production seam; group 11 must first assess existing containment reuse.
For each requested mutation, capture the original source/index state, apply only
the named regression, run the affected proof through the required runner, and
require failure for the intended assertion. Restore the exact source and original
index state before running the corrected case. Do not commit or leave mutations.
If a proposed mutation is infeasible, record concrete alternative distinguishing
evidence and the limitation rather than claiming mutation success.

### Verification workflow

Resolve `CODEX_HOME` from the environment, otherwise use `.codex` under the OS
user profile. Read `MY-SETTINGS/test-runner-workflow.md` and the authoritative
`agents/test-runner.toml` beneath that root before automated execution. Follow
its typed request schema, snapshot rules, serialization, identity validation and
runner retirement; do not copy that schema here. Complete edits before dispatch
and keep checked code/index unchanged while a request runs.

1. Revalidate each group's current source evidence and run its focused scopes and
   proposed faulty/corrected proof before marking its ticket done.
2. After combining candidates, run the complete suite through the required typed
   runner and active staged Quality Gate workflow. For current-source/policy
   changes, also satisfy the [agent reference](../../docs/agent-reference.md)
   current-source verification requirement before any commit.
3. Reconcile collection with the actual changes: additional rows are expected;
   existing tests must not disappear or acquire unexplained skips/deselections.
   Record exact skip IDs/reasons and unverified platform obligations.
4. Verify preserved integration controls, zero remaining production mutations,
   unchanged original source fixtures and no unrelated checkout/index changes.
   Resolve red/unchecked results honestly; no waiver or skipped assertion may
   substitute for a repaired prerequisite.

### Observed baseline and limitations

The completed audit read 395 Python declarations in 28 files and one Node fixture
declaration in two independent GPT-6.1 Sol High passes. The 2026-10-11 Windows /
Python 3.12 baseline, run from the repository root through the typed runner, was:

```text
python -B -m pytest -p no:cacheprovider -q --tb=long -ra --durations=25
575 collected; 568 passed; 7 skipped; 0 failed; exit 0; 277.80 seconds
```

Baseline revision: `4457addf27d64eada34d2cdc43e41b1f08496567`. Conversion revision:
`00f30c43caa592ebf1c69fbe40d48bb892ae6e7f`, branch `feat/audit-test`. Their tracked
file trees were identical when checked; the conversion starts with no tracked or
index modifications. Verify the current revision/worktree before implementation
and revalidate findings affected by intervening changes. This historical baseline
is not acceptance evidence for future edits.

The runner summary omitted exact skip IDs/reasons. Source establishes four Windows
exclusions; three optional symlink cases plausibly explain the remaining skips,
but actual IDs are unconfirmed. Linux, live GitHub/GHCR, separate Node-fixture
execution, coverage and the proposed mutations were not measured/performed. No
baseline product failure occurred. The Node fixture is routing input, not product
behavior proof.

Measured slowest baseline operations: release-backed parity setup 21.95 seconds
and call 14.54 seconds, web-boundary CLI call 12.57 seconds, native pre-push call
11.08 seconds, and native commit call 8.28 seconds. Installed-release infrastructure
is already module-scoped; no removal or speed-up follows from these timings.
The five audit-created fixture lock roots were removed after recording evidence.

The supplemental [performance evidence](performance-evidence.md) distinguishes
the noisy 2026-10-13 measurements from two isolated 2026-10-14 complete runs on
revision `5454af580629dd10484ca61281f7f5ab08b7fdd7`. It owns the current timing
tables, exact platform skips, portable artifact location and resource experiments.
These are pre-implementation baselines, not speed-up or final Gate acceptance.

## Out of Scope

- Implementing these repairs during specification/ticket preparation.
- Changing supported verdicts, public schemas, policy releases, workflow
  permissions, consumer pins or release/publication contracts. Group 11 repairs
  timeout resource ownership without broadening those contracts.
- Removing or consolidating tests, adding suppression/waivers, weakening asserts,
  replacing valuable integration proof, or pursuing speculative optimization.
- A new repository-wide audit, live publication, external issue creation, commits,
  pushes or deployment. Any implementation commit follows the Git skill separately.

## Further Notes

The original nine groups derive from accepted findings above the user's 80% confidence
threshold. Provenance is retained beside each group's evidence; three findings
were shared by the two passes and six were independently reported by one pass.
The supplemental groups retain confirmed resource failures, a measured setup
optimization candidate and an explicitly historical path-headroom prerequisite.
They do not assert an unmeasured speed-up or completed product repair.
This package supersedes the removed audit report and retains its implementation
evidence. No ticket is done until its
acceptance proof and applicable verification actually pass.
