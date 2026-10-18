# Edit-check provider acceptance

This record covers the Quality Gate provider scope of the `skills` repository's
`.scratch/python-quality-hooks/spec.md`, `issues/01-ruff-hook.md` and `research.md`.
Those parent inputs and the active Codex hook/settings/registration belong to the host
implementer. This PR does not change the parent ticket state or activate a host hook.
The public handoff is [edit-check schema 1](../edit-check.md).

## Candidate and release boundary

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
the shared capture boundary now rejects them as stale input. The repository's full-test
timeout is 400 seconds, applying the owner's intended limit from `feat/audit-test`;
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
