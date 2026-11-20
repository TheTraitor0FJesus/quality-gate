# Quality Gate agent reference

Use this page as the entry point for work on Quality Gate. The project keeps contracts in one
place and uses pointers here instead of copying configuration rules.

## Authority map

- [Architecture map](architecture.md) is the route through modules and execution flows.
- [Coding standards](coding-standards.md) is the review standard for implementation changes.
- [Quality policy change guide](quality-policy-changes.md) applies to the runner, shared tools,
  manifest contract, and reusable workflow.
- [Lesson format](lessons.md) is the source of truth for escaped-defect lessons.
- [Release procedure](release.md) is the source of truth for release validation and publication.
- [Shared publisher interface](publication.md) owns reusable release callers, evidence, and recovery.
- `quality_gate/contracts.py` is the source of truth for schema 2, verdicts, and waivers.
- `quality-gate.toml` declares this repository's policy release, documents, components, and limits.

This page is the single manifest-maintenance guide. Read it before changing declared components,
test paths, dependency inputs, required documents, workflows, waivers, or supplemental tests.

## Normal operation

Run commands from the repository root. `check`, `audit`, `validate`, `doctor`, and `version` do
not change tracked project files. Quality Gate puts temporary work under the checked repository's
ignored `temp/` directory. At startup it reclaims an inactive `qg-` workspace only when a user-local
HMAC key validates the root-level owner record and directory identity, the in-directory marker and
active-lock identities match, and Gate acquires the run lock. A signed cleanup record permits a later
run to resume after partial removal. A name prefix or marker alone does not authorize deletion.
Quality Gate preserves foreign entries, ambiguous directories, legacy `.quality-gate-edit-*`
directories, active runs and the root lock. Each invocation removes only its own workspace when it
exits. On POSIX, `temp/` also retains a private, authenticated `.quality-gate-cleanup-<key-id>`
directory for deletion journals. Separate user-state keys use separate areas. A previous authenticated
area and its journals remain available when that key is restored; untrusted areas are preserved.
An incomplete older key file is preserved alongside a separate recovery key, so restoring the older
key permits its signed records to be checked again.
Read-only checkouts and candidate snapshots use a private hashed workspace
per repository under the user-local system temporary directory. Every consumer repository must ignore its root
`temp/` directory in `.gitignore` (for example, with `/temp/`). Cache staging and atomic replacement
files remain beside their persistent targets. `sync`, `setup`, rollback, confirmed prune,
`format`, and `edit-setup` are explicit mutation commands. An `edit-check` request with
`phase: "write"` applies approved Ruff fixes and formatting only; `phase: "validate"` is
read-only. Omitting `phase` preserves the earlier synchronous writer-and-diagnostics behavior.

1. Run `quality-gate validate` after editing `quality-gate.toml`.
2. Run `quality-gate doctor` to verify the selected release and component runtimes are available.
   Run `quality-gate setup` after selecting a policy release or adding/changing Python components.
   Setup provisions the tools and component runtime before editing; edit-time hooks do not
   install them, and a missing runtime is reported as `unchecked`.
3. When an authorized commit is the next step, rely on its active commit hook to run the staged
   Quality Gate. Outside `$implement`'s explicitly required pre-review Gate, run a
   standalone or current-source `quality-gate check` only when the user directly requests it.
   Generic verification, review, PR, release, or policy-change guidance does not trigger another
   Gate. After a failed staged Gate is fixed and commit is next, run focused checks outside hook
   coverage and retry the commit; do not run a standalone Gate before the hook. Resolve
   `CODEX_HOME` from the environment, or use `.codex` under the OS user profile when it is unset.
   Run complete test verification with
   `python <CODEX_HOME>/tools/test_runner/cli.py run --repo <repo> --scope full --json`.
   For a direct user request or the `$implement` pre-review Gate, run
   `python <CODEX_HOME>/tools/test_runner/cli.py run --repo <repo> --scope quality-gate --json`.
   This scope runs only the installed `quality-gate --root <repo> check` against the staged
   candidate; it does not verify mutable checkout source. For consumers without a commit-hook Gate,
   rely on CI and report an unavailable Gate instead of starting one locally.
   Accept evidence only when `schema_version`, canonical `repo`, `scope`, and `snapshot` match
   the request; `capture_complete` is true; every required stage has `state: "completed"`, an
   exit code, and a repository-relative `log_path`; and `candidate.state` is `unchanged`.
   A `run_id` alone, launcher return, or empty output does not prove completion. If the CLI
   reports `run_in_progress`, wait for that run to finish before requesting a new one. Unsupported
   manifest declarations require an owner-approved CLI extension; do not substitute arbitrary
   commands or run an incomplete subset as full verification. Read retained stage logs for complete
   test evidence. If current-source verification is explicitly requested, establish and report the
   engine identity and use a procedure that supports that source check, or report it unavailable.
   The Gate's default report preserves every observed failed and unchecked status while bounding
   finding details. Known and unknown upstream omissions are identified explicitly. Use `--verbose`
   for every status and all safely collected redacted findings.
