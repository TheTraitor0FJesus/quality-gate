# Test-proof repairs

## Problem Statement

Nine confirmed audit findings show that selected tests can remain green after a
meaningful regression, miss a producer-to-consumer boundary, or write into shared
source fixtures. A maintainer needs a passing suite to distinguish the intended
guard or delivery contract from an unrelated failure or a fixture-supplied result.
The audit established weaknesses in regression proof, not current product defects.

## Solution

Strengthen existing proof and add only the missing boundary cases in nine bounded
work groups. Preserve production behavior, independent integration protection,
failure diagnostics, and deliberate temporary-workspace locking.

This specification is the technical source of truth. All source paths are relative
to the Quality Gate repository root; links are relative to this document. Ticket
IDs match the implementation groups below. The user approved the nine-ticket
breakdown; tickets are published under `issues/` according to the
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

Each group can deliver its outcome without a later group. There are no blockers:
shared files and preferred execution order do not create acceptance dependencies.
Group 06 and group 09 touch one common declaration but protect different contracts.

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

## Implementation Decisions

Implement test changes using existing seams. No production API, persisted format,
manifest, dependency, policy pin, or reusable-workflow behavior change is planned.
Test-owned helpers belong to the slice they enable; no prefactoring-only ticket is
needed. Before starting a group, read [repository instructions](../../AGENTS.md),
the [architecture map](../../docs/architecture.md),
[coding standards](../../docs/coding-standards.md), and the
[agent reference](../../docs/agent-reference.md). Read the
[publication contract](../../docs/publication.md) for groups 03/04 and the
[quality policy guide](../../docs/quality-policy-changes.md) before any change to
shared behavior. A newly discovered product defect requires separate scoped work;
test repair must not redefine the existing supported behavior.

### 01 — Exact secret waivers

**Evidence:** `test_exact_fingerprint_waiver_applies_to_one_candidate_finding`
(`tests/test_secrets.py:103`) supplies one exactly matching finding and checks only
WAIVED. Returning true instead of comparing fingerprints in `Waiver.matches`
(`quality_gate/contracts.py:184`) would still satisfy it. The composite negative
`test_invalid_typed_waiver_is_rejected_without_secret_value`
(`tests/test_contracts_cli.py:681`) includes unknown `value`, wildcard target, bot
approver and no fingerprint. `_parse_waiver` (`quality_gate/contracts.py:444`)
rejects the unknown field before the later guards; their removal is unobserved.
`_apply_waivers` (`quality_gate/secrets.py:158`) consumes the actual matching result.
Existing deptry waiver tests protect standard targets, not secret fingerprints.

**Change:** Keep the unknown-field/redaction case. Derive isolated invalid rows
from a valid waiver: wildcard target, bot approver, expired `expires_on`, future
`reviewed_on`, and malformed fingerprint. Observe each specific admission reason.
Use a controlled current date or dates relative to the test's current date. At the
existing fake-scanner seam, provide distinct secrets at the same target; the exact
reviewed fingerprint is waived and the other finding keeps the result FAILED.
The parser checks fingerprint shape; secret identity belongs at the scanner-result
boundary. Inspect surviving findings and redacted compact/verbose rendering.

**Preserve:** Valid waiver acceptance, unknown-field rejection, raw-secret
redaction, standard-waiver mixed-finding tests, and current date semantics.

**Proof and completion:** Each isolated negative reaches its named guard. A
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

## Testing Decisions

### Shared proof standard

Use independent expected values and observe both the specified result and absence
of forbidden side effects. A negative earns its guard only when valid unrelated
prerequisites and a positive control distinguish that guard from another failure.
Preserve useful mocks at I/O boundaries; keep the real decision/producer under
test. Do not replace independent package, native Git, provenance, process-tree or
workflow-permission proof to make these additions faster.

The group-specific seams and mutation checks above are proposed implementation
verification, not executed audit evidence. No new production seam is required.
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

## Out of Scope

- Implementing these repairs during specification/ticket preparation.
- Changing supported production behavior, public schemas, policy releases,
  workflow permissions, consumer pins or release/publication contracts.
- Removing or consolidating tests, adding suppression/waivers, weakening asserts,
  replacing valuable integration proof, or pursuing speculative optimization.
- A new repository-wide audit, live publication, external issue creation, commits,
  pushes or deployment. Any implementation commit follows the Git skill separately.

## Further Notes

All nine groups derive from accepted findings above the user's 80% confidence
threshold. Provenance is retained beside each group's evidence; three findings
were shared by the two passes and six were independently reported by one pass.
This package supersedes the removed audit report and retains its implementation
evidence without requiring a separate report. No ticket is done until its
acceptance proof and applicable verification actually pass.
