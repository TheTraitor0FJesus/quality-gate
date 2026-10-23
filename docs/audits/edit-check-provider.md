# Edit-check provider acceptance

This record covers the Quality Gate provider scope of the `skills` repository's
`.scratch/python-quality-hooks/spec.md`, `issues/01-ruff-hook.md` and `research.md`.
Those parent inputs and the active Codex hook/settings/registration belong to the host
implementer. This PR does not change the parent ticket state or activate a host hook.
The public handoff is [edit-check schema 1](../edit-check.md).

## Original v2.6.0 candidate and release boundary

- Integration base: `main`, starting at `f3220b4693d62c267649fae461b43d3c4b6e9168`.
- Published baseline: [v2.5.0](https://github.com/TheTraitor0FJesus/quality-gate/releases/tag/v2.5.0),
  source `c3ee03d79695cf5efe7ac7bc00759d417adf998f`, published 2026-10-12.
- Candidate: v2.6.0, MINOR for the additive public CLI. Existing Ruff rules, schema 2,
  staged checks and Git hooks are preserved. The self-policy pin remains v2.5.0.
- Owner merge and the existing exact-source Windows/Linux builds, audit and release
  publication/readback are required. No immutable v2.6.0 release is claimed here.
- The initial host consumer can retain v2.4.2. It must explicitly install the new
  interface engine, provision native Ruff for its pin and complete host activation.

## Acceptance map

| Provider requirement | Authoritative seam | Evidence |
| --- | --- | --- |
| Versioned bounded request/result, identities and redaction | `edit_protocol.py`, `edit_cli.py`, public protocol | Executable JSON/exit tests; fixed failure codes; final-content hashes |
| Exact working-tree files, non-Git, path safety and freshness | `edit_inputs.py`, isolated publication | Stale/deleted/directory/escape/junction, duplicate, no-op, unrelated-file and bounds cases |
| Managed policy/runtime and working-tree inputs | `edit_check.py`, existing runtime identity/inspection | Old-pin managed fixture; independent baseline coverage; dependency/configuration/policy invalidation |
| Explicit baseline and offline preparation | `edit_policy.py`, `edit-setup` | Real native wheel payload; corrupt/missing release/tool/runtime cases; no edit-time setup calls |
| Existing exact pins | Compatibility adapter and Gate distribution verification | v2.4.2/v2.5.0 real released artifacts; unsupported pins unchecked |
| Reviewed fixes, residual lint, final checks | Batched native Ruff commands | I001 sorting, surviving F401, production/test exceptions, noqa, syntax and residual lint plus formatting |
| Invocation deadline, bounds and cleanup | Supervisor, bounded streams, Windows Job Object / POSIX process group | Runtime descendant timeout, bounded output, no leaked workspace, source-free failure output |
| Public behavior and parity | `tests/test_edit_check.py`, disposable real-release fixtures | CLI cases and corresponding exact release's ordinary Gate checks |
| Timings/process counts | Installed candidate executable, 30 warm observations per profile | Provider measurements below; host event-to-feedback acceptance remains separate |
| Documentation, declarations, release and full verification | Architecture/reference/contract, product projections | Final staged/current-source Gate, independent reviews and hook evidence attached to the PR candidate |

## Development verification

Typed runners used fresh request identities with `snapshot: working-tree`, the exact
Quality Gate worktree root and bounded path/nodeid scopes. The initial regression
`edit-check-red-20261015-01` failed because the public command did not exist. The first
transport slice passed as `edit-check-green-20261015-02`.

The pipeline regression exposed Windows short TEMP paths; canonicalizing the isolated
workspace restored real Ruff behavior. `edit-check-pipeline-green-20261015-10` passed
the initial five executable cases. `edit-check-boundaries-green-20261015-13` passed
24 cases after final format results were made per-file. `edit-check-process-boundaries-green-20261015-15`
passed 30 cases, including an actual Windows junction and an owned child process timeout.

`edit-check-policy-stale-red-20261015-19` demonstrated that a policy changed after initial
integrity verification could otherwise produce success. `edit-check-policy-stale-green-20261015-20`
passed all 31 cases after observing release, native tool, interpreter and runtime metadata
identities through publication. Ruff source validation passed in
`edit-check-ruff-green-20261015-18`; subsequent changes require refreshed final evidence.

The later executable regressions found two additional boundaries. `edit-check-nosite-red-20261016-42`
demonstrated that `-I` alone executed an installed `.pth` startup hook during version
probing; `edit-check-nosite-green-20261016-43` passed after adding `-S` to the probe
and controlled worker. `edit-check-interpreter-red-20261016-46` demonstrated that a
changed interpreter could be recorded after it had executed. Interpreter identity is now
captured before probing, including the resolved POSIX symlink target, and checked through
publication. `edit-check-boundaries-green-20261016-47` passed the complete CLI scope.
The POSIX interpreter-symlink case is explicitly skipped on Windows; Linux verification
remains part of the existing platform CI/release route.

Linux CI also checks platform-specific typing. Its `ctypes` stubs omit `WinDLL`;
the Windows-only job helper uses the same narrow attribute annotation as the existing
publisher job helper. This annotation changes no runtime code; the installed-interface
measurements below retain their recorded wheel identity.

Configuration recapture preserves the first observed identity, including optional-file
creation or deletion. Deterministic regressions first failed for all three mutations;
the shared capture boundary now rejects them as stale input. At that candidate, the full-test
timeout was set to 400 seconds, applying the owner's intended limit from `feat/audit-test`;
the aggregate gate deadline and edit invocation deadline are unchanged.

The Gate's nested Windows temporary workspace exposed ensurepip's path-length limit
in the new managed-runtime fixture. Shortening only that fixture's disposable cache
preserves real runtime setup, integrity checks and independent mutable project state;
system long-path settings and production runtime behavior are unchanged.

The suite also verifies unchanged index bytes despite different working-tree content,
Unicode/spaces and repeated no-op edits, unrelated missing runtimes, a missing release,
RECORD corruption, oversized deadlines and blocked result delivery. Git ignore behavior
is checked against the real pinned Ruff explicit-file invocation; it does not suppress
explicit paths. No new project Ruff policy or waiver semantics were introduced.

Final source/staged Gate verdicts, review attestations and the native commit-hook result
must bind the frozen PR candidate. This audit records behavioral and installed-interface
evidence; it does not replace those final checks or certify the host installation.

## Installed release comparison and measurement method

The isolated candidate wheel is installed into a disposable engine environment. Separate
complete genuine v2.4.2 and v2.5.0 caches retain their released wheels, inventory and policies;
`edit-setup` provisions each native Ruff without modifying either release. Managed fixture
runtimes are prepared explicitly. The unmanaged benchmark is outside Git.

The public invocation is `python -I -B -m quality_gate.cli edit-check`, matching the regular
module route used by the host's verified release launcher. Each parity fixture is checked
with its own released ordinary Gate after staging the final formatted bytes. The only
expected failed Gate check is Ruff `F401`; formatting and every other applicable check pass.
The corresponding released Ruff JSON rule/path/line/column tuples must equal the edit result.
Fixture staging belongs to this comparison only; edit-check itself never reads or writes an index.

Each timing profile restores a dense typed-function fixture with unsorted unused imports,
then validates retained `F401`, final formatting and final SHA-256 on every invocation.
The single file is 122,827 bytes; ten files total 966,470 bytes. Thirty warm observations
follow a separately reported first invocation. Every call starts fresh processes; caches
and explicit preparation already exist. Percentiles use sorted warm samples and nearest
rank for p95. External wall time includes module startup, request/result transport and
cleanup, but excludes host event attribution, bridge verification and input hashing.

Profiling identified oversized artifact read buffers: repeated artifact verification alone
took about 387 ms. Streaming SHA-256 removed those allocations while retaining complete
release verification, every artifact hash, bounds and freshness checks. Native Ruff is bound
to the authenticated wheel RECORD without decompressing its 26,395,136-byte executable on
each edit. No integrity checks, rules, final checks or process ownership were removed.

## Installed candidate results (2026-10-16)

`edit-check-installed-20261016-55` completed both parity cases and all four measurement
profiles, exit 0, 127,000 ms. Environment: Windows, CPython 3.12.10, Ruff 0.16.4.
The disposable candidate wheel (not a published release asset) has SHA-256
`888f40c49d6716ad17deb8d8aae941f2e3f5e322960a9f7b006e7ae1cac97b15`.
The native Ruff executable has SHA-256
`53babb992a013a4c690479e15d3647065ee2078f4994fbe17fef4caa7d9aeea7`;
the selected released policy has SHA-256
`d88b55ed2bc30e9b376f78a0f05d12476e90fb9cf7c90dc22da962557453f104`.

| Coverage | Files | First ms | Warm p50 ms | Warm p95 ms | Warm max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Managed | 1 | 1,003.96 | 833.05 | 910.83 | 966.55 |
| Managed | 10 | 958.52 | 931.92 | 2,855.25 | 2,972.26 |
| Baseline, non-Git | 1 | 841.23 | 813.50 | 974.45 | 1,033.65 |
| Baseline, non-Git | 10 | 892.84 | 911.76 | 2,644.61 | 3,345.85 |

Every invocation used eight direct provider process starts: entry, worker, isolated
version probe and five native Ruff commands. This excludes the host bridge and Windows
interpreter/console bootstrap processes. The final profile set meets the proposed single-file
p95 threshold of 1,000 ms but exceeds the unchanged 2,000 ms batch target in both coverage
modes. An earlier candidate set met both thresholds; the refreshed final candidate results
above supersede it. Behavioral parity passed, but latency acceptance remains incomplete.
These observations do not establish a hard latency guarantee or complete event-to-feedback
acceptance. Host attribution, release-launcher verification and delivery still need their
own measurements. Before activation, the owner must decide the measured trade-off if the
unchanged targets cannot be met; this provider PR does not relax them.

The protocol accepts a caller-owned deadline at most 10 seconds ahead. Every edit stage
and output delivery share it; this is a safety ceiling, not the accepted host latency
budget. The host must choose its aggregate budget from complete event measurements and
supervise the entire bridge/invocation, with bounded process-tree cleanup afterward.

Disposable fixtures, installed engine, measurement scripts and reports are task-owned
verification resources outside the repository. They must be removed after final verification
and review; immutable user releases and existing runtimes remain untouched.

## Direct wheel correction (v2.6.1 candidate)

The published v2.6.0 host smoke exposed an archive-backed launch defect after this
record's extracted-install measurements. The confirmed Windows engine wheel SHA-256
is `6f065d0ba5188bee51c6d1f47ec3cf1eb9aca01f7d72c4bd80da101d57673106`; its
`release.toml` SHA-256 is `0f3257c2fdef15fd7db42100f44c85d593264e19e74c7f58184d194582c55a18`.
The native launcher puts the verified wheel on `PYTHONPATH`. The v2.6.0 supervisor
then used that archive file as cwd and returned `unchecked / io` before checking files.
The v2.6.1 correction uses the existing private workspace as cwd while retaining the
explicit engine import and isolated worker. See the [escaped-defect lesson](../../lessons/incident-2026-10-edit-wheel-cwd.md)
and the [corrected invocation and compatibility contract](../edit-check.md).

The public wheel regression first reproduced exit 2, `io`, and zero elapsed milliseconds
as `wheel-cwd-red-20261017-03`. `wheel-cwd-green-20261017-04` then passed eight Windows
cases: both public CLI modules with v2.4.2/v2.5.0/v2.6.0 and the v2.6.1 candidate pin.
The same platform-independent cases run in Linux CI. The provider's CI prepares the
base build environment from the existing hash-locked `.release/build-requirements.txt`;
the backend is not assumed to be preinstalled, and this preparation applies only to
the provider repository. The real wheel is built once, then passed directly on
`PYTHONPATH`; it is neither installed nor extracted for execution. The test
checks safe fixes, residual F401, final hashes and workspace cleanup. This correction
does not certify actual host event delivery or replace the earlier measured wheel's
identity. The unchanged 1,000/2,000 ms targets and full host timing acceptance remain open.

`wheel-cwd-module-20261017-07` passed the complete edit-check module: 60 passed and
one explicit POSIX-layout skip on Windows. The first wheel case, including shared
immutable infrastructure, took 9.767 seconds; the wheel and prepared policy cache
are module-scoped, while mutable projects remain isolated per case.
`wheel-cwd-real-releases-20261017-06` passed six public CLI invocations against genuine
cached v2.4.2/v2.5.0/v2.6.0 inventories: residual F401 after formatting and a clean
follow-up for each pin, with unchanged release and wheel hashes. Its disposable
engine wheel SHA-256 is `2c7674b09fc1a20f83f02518b1f70a490d9cc15c336bb2eb77c895d2c021f6ff`.
These are functional checks, not replacements for the required 30-edit host profiles.

## Pending host activation

After publication/readback, the host must install the exact engine wheel and record its
digest/version separately from policy pins; run explicit sync/setup/edit-setup; configure
the baseline; validate the schema 1 result reader and final-file identities; finish actual
Codex registration/trust, event delivery, rollback and regressions; measure complete
event-to-feedback p50/p95/max over its required 30-edit profiles. Provider timings alone
cannot establish those host outcomes. Tickets 02–04 and consumer pin upgrades are excluded.

## Unchanged-content reuse candidate v2.6.2

The 2026-10-18 provider delegation starts from published v2.6.1, source
`45b16c7f8dc8572cd9fe3f39f93f64415c40deea` (PR #58), and requests safe deduplication
for host ticket 01. The originating host specification remains in the `skills`
repository at `.scratch/python-quality-hooks/spec.md`; its `research.md` section
"Pre-activation review and POSIX correction" owns the host investigation. The scoped
requirements are: first prove an existing safe recipe or add minimal provider-owned
reuse; freshly verify exact meaningful inputs; retain failed findings/final coordinates,
offline operation, shared deadline and original pins; bound source-free state; verify
real released Ruff and quantitative process/latency behavior; deliver a reviewed PR
without changing host files, settings, registrations or ticket state.

Published schema 1 has no complete identity-only probe: ordinary `edit-check` runs Ruff,
while an empty batch is inapplicable and does not resolve full selection. Reimplementing
private distribution/runtime rules on the host would create a second authority. The
chosen boundary is transparent reuse inside the same public schema 1 request/response;
[the protocol](../edit-check.md#unchanged-content-reuse) owns exact identities and recovery.
This is PATCH v2.6.2 from published v2.6.1; source policy stays v2.5.0, and the initial
consumer retains v2.4.2. Owner merge/publication/readback remain required.

The regression first observed 8 processes instead of the required 3 on an unchanged
batch. Adding a local module then exposed stale import classification (3 instead of
8). Both went red before their fixes. The relevant Ruff classification implementation
is [the pinned upstream source](https://github.com/astral-sh/ruff/blob/0.16.4/crates/ruff_linter/src/rules/isort/categorize.rs).
`edit-reuse-module-20261018-09` passed source lint and the complete then-current module:
79 passed, one explicit Windows skip for POSIX interpreter-symlink layout, 154.17 s.
The final suite additionally covers the candidate policy pin, occupied optional cache,
POSIX executable permissions, and temporary-file cleanup on permission failure.
`edit-reuse-persistence-red-20261018-12` reproduced a Windows temporary-key leak when
chmod failed before closing the stream; cleanup now runs after closing on both success
and failure. Final rebuilt-wheel regression and full Gate evidence belong to the PR
candidate, rather than the earlier development counts.

Measured fixture costs above two seconds are explicit: module-scoped immutable
release/native-tool and engine-wheel setup took 12.13 s once; the managed/mixed batch
case took 11.03 s including mutable runtime setup, repeated checks and runtime corruption;
the startup-hook and unrelated-component cases took 7.42/7.58 s. The policy-selection
case took 6.94 s, and corrupt-tool/release cases 4.70/3.92 s. These are setup or behavior
under test, not repeated immutable preparation; mutable result caches remain isolated
per test outside project roots. The additional verification intentionally exercises
cache hits before invalidation and does not relax the existing 400 s test budget.

### Native-wheel comparison

`edit-reuse-wheel-acceptance-20261018-11` passed one executable acceptance test, zero
failures/skips, in 234.852 s (237.588 s including doctor). The test made 248 public CLI
calls: four profiles, two engines, one separate first call and 30 warm calls each.
It used Windows/CPython 3.12, genuine v2.4.2 inventory/native Ruff, prepared managed
runtimes and real wheels on `PYTHONPATH`, with a separate launch cwd. Each call had a
fresh 10 s deadline and current SHA. Baseline-1/managed-10 were clean; baseline-10 and
managed-1 retained F401. Both wheel bytes and the entire policy inventory were unchanged.

- Published v2.6.1 wheel SHA-256: `d0723921710f8c012e59a41367bb2198a845e24146113156550c1656f6d15c64`.
- Timing candidate v2.6.2 wheel SHA-256: `60eb1a6335dde3d039e672fa0f4ac75f052aa367cf1ec07a1b73075d1b649c9e`.
- Exact v2.4.2 `release.toml` SHA-256: `96e3d83d799df2d81eacafd6901fba2e9b3f6c44b7e1f2e9f71be85924320e69`.

| Profile | v2.6.1 warm p50/p95/max ms | v2.6.2 warm p50/p95/max ms | Processes before/after |
| --- | --- | --- | --- |
| Baseline, 1 file | 933.12 / 1299.89 / 1371.87 | 817.86 / 968.60 / 983.70 | 8 / 3 |
| Baseline, 10 files | 998.02 / 1098.46 / 1107.59 | 852.20 / 959.85 / 980.90 | 8 / 3 |
| Managed, 1 file | 942.47 / 1114.16 / 1363.76 | 879.91 / 973.53 / 1011.08 | 8 / 3 |
| Managed, 10 files | 1021.41 / 1162.21 / 1237.26 | 865.96 / 1105.16 / 1127.15 | 8 / 3 |

External wall time uses `perf_counter`; p95 is the 29th of 30 sorted observations.
All hits preserved current input/final SHA, findings, exact engine and v2.4.2 selection.
The check also verified bounded record size/count and absence of a synthetic source
marker from persistent records. Timing preceded the cold-write error-cleanup correction;
its wheel identity is retained separately from the final rebuilt-wheel verification.
These are provider timings, excluding native host bridge and event-to-feedback delivery.
The parent host owns message compaction and reports its v2.6.1 activation separately;
new installed-host acceptance still requires the published immutable v2.6.2 wheel.

### Host handoff

After owner merge and exact-source Windows/Linux release publication/readback, explicitly
select the new engine while retaining exact policy v2.4.2. Continue ordinary schema 1
requests and strict validation: current-request `input_sha256`, actual final bytes,
exact root/engine/selection and freshly read configuration inputs. The unformatted-first
then formatted-hit case is covered for both clean and F401 results, including rebasing
`input_sha256` and `changed=false`. Host event/session/message deduplication remains on
the host. Repeat installed event-to-feedback profiles there; provider CLI measurements
do not establish actual registration, event delivery or UI acceptance.
