# Edit secret provider preparation

## Post-merge release correction

PR #61 merged as `5acc8064341c8bb197a613bfa0178ea8d41a8f96`. Its PR Quality Gate
passed, but release run `39403272564` failed the installed artifact audit on both
platforms. The public-wheel comparison selected sibling `v2.5.0` even when the
fixture supplied `v3.0.0`; the private controller cache contained only the candidate.
Docker reproduced `DistributionError` for the absent historical `release.toml`.

The comparison now tests the supplied verified wheel against its own contract:
historical v2 placeholder digest, corrected v3 actual-value digest, identical
locations, and no synthetic-value output. It requires no extra baseline release
in the candidate cache. The [incident lesson](../../lessons/incident-2026-10-secret-release-cache.md)
records the missed boundary and qualification requirement.

The original failed envelope and source are preserved. Version 3.0.0 remains
unpublished; the correction PR carries the accumulated MAJOR decision relative
to v2.6.2 and requires a new owner merge. Host activation remains pending.

This local candidate implements the provider portion of the `skills` repository's
`python-quality-hooks` ticket 02. Host entry points, settings, registrations,
coordinator, backups and actual Codex events belong to the host implementer.
The [public contract](../edit-secrets.md) owns request/result fields and the
development-only executable seam. The owner's subsequent PR instruction extends
preparation to verified commits, push and a normal PR. Direct publication, mutable
cache replacement and host activation remain outside this provider task.

## Ownership and acceptance map

| Contract | Owner / implementation seam | Verification |
| --- | --- | --- |
| Exact root-relative raw bytes and read-only freshness | `edit_inputs.py`, `edit_secrets.py` | Public CLI: Unicode/spaces, duplicates, rename/deletion, stale/large/missing inputs; changed source/config at native boundary |
| Managed repository-wide / non-Git baseline selection | `edit_secrets.py`, immutable distribution verifier | Real v2.4.2 cache and Gate-selected policy in isolated suite; no component interpreter; corrupt/missing/unsupported release fails closed |
| Same-release detector/path semantics and actual digests | `secrets.py` shared scanner transport | Real Gitleaks 8.30.1, repeated/multiple matches, current normal candidate and published wheel comparison |
| Exact typed waivers | `contracts.py` unchanged resolver | Actual-secret waiver, mismatch, wrong target, expired manifest and historical placeholder digest |
| Source-free failure boundary | shared edit worker/CLI, in-memory report parser | Invalid/truncated/duplicate JSON, unrequested paths, invalid lines, stderr, injected IO exception, exit disagreement; no retained report/log |
| Supported text and explicit skipped coverage | `edit_secrets.py` byte validation and pinned lifecycle reader | UTF-8/BOM, invalid UTF-8/NUL/UTF-16, native PDF/application skip, log-like attributed names |
| Deadline, bounds and owned native tree | `edit_process.py`, shared supervisor | Real native cancellation, orphan termination, bounded output, existing Ruff timeout/wheel/reuse regressions |

Dependencies point from the CLI to each application and shared input/release/process
owners. Secret selection consumes no Python component runtime and changes no
Ruff policy or schema. Full Gate still owns repository/history checks and tests.

## Confirmed findings and compatibility

1. Real Gitleaks 8.30.1 applies `--redact=100` to JSON `Secret`, confirmed by the
   pre-fix normal-candidate test. The existing mock-report tests supplied an
   unredacted Secret, so they did not exercise this transport defect. Historical
   normal output had SHA256("REDACTED") for distinct detected values. The corrected
   shared transport hashes actual values in memory, exposes only locations and
   digests, and keeps typed waiver validation/matching unchanged. This deliberately
   does not honor an incorrect placeholder-digest waiver for another secret.
2. Native directory-mode identities are absolute. A real metadata-only probe
   established `File` and `Fingerprint` independently of the Gate normalization.
   Relative `.gitleaksignore` entries do not suppress the normal materialized
   candidate; the provider preserves that behavior. A native-ignore migration
   would be a separate policy decision. Gate waiver targets remain relative.
3. Valid UTF-8 alone does not prove native coverage. A synthetic PDF header was
   initially reported passed because the scanner skipped it. The new regression
   starts red and verifies explicit coverage unchecked after pinned lifecycle
   inspection. Binary/archive skip diagnostics never become successful silence.

The supported old policy pins retain the same Gitleaks 8.30.1 detector inventory.
The compatibility adapter verifies complete old release inventories without
executing their wheels on the edit path. Published-wheel normal comparisons are
test-only evidence and separately expose the redaction discrepancy. No release
content, consumer pin or cached executable is changed.

## Development evidence

Typed working-tree requests `edit-secrets-01-regression-red` and
`edit-secrets-03-fingerprint-empty-green` established the historical fingerprint
regression and the initial fix (10 tests green). Requests
`edit-secrets-05-touched-waiver-red-confirm` and
`edit-secrets-06-touched-waiver-green` established real touched-tree/waiver
behavior (12 tests green). Request `edit-secrets-11-binary-coverage-red` proved
the native-skip coverage hole; `edit-secrets-12-binary-coverage-green` passed the
then-current secret suite. These are development evidence, not final-candidate
certification. Final full checks, candidate/authority hashes and independent
review outcomes are reported with the final prepared candidate.

