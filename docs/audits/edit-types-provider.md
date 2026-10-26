# Edit typing provider preparation

Paths are relative to the Quality Gate repository. This is the provider handoff
for `skills` repository `.scratch/python-quality-hooks/issues/04-typing-hook.md`.
That ticket and its specification own installed acceptance. The provider owns the
[public edit-types contract](../edit-types.md); the host owns event attribution,
ordering after Ruff, scheduling, active-turn delivery, replacement and Stop/drain.
This change prepares source and a release candidate, not host activation.

The agreed wire document SHA-256 is
`801c4dbdac8668b9783f9bf813e91377c1908fa97b5a851905b061465a3d403e`.
Schema 1 retains the remaining synchronous ten-second limit. The owner approved
the additive schema 2 thirty-second background invocation on 2026-10-21, with one
separate three-second cleanup allowance. A provider process is one-shot; pending
state, replacement and delivery belong to the host.

## Acceptance map

| Required outcome | Provider owner | Verification |
| --- | --- | --- |
| Exact policy and declared managed runtime | `edit_type_environment.py`, immutable distribution and runtime inspection | Real released mypy in managed and non-Git `.venv` fixtures; unsupported policy, missing runtime, corrupt installed tools and missing dependencies are unchecked |
| Whole affected component and unchanged consumers | `edit_types.py`, `edit_type_inputs.py` | Changed function signature and imported type diagnostics in unchanged consumers; visible managed `build/` included; independent same-release full-directory mypy comparison |
| No ignored missing prerequisite becomes success | Static import-context inspection | Deleted absolute/relative modules, missing packages, untyped packages, missing stub submodules and missing stdlib submodules |
| Opt-out compatibility | Authoritative schema parser, full runner and runtime identity | New-policy false is inapplicable; explicit old-policy true/false is rejected; omitted true retains old identity; full Gate still schedules other Python checks |
| Independent freshness evidence | Fixed source/configuration/environment filesystem algorithms | Added/deleted source membership, recreated deletion input, concurrent source/configuration/environment changes and hardlinks |
| Canonical dependency evidence and valid settings | Typing configuration capture and result construction | Managed duplicate/out-of-order dependencies and unmanaged path ordering; malformed TOML, INI and JSON, duplicate JSON keys and non-JSON constants are unchecked |
| Source-free bounded read-only result | Shared supervisor and typing application | Exact exit/status, event binding, invalid cancellation ownership, source-inventory overflow, no project-code execution and no source rewrites |
| Owned background cancellation and deadlines | Shared process supervisor and host-owned signal | Existing and active cancellation, event identity on expired requests, real descendant cleanup and no retained invocation workspace |
| Exact executable tools despite mutable environment | Released wheel RECORD verification and isolated import origins | Corrupt bytes and package shadowing fail before import; bytecode cache content cannot replace verified source |

The tests are in `tests/test_edit_types.py`, `tests/test_edit_type_inputs.py`,
`tests/test_typing_optout.py` and `tests/test_edit_process.py`. Real-tool fixture
preparation is explicit and offline: create a disposable venv, install only wheels
from a verified release, then copy that immutable infrastructure into each mutable
test project. No production edit path provisions anything.

## Compatibility and setup

Version 3.1.0 is a MINOR capability addition relative to published v3.0.0. The
provider source policy remains on its existing published pin. Consumers choose
interface engine and policy separately; the old exact pins in the wire contract
remain valid. Adding `typecheck` requires the new selected policy; a newer engine
does not reinterpret this field for an older policy. No typing waivers are added.

Managed consumers run the documented explicit `sync`, `setup` and `doctor` route.
Unmanaged consumers explicitly prepare `root/.venv`, disable system-site-packages,
and install the selected release's exact mypy and required dependencies, together
with their own project dependencies and typing stubs. The release's platform wheel
must match the selected Python ABI; current release builds use CPython 3.12.
There is no fallback to global Python, editable installation, project plugin or
another project's environment. Typing validates the reviewed managed-runtime
`.pth` payloads but never executes site startup; unknown or changed `.pth`
content remains unchecked.

The environment certificate hashes actual regular files and the documented POSIX
interpreter aliases. Tool package origins and their complete installed package
contents must also agree with the verified wheel RECORD files. Isolated mypy
imports only verified tool modules and the standard library; it reads project
typing context without importing project code. An invocation-private empty Python
bytecode prefix prevents unchecked `.pyc` files from overriding verified sources.

## Policy and cache limits

The released `follow_imports=skip` and `ignore_missing_imports=True` remain intact.
The preflight prevents missing required static import context from being accepted
because of those flags. It does not prove dynamic imports, skipped external
implementation bodies or runtime-dependent behavior. Managed coverage includes
the full release-defined component discovery; unmanaged discovery has the separate
explicit exclusions in the wire document. Source symlinks and unsupported layouts
are unchecked. Evidence limits bound work, not the latency of an arbitrary project.