4. Use `quality-gate format <explicit-path>...` and stage the result. The next authorized commit
   runs the staged Gate. Do not repeat it before the hook solely because formatting or a correction
   changed the candidate; use focused checks outside hook coverage.
5. The `quality-gate version` command prints the installed engine package version. The repository's
   `quality-gate.toml` selects the policy release; `quality-gate doctor` checks that release and its
   runtimes but does not print its version. `quality-gate setup` prints the selected release when it
   prepares the policy and runtimes.

Each `check` or `audit` owns one operation after local admission. Its monotonic work clock starts
before repository discovery and candidate construction, then covers preparation, readiness probes,
cache validation, scanners and checks. The staged manifest sets the work limit from that original
start; workspace lock waits and stale-workspace reclamation use that same deadline, and every child
process uses the remaining operation time and its existing shorter command cap.
Local admission keeps its separate queue contract. After work stops, process-tree termination, snapshot,
workspace, nested tool-temporary, and native-cache staging roots share one separately bounded
120-second cleanup phase. Native cache leases stay held until that cleanup finishes.
The final report is rendered only after cleanup settles, and the operation's elapsed duration
includes that cleanup.

Windows cleanup removes each owned name through its identity-checked handle using POSIX disposition
when supported. This permits directory removal while a delete-sharing reader still holds a file
handle. Unsupported systems retain legacy disposition; access errors remain blocking. Immutable
WinAPI bindings and structure classes are initialized once, while ownership records, file identities
and attributes are checked afresh. The cleanup allowance covers the entire operation and is never
renewed per root. Enclosing hook deadlines include this allowance. The installed test CLI owns its
separate execution and process-cleanup budget.

Reports keep existing check IDs, outcome/status lines and exit precedence. Appended operation and
check telemetry records monotonic elapsed time, actual scope, stage and execution disposition
(`executed`, `reused` or `not-run`); outcome remains the check's separate `passed`, `failed`,
`unchecked` or other status. Readiness, policy/runtime cache lookup and validation, candidate
preparation and cleanup have their own stage observations. A reused result must carry its original
execution identity and duration separately from current lookup and validation. For file-local Ruff
evidence, `original_elapsed_ms` is the longest original producer operation duration among reused
files. `original_operation_id` identifies that producer by its operation name and UTC start time.
This representative duration includes the producer's work beyond formatting and is not a sum of
per-file execution times. Mixed fresh and reused checks remain `executed`; fully reused formatting
is `reused`, including a retained failed outcome. The current Gate does not implement whole-suite
result reuse or automatic rerun selection. Pytest telemetry retains bounded
terminal outcome totals when available. Telemetry is additive, redacted and bounded; it does not
change whether a check is eligible or which exit code wins.

For this provider, the Python test stage has a 30-minute limit, Gate work has a 40-minute
limit plus the shared cleanup allowance, and the reusable CI job has a 50-minute outer limit.
The job limit includes bootstrap and cleanup. Local full-suite and Quality Gate requests use the
installed test CLI's 35-minute aggregate budget, including all command stages and process-tree
cleanup. This aggregate budget is the
effective local limit when an inner stage has a longer timeout.

The native pre-commit wrapper reads the staged manifest, resolves `quality.policy_release`, verifies
that exact cached release, and calls the Gate without importing mutable repository source. The
wrapper has no separate policy-version pin. If CI reports a v2 release, inspect the
`policy_bootstrap` output and the staged manifest. The bootstrap release is used only when this
repository's selected release API returns 404. The native pre-push wrapper blocks updates and
deletion of the remote default branch when Git can prove its name. For each other update it
scans locally available outgoing commits with the selected release's Gitleaks scanner. A first
push scans all reachable history; an update with a known remote object scans commits reachable
from the local object but not that remote object. Multiple updates retain the union of their
individual ranges. Deletions add no history. Annotated tags must peel to commits. Missing objects,
shallow history, scanner errors, and timeouts return `PUSH UNCHECKED`. Fetch or repair the
required history and restore the pinned release before retrying; the hook does not fetch or install.
Findings show locations and fingerprints, never credential values. History waivers retain the
`secrets.history` check ID and exact commit, path, line, and fingerprint matching.

Queue-aware releases admit four independent local full Gate runs across repositories for the
current user. Waiting continues automatically when a slot opens and has a separate configurable
budget. Ruff children default to four worker threads. See [Local Gate queue](local-gate-queue.md)
for settings, nested execution, and the native-wrapper activation requirement. Source changes do
not activate this behavior in an older immutable consumer release.