`edit-secrets-15-focused` passed 56 tests in 46.35 seconds, including the real
historical-wheel comparison, malformed-report boundaries, legacy-only examples
and owned native descendant cleanup. `edit-secrets-24-types-green` checked 43
source/test/script files with pinned mypy 2.3.1 after explicit type annotations
and baseline narrowing. Later full checks must cover the resulting candidate.

`edit-secrets-27-staged-full` passed manifest preflight but its pinned Gate
reported pytest unchecked after the existing 400-second test deadline. This is
not accepted full evidence; the budget was retained pending profiling
and a complete rerun. Before the next candidate, `edit-secrets-29-shadow-red-confirm`
reproduced an external-extension guard gap: a second case-insensitive TOML table
reached scanner execution and returned tool_output rather than configuration.
All such tables are now inspected before scanner startup; its final regression
passed in `edit-secrets-30-focused-profile` (57 tests, no skips).

The complete diagnostic profile `edit-secrets-31-suite-profile` completed 716
passed and nine explicit Windows/platform skips in 402.74 seconds with the
prepared Python 3.12 runtime and exact v2.5.0 policy. Unlike the timed-out Gate,
this direct pytest profile completed all intended tests; its timing is not a
like-for-like speed comparison with the interrupted Gate. The slowest existing
CLI cases took 11.59/10.17 seconds, and the immutable engine-wheel setup took
8.66 seconds. No existing fixture, public delivery path or timeout was changed.

The new secret/process suite before test optimization took 44.55 seconds in
`edit-secrets-30-focused-profile`: 57 passed, no skips. Its operations above two
seconds belong to behavior under test: repeated real scans for UTF-8/BOM
(3.17/3.02 seconds), the historical-wheel comparison (2.53), rename/deletion
(2.16), and native cancellation (2.02). The immutable policy fixture is shared
and its setup stayed below two seconds; mutable projects remain test-local.

Input/policy cases now use the public worker in process with real Gitleaks IO.
Full CLI cases retain clean/finding batches, exact waivers, source-only launch,
historical-wheel comparison, PDF coverage, trace-like names and corrupt releases;
normal and unchecked CLI cases retain actual workspace cleanup assertions.
No detector, integrity verification, assertion outcome or test case was removed.
`edit-secrets-32-focused-after` completed the same 57 cases with no skips in
30.90 seconds under the same interpreter, pin and prepared-cache conditions.
This one before/after pair observed 13.65 seconds less pytest wall time; it is
provisional, not an established stable improvement. Remaining operations above
two seconds were the historical wheel (2.39), repeated scans (2.29/2.22), and
actual cancellation (2.03). Full-suite remeasurement and final staged/source
Gate evidence are required separately.

The complete after-profile `edit-secrets-36-suite-after` completed the same 716
passed and nine platform skips in 470.89 seconds: 68.15 seconds (16.92%) slower
than the single before-profile, despite the focused reduction of 30.64%.
These two runs used the same prepared interpreter, exact policy and command;
they do not establish an overall speed improvement. Existing staged-candidate
and CI-parity calls increased from 10.17/9.88 to 14.12/14.15 seconds; a separate
existing junction teardown took 13.42 seconds. The retained test change removes
entry-process startup from input/policy cases while preserving real scanner IO
and separately tested public delivery, not a required production decision.
This is the ticket justification for retaining that bounded test change despite
the full-suite regression. Neither sample justifies broader fixture changes or
claims a stable improvement; full Gate verification is required separately.

The subsequent staged run `edit-secrets-46-final-full` again reached the
400-second pytest timeout; its other checks passed. Together with both completed
725-case diagnostic profiles, this disproves the assumption that the retained
local test budget covers this candidate in the observed environment. The
repository-owned test limit is therefore 525 seconds, allowing 54.11 seconds
beyond the slowest completed run. This is an execution safeguard, not a speed
claim: all tests, failures and integrity checks remain required. The aggregate
Gate limit stays 600 seconds and the public edit deadline stays ten seconds.
The historical edit-check audit retains its original 400-second setting as a
dated candidate decision. The new staged/current-source Gate must complete under
the revised local test budget before this preparation is verified.

Linux native-process diagnostics in `edit-secrets-42-posix-state` reproduced an
immediate orphan-PID assertion failure (two other cases passed). By failure
reporting, `/proc/<pid>/stat` was already absent and remained absent after 100 ms.
The new test now allows one bounded second for asynchronous kernel/init reaping;
the POSIX production process-group termination is unchanged.
`edit-secrets-43-posix-green` passed all three native cases in 2.69 seconds using
the existing pinned Python image, read-only source/policy mounts, disabled
network and container init. Its stopped container is retained for owner removal.
Final Windows and full-Gate evidence remain separate required checks.