Mypy analysis caches are invocation-private and removed with the owned workspace.
There is no persistent typing-result cache, shared incremental database or daemon.
A later one-file edit recomputes the whole affected scope. Warm measurements
therefore describe warm filesystem/runtime pages, not reuse of a previous mypy
analysis. Configuration, environment, policy or dependency changes cannot reuse
an old typing result. The external cache holds the ordinary verified releases and
prepared runtimes; no release or project cache is modified during editing.

## Development evidence and performance conditions

Typed requests 026 and 033 established real managed/non-Git component behavior and
same-release parity. Request 036 exposed a missing stub-submodule false success;
037 passed the eleven context/freshness cases after correction. Request 038 exposed
the full runner's missing recovery action for an opt-out; 039 passed the five
opt-out tests. Requests 041/042 established stdlib-submodule checking; 043/044
established event identity on expired background requests. Request 049 proved that
an added package could shadow a verified tool module and execute; 051 passed the
corrected origin validation and useful real-tool cases. Request 052 proved that
unchecked Python bytecode could execute; 053 passed bytecode isolation, shadow
rejection and real useful checking. These are development results, not a final
candidate certificate.

Historical schema 1 measurements in request 020 used Windows, CPython 3.12,
released mypy 2.3.1, two sources, one static import and an environment with 2,058
file/interpreter records totalling 44,066,955 bytes. Four attempts produced:

| Attempt | Completed outcome | Public wall ms | Provider ms | Analysis process ms |
| --- | --- | ---: | ---: | ---: |
| Cold | failed with expected typing diagnostic | 8515 | 8187 | 2578 |
| Warm unchanged 1 | unchecked deadline | 10360 | 9922 | unavailable |
| Warm unchanged 2 | failed with expected typing diagnostic | 9875 | 8922 | 2343 |
| Signature invalidation | passed | 6593 | 6125 | 1703 |

This is one cold, two warm and one invalidation attempt, before subsequent tool
isolation changes. It does not establish a percentile, a current performance
improvement or aggregate synchronous readiness. Host rehash, Ruff, secrets,
integrity and actual event delivery were absent. The incomplete warm attempt
remains incomplete. The owner selected explicit background execution instead of
weakening freshness or supplying a fresh synchronous allowance.

The opt-in `test_typing_measurements` separately records completed schema 2 cold,
warm and invalidation calls, real mypy version-process startup, its full graph size,
application phase costs, environment size and equivalent delivery rehash cost.
It asserts completed equivalent work; a deadline is a failure of that measurement.
Its filesystem rehash is not an independently installed host implementation and
cannot certify active-turn delivery or the host's aggregate budget.

Request 054 completed that measurement on Windows with CPython 3.12, released
mypy 2.3.1 and policy v2.4.2. Each call analyzed two sources and one static import;
the real full-directory graph contained 48 nodes. The environment contained
2,058 file/interpreter records and 44,066,919 bytes. Six tool RECORD files bound
1,535 hashed entries totalling 36,779,373 bytes. No persistent mypy cache was reused.

| Completed schema 2 call | Outcome | Public wall ms | Analysis process ms | Equivalent delivery rehash ms | Wall plus rehash ms |
| --- | --- | ---: | ---: | ---: | ---: |
| Cold | expected diagnostic | 7453 | 2046 | 1141 | 8594 |
| Warm unchanged 1 | expected diagnostic | 19453 | 15750 | 1062 | 20515 |
| Warm unchanged 2 | expected diagnostic | 5375 | 1656 | 1063 | 6438 |
| Signature invalidation | passed | 5406 | 1672 | 1047 | 6453 |

The separate `-I -B -m mypy --version` process completed in 250 ms. That startup
command differs from the provider's isolated bootstrap and is not subtracted
from analysis time. An additional completed application profile measured initial
environment selection at 4,326 ms and final environment verification at 1,681 ms.
The two environment tree hashes together took 3,198 ms; six RECORD validations
reusing the captured hashes took 114 ms; two release verifications took 224 ms.
These inclusive phase times overlap. The profile's mypy process took 1,326 ms.

There was one cold, two warm and one invalidation call, under uncontrolled shared
machine load. The slow warm call completed equivalent work; its cause is unknown.
These samples do not establish a percentile or aggregate installed-event budget.
In particular, a cold edit is not hidden in setup and a warm edit does not reuse
an incremental database. The explicit background contract retains a thirty-second
bound and returns unchecked if that bound or final delivery freshness cannot be met.

## Immutable release and host handoff

Owner merge and the normal [release procedure](../release.md) must produce verified
Windows and Linux archives for the exact merged source, with matching wheel,
release inventory and artifact digests. No existing cached release is replaced.
The host must select that published engine explicitly while retaining its chosen
policy, then validate actual installed events, active-turn findings, silent clean
completion, cancellation/replacement, disablement/rollback, Stop/drain and aggregate
profiles. Pending or undelivered work remains incomplete. This provider PR cannot
mark the installed ticket done or establish a published release.