Each applicable Python component runs Ruff during its Gate checks. The runner runs mypy only when the
component enables type checking with `typecheck = true`. Native-cache selection never skips a
configured tool invocation. Each process receives a persistent native cache namespace keyed by the
shared Git directory for linked worktrees, prepared runtime fingerprint and engine version, exact
pinned tool and policy configuration, host platform and Python version, command options, and component
import root. Worktrees in one clone can share namespaces; separate clones remain isolated. For a
persistent namespace, the runner copies the current candidate to a stable, read-only source root that
it owns while holding that namespace's lease. Gate operations supervise staging against the original
work deadline, then transfer the validated source root to the shared cleanup phase and keep the lease
held until cleanup finishes. The runner derives file timestamps from file contents so Ruff can reuse
entries when equal source bytes arrive in different temporary snapshots. If staging is unsafe or
unavailable, the runner uses the original snapshot with that Gate run's private cache. Standalone
callers retain synchronous staging and cleanup. The cache directory is under the policy cache's
`native-tools/` directory. A bounded OS-lock lease is held while each process runs. Busy,
inaccessible, unowned, or invalid storage uses the private cache and continues verification.
Provider-owned entries expire after 30 days and are limited to eight namespaces per repository and
tool; cleanup skips active leases and removes no unowned entry.

The runner exposes a `NativeToolCacheSelection` record beside each native tool command, with
`namespace_id`, `storage`, `fallback_reason`, and `selection_duration_ms`. During an operation,
`lookup_ms` reports the total namespace-selection cost, including metadata validation.
`tool_cache=native-persistent` or `native-private` identifies the selected storage; a bounded
fallback reason explains private selection. Both paths execute the tool. These observations
do not prove an internal Ruff or mypy cache hit or reuse of a complete check result.
Lookup, pruning, namespace preparation and source preparation have separate stage observations.
Maintenance-lock and namespace-lease waits use the remaining original work budget. Pruning and
namespace preparation run in bounded children while the parent retains the maintenance lock;
namespace preparation also retains its namespace lease. Preparation workers do not mark a check as
executed: that disposition requires the checked tool's own process to start. Standalone cache
callers retain their existing local waits and synchronous maintenance.

One opt-in measurement used policy v4.2.0, Python 3.12.10, Windows 11, Ruff 0.16.4, and mypy 2.3.1.
Seven fixture snapshots invoked both tools: one cold snapshot, five warm snapshots, and one snapshot
with changed source. Each tool ran seven subprocesses, 14 in total. The cold and warm checks passed;
both tools ran and failed on changed source as expected. The persistent cache payload contained nine
Ruff files and 18 mypy files, excluding ownership markers.

| Tool | Cold run | Five warm runs | Median (range) | Cold/median ratio |
| --- | ---: | ---: | ---: | ---: |
| Ruff | 151.28 ms | 87.83, 96.33, 83.09, 84.87, 82.88 ms | 84.87 ms (82.88–96.33 ms) | 1.78x |
| mypy | 1,146.77 ms | 239.92, 257.09, 233.13, 253.92, 235.81 ms | 239.92 ms (233.13–257.09 ms) | 4.78x |

This was one measurement on one host. Timings are tool subprocess wall time, and operating-system
cache state was uncontrolled. Every check executed the tool; the measurements describe repeated
tool execution with a shared persistent namespace and cache payload, not a confirmed internal cache
hit or complete-result reuse. Treat the timings as provisional, not as a stable performance estimate.

If persistent cache use repeatedly falls back, let active Gate runs finish, then remove the
user-local policy cache's `native-tools/` directory to rebuild only native tool state on the next
run. The default policy cache is `%LOCALAPPDATA%\quality-gate` on Windows (system temp if unset)
and `$XDG_CACHE_HOME/quality-gate` (or `~/.cache/quality-gate`) on Unix. A private fallback is
automatic and still runs the selected check.

## Manifest maintenance

See [Manifest maintenance](agent-reference-operations.md#manifest-maintenance).

## Setup and unavailable verification

See [Setup and unavailable verification](agent-reference-operations.md#setup-and-unavailable-verification).

## Consumer release update

See [Consumer release update](agent-reference-operations.md#consumer-release-update).

## Migration, audit, and lessons

See [Migration, audit, and lessons](agent-reference-operations.md#migration-audit-and-lessons).

## Troubleshooting and security

See [Troubleshooting and security](agent-reference-operations.md#troubleshooting-and-security).

## Extension route

See [Extension route](agent-reference-operations.md#extension-route).

## Release and rollback route

See [Release and rollback route](agent-reference-operations.md#release-and-rollback-route).
