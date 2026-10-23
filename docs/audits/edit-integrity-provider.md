# Edit integrity provider preparation

## Ownership and scope

The provider portion supports `python-quality-hooks` ticket 03. Its public
contract is [edit-integrity](../edit-integrity.md). The implementation reuses
the Gate's marker and waiver logic through `git_conflict_marker_results` and only
reads an explicit bounded set of complete working-tree files. The host owns event
attribution, the final post-Ruff snapshot, feedback, registration and rollback.

The candidate is an additive engine release. It does not alter shared detector
policy, native Git hooks, secret checks, legacy protection or backup behavior.
The provider selection continues to bind an immutable policy pin; the engine
version and consumer policy version remain distinct identities.

## Acceptance map

| Contract | Implementation seam | Evidence |
| --- | --- | --- |
| Same marker and exact waiver semantics | `integrity.git_conflict_marker_results` | Public CLI compared with normal Gate results for managed files |
| Explicit paths, non-Git baseline and nested files | `edit_inputs.sources`, `edit_integrity.execute` | Public CLI fixture; no Git enumeration |
| NUL and invalid UTF-8 behavior | Shared Gate helper | Binary and undecodable inputs preserve normal Gate skip semantics |
| Release and manifest freshness | `edit_policy.select_release`, `Configuration` | Immutable v2.4.2 fixture; malformed/stale release cases remain required |
| Bounded source-free worker result | `edit_protocol`, `edit_worker`, `edit_cli` | Public CLI asserts no stderr and no workspace residue |
| Host JSON/exit route and post-Ruff event seam | `file_integrity.check`, `provider_bridge`, coordinator, and candidate public CLI | `tests/test_edit_integrity_host.py` covers managed/unmanaged conflict and clean results, malformed input, post-Ruff final bytes, rename destination and deletion omission; only the unpublished immutable-wheel launcher is substituted |
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
implementation. Only the immutable native-wheel launcher is substituted while
release `v3.1.0` remains unpublished. The coordinator case simulates Ruff's
post-write bytes, checks the rename destination and omits the deleted path; the
test also covers malformed input and source-free feedback.

The provider scanner streams one file at a time, limits each file to 8 MiB, the
aggregate candidate to 256 MiB and findings to 4,096, and returns `unchecked`
with the affected path on read or limit errors. The public route maps its
finding limit to the explicit `limits` recovery reason. The earlier Spec gaps
remain corrected: the consumer template matches v3.1.0, installation checks
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

These checks certify the staged source candidate only. Immutable publication,
same-release installed-source parity, real clean/conflict/fix Codex events,
the four required latency profiles, disable/rollback/reactivation and final
identity/regression/cleanup evidence remain pending. Final Standards and Spec
review results accompany the implementation handoff. Keep ticket 03
`in-progress` and the host mechanism disabled until those live acceptance cases
pass.