The initial combined edit regression run took approximately 262 seconds and
returned 122 passed, two failed and two explicit skips. One failure was the
incorrect test assumption about relative native-ignore identities. The other
was an archive-backed Ruff invocation that exhausted its shared deadline under
that run; it passed in the complete diagnostic profile. Neither earlier failure
is accepted as green. Existing immutable wheel/setup fixtures dominate that broad scope;
this change does not alter their ownership or weaken deadlines.

### Public executable latency

Run `python -B -m scripts.measure_edit_secrets` from the provider root after
preparing the ordinary immutable v2.4.2 cache. The harness removes its disposable
projects, alternates clean/failing content and prints source-free JSON summaries.
`edit-secrets-25-profiles-final` completed all 124 invocations in approximately
160 seconds on Windows / CPython 3.12.10. Each profile has a separate first call
and 30 warm calls, with 16 findings and 15 clean results in total. Every call
reported three logical provider process starts (entry, worker, scanner) and
completed within the unchanged ten-second deadline. These counters are protocol
reports, not an operating-system process census. This is one prepared-cache
measurement series; the table reports within-series variation.
First means first measured call with already prepared shared release caches;
it does not imply a cold operating-system disk cache.

Exact v2.4.2 `release.toml` SHA256:
`96e3d83d799df2d81eacafd6901fba2e9b3f6c44b7e1f2e9f71be85924320e69`.

| Profile | Source bytes | First ms | Warm p50 / p95 / max ms |
| --- | ---: | ---: | --- |
| Managed, one file | 8192 | 1219 | 1297 / 1406 / 1422 |
| Managed, ten files | 1000000 | 1344 | 1468 / 2062 / 2156 |
| Baseline non-Git, one file | 8192 | 1344 | 1297 / 1594 / 1735 |
| Baseline non-Git, ten files | 1000000 | 1344 | 1281 / 1969 / 2265 |

Percentiles use nearest rank. The one-file one-second objective and managed
ten-file two-second objective were exceeded; these are optimization objectives,
not permission to weaken integrity verification or change cancellation bounds.
These measurements cover provider launch through response delivery and exclude
host attribution, existing Ruff, legacy security and actual event feedback. The
host must measure that aggregate before activation. An earlier 175.4-second
measurement aborted on an unexpected verdict without retaining its reason and
is not accepted evidence; the diagnostic harness now records fixed status/reason
and profile/sample metadata on failure, and the complete current run passed.

## Pending release and host handoff

The release candidate is v3.0.0 from published v2.6.2. The new command alone is
additive, but the shared fingerprint correction requires reissuing persisted
placeholder-digest waivers; the combined release therefore has MAJOR impact.
Source/commit hashes identify the candidate until immutable publication. Version
authority, declared projections and Russian notes are prepared for owner review;
Windows/Linux product verification, owner merge and explicit host installation
remain required. The provider source policy stays on published v2.5.0.

The selected-wheel regression uses the supplied verified release. A published
v2 fixture retains the historical placeholder digest; the platform verifier's
v3.0.0 candidate fixture verifies the corrected actual-value digest. It requires
no sibling historical release in the isolated candidate cache. Ruff's real wheel
regression also exercises exact v3.0.0 policy alongside every retained old pin.

`edit-secrets-50-release-focused` passed 80 cases in 71.39 seconds with the
newly prepared Python 3.12 runtime and exact published v2.5.0 scanner policy.
The scope includes the complete secret/process regressions, release metadata
and inventory checks, both real wheel entry points with v3.0.0 policy and
fresh/reused Ruff selection across the old pins and v3.0.0. This is focused
release-candidate evidence; final staged/source Gate and both review axes
are reported separately for the frozen candidate.

Keep the host ticket unfinished until real installed scanner integration, actual
registered event delivery, combined Ruff-plus-secret latency and migration pass.
The host retains legacy secret-file access and unrelated protection. A narrow
supplemental preservation proposal must compare legacy-only detections separately;
the provider contains no supplemental detector and silently adds none to Gate.

### Narrow legacy preservation proposal

The host's existing `MY-SETTINGS/hooks/codex_hook_adapter.py`, resolved relative
to its Codex root, owns five built-in pattern families. Four synthetic examples
matched by those literal patterns are intentionally clean under the selected
Gitleaks defaults: an eight-character AWS secret assignment, a twenty-character
short GitHub token, a twenty-character short OpenAI-style key and an isolated
private-key header. Public provider tests identify these differences without
adding them to shared detector policy. These examples establish gaps, not an
exhaustive equivalence proof for the fifth AWS access-key family.

Before removing the legacy post-edit scanner, the host should preserve exactly
its reviewed built-in families in a separately identified bounded supplemental
check, run on the same final touched bytes and deadline. Keep its identities and
waivers distinct from `secrets.candidate`; do not label a supplemental match as a
Gate finding. Review any broader coverage separately. Do not enable Semgrep or
detect-secrets as an implicit replacement. Pre-command sensitive-file access
protection remains the host's separate mechanism. Host review, implementation
and real-event migration verification are pending; this provider activates none
of these changes.
