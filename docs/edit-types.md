# Working-tree typing interface

Paths are relative to the Quality Gate repository root. This is the provider
contract for the skills repository's `python-quality-hooks` ticket 04. The host
owns attribution, ordering after Ruff, the common event deadline and delivery.
The provider owns scope, exact released mypy semantics and prerequisite checks.
Source integration and an open PR establish neither publication nor activation.

## Request

Invoke the immutable interface engine with `quality-gate edit-types` (or
`python -I -B -m quality_gate.cli edit-types`). The native release launcher may
instead execute the verified wheel using its documented `PYTHONPATH` route,
without outer `-I`. Close stdin after one UTF-8 JSON object. No project code,
plugins or imports are executed during prerequisite inspection.

```json
{
  "schema": 1,
  "root": "/absolute/project",
  "files": [
    {"path": "app/api.py", "sha256": "<64 lowercase hex digits>"},
    {"path": "app/deleted.py", "sha256": null},
    {"path": "requirements.txt", "sha256": "<64 lowercase hex digits>"}
  ],
  "deadline_unix_ms": 1792512009000,
  "baseline": {"policy_release": "v2.4.2"},
  "cache_dir": "/absolute/external/quality-cache"
}
```

All keys except `baseline` and `cache_dir` are required; unknown/duplicate keys
are invalid. `schema` is integer 1. `root` is an absolute existing directory.
`files` contains at most ten distinct explicit root-relative regular file paths,
including configuration/dependency mutations; conflicting duplicate identities
are invalid. Each record has exactly `path` and `sha256`. A string hash binds raw
bytes; null binds actual absence, including at final freshness verification.
Symlinks, reparse points, hardlinks and path escapes are unsupported.
`deadline_unix_ms` is an integer absolute Unix timestamp in milliseconds, at most
10 seconds ahead; it consumes the host's remaining common budget, including
startup and delivery. Owned process cleanup has at most three separate seconds.
There is no background work or daemon. Empty input is `not_applicable`.

`baseline` has exactly one nonempty string `policy_release`; it is mandatory
only for unmanaged scope. Invalid managed manifests never fall back. `cache_dir`
is an optional absolute external cache root, outside the project and all analyzed
source roots, with no unsafe path links. The default is the ordinary Gate cache.
Editing never downloads, installs, provisions or changes immutable releases.

## Result

Exactly one bounded JSON object is written to stdout; stderr and raw mypy output
are never forwarded. The maximum response is 256 KiB. All arrays are deterministic
and ordered by path, except `selections` and `components`, which follow component
declaration order. SHA-256 strings below contain 64 lowercase hexadecimal digits.
Identity digests use UTF-8 JSON with sorted object keys, ASCII escaping and compact
separators, without a trailing newline. Host validation rejects unknown schemas,
inconsistent exit/status, incomplete evidence and expired/stale results.

```json
{"schema":1,"check_id":"mypy","engine_version":"3.1.0","status":"failed","reason":null,"action":null,"writes_may_have_occurred":false,"root":"/absolute/project","root_identity":{"device":1,"inode":123},"files":[{"path":"app/api.py","input_sha256":"<sha256>","final_sha256":"<sha256>"}],"configuration":[{"path":"quality-gate.toml","sha256":"<sha256>"}],"selections":[{"policy_release":"v2.4.2","release_sha256":"<sha256>","policy_sha256":"<sha256>","mypy_version":"2.3.1","mypy_sha256":"<sha256>","python":"/external/runtime/python","python_version":"3.12","python_sha256":"<sha256>","base_python":"/system/python","base_python_sha256":"<sha256>","runtime_fingerprint":"<sha256>","environment_sha256":"<sha256>","environment":{"path":"/external/runtime","sha256":"<sha256>","file_count":100,"bytes":1000000}}],"components":[{"name":"app","path":"app","coverage":"managed","status":"failed","reason":null,"selection":0,"inventory":[{"path":"app/api.py","sha256":"<sha256>"},{"path":"app/consumer.py","sha256":"<sha256>"}],"directories":[{"path":"app","entries":[{"name":"api.py","kind":"file"},{"name":"consumer.py","kind":"file"}]}],"scope_sha256":"<sha256>","dependency_inputs":[{"path":"requirements.txt","sha256":"<sha256>"}],"diagnostics":[{"path":"app/consumer.py","line":4,"column":9,"code":"arg-type"}],"source_count":2,"import_count":1,"analysis_ms":120}],"freshness_sha256":"<sha256>","process_count":4,"elapsed_ms":450}
```

`engine_version` is the executing package version; the host launcher verifies the
immutable wheel. `root` is canonical absolute; `root_identity` has nonnegative stat
`device`/`inode` revalidated at completion. `files` contains each normalized request
path and matching nullable input/final hashes. `configuration` records present and
absent named files. Selections bind exact release, policy, mypy, interpreter,
runtime fingerprint and environment tree. `python` is `Scripts/python.exe` on
Windows or `bin/python` on POSIX inside `environment.path`. Managed coverage uses
the prepared component runtime; baseline uses exactly `root/.venv`. Components
share a selection only when every provenance field matches. Policy and tools come
from the verified `releases/<policy_release>` inventory. `base_python` is the
canonical isolated-probe executable and `base_python_sha256` binds its bytes.
Managed ownership is `<cache>/runtimes/<repository-key>/<runtime_fingerprint>`,
where repository-key is the first 32 hex digits of SHA-256 of the casefolded,
UTF-8 canonical root. The host validates managed and baseline ownership before
opening environment evidence.

`components[].path` is root-relative (`.` for unmanaged), `coverage` is `managed`
or `baseline`, and `selection` is a zero-based index or null for an opt-out.
`inventory` contains every analyzed `.py`/`.pyi` source; for managed coverage it
is the union of `component.path` and the component's declared `source_roots`.
`directories` captures the complete scanned directory membership for those roots,
including empty directories. `scope_sha256`
hashes `{inventory,directories}`. `dependency_inputs` uses raw-byte hashes of
declared dependency files. A diagnostic has exactly `path`, positive integer
`line`/`column` (one-based), and nonempty mypy `code`. It can refer to an unchanged
source in that component's inventory. Diagnostics contain no messages, identifiers
from source, source fragments, revealed types, literal values or exceptions.
`source_count`, `import_count`, `analysis_ms`, `process_count` and `elapsed_ms`
are nonnegative integers. `import_count` counts static import statements in the
analyzed source, not mypy's transitive dependency graph size.

`freshness_sha256` hashes `{root,root_identity,files,configuration,selections,components}`
without component `analysis_ms`. There is no `source_roots` result key. The host
obtains roots from the verified manifest by component name and path, validates
schema, request coverage and selections, then rechecks root stat, input presence
and hashes, configuration, environment and source inventory. It re-enumerates
each recorded directory and compares immediate names and kinds using the same
coverage exclusions. The records must be exactly the recursive closure of the
component and declared roots, including empty directories and one record per
included `.py` and `.pyi`, even shadowed `.py` files. Reject missing, extra,
undeclared, duplicate or orphan paths and inconsistent membership. A change after
the final check needs a new event; the evidence is not atomic thereafter.

All results contain schema/check/engine/status/reason/action/write flag, `files`,
`configuration`, `selections`, `components`, process count and elapsed milliseconds.
For an early `unchecked`, root/root_identity/freshness_sha256 are null and evidence
arrays may be empty; these are never usable coverage certificates. `reason` and
`action` are fixed source-free strings for unchecked, otherwise null. Component
opt-outs have `status: not_applicable`, `reason: typecheck_disabled`, null selection,
empty inventory/directories/dependency_inputs/diagnostics, and zero counters.
Top-level `not_applicable` also covers empty/non-Python/outside managed scope;
it retains validated request file/configuration/root evidence, has no selections,
and empty components unless there are affected opt-outs. It must not be interpreted
as completed typing analysis. Missing-root/invalid-request failures use this exact
early-failure shape (with real engine version and elapsed/process counters):

```json
{"schema":1,"check_id":"mypy","engine_version":"3.1.0","status":"unchecked","reason":"invalid_request","action":"Send a valid schema 1 typing request with current identities.","writes_may_have_occurred":false,"root":null,"root_identity":null,"files":[],"configuration":[],"selections":[],"components":[],"freshness_sha256":null,"process_count":0,"elapsed_ms":0}
```

No optional `stage` or free-form exception fields are emitted. Reason enum:
`invalid_request`, `deadline`, `unsafe_path`, `limits`, `stale_input`, `manifest`,
`unsupported_policy`, `release`, `runtime`, `tool`, `configuration`, `dependencies`,
`tool_output`, `io`. Each maps to a fixed recovery action in the provider; host
rendering may replace it with a local safe message. Other reason values are invalid.

## Fixed freshness algorithms

All path arrays use case-sensitive Unicode code-point ordering of normalized
POSIX relative paths. Reject control characters, `.`/`..` segments, backslashes,
colon, absolute paths and duplicate paths. Source files have one hardlink and no
symlink/reparse-point path segments. Directory entries have exactly string `name`
and `kind: file|directory`; reject other filesystem kinds. Root directory path is
`.` only for baseline. Files in `configuration` may be absent; other file evidence
is required to exist.

Configuration names are exactly `quality-gate.toml`, `pyproject.toml`, `mypy.ini`,
`.mypy.ini`, `setup.cfg`, `requirements.txt`, `requirements-dev.txt`, `uv.lock`,
`poetry.lock`, `Pipfile`, `Pipfile.lock`. Capture all eleven names at the project
root, including absent records, and `pyproject.toml`, `mypy.ini`, `.mypy.ini`,
`setup.cfg`, `quality-gate.toml` in every included source directory and its ancestors
inside root. Hash each existing `py.typed` marker in included source directories.
Also capture `__init__.py`, `__init__.pyi` and `py.typed` at every managed component
and source-root ancestor, including absent paths. Union these with every affected
component's declared dependency inputs and request configuration/dependency paths.
Deduplicate by relative path. Directory membership records absent markers inside
included source directories. Configuration mutation routes all components;
declared dependency mutation routes all components declaring that input. A deleted
Python path routes by its lexical component membership. Unrecognized non-Python
paths are inapplicable. Project mypy config does not override released policy;
malformed or ambiguous dependency/project settings are unchecked.

`environment` has exactly absolute canonical directory `path`, `sha256`, integer
`file_count` and `bytes`; `environment_sha256` equals its `sha256`. Recursively
collect records sorted by relative `path`, then hash their canonical JSON array:

- Directory: `{path,kind:"directory"}`, including `.`.
- Regular file: `{path,kind:"file",sha256,size}`; hash raw bytes and sum sizes.
- POSIX `bin/` aliases named `python`, `python3`, or `python3.<digits>`:
  `{path,kind:"interpreter",target,sha256,size}`. Resolve to an executable regular
  file, bind and revalidate its canonical target and bytes, and count it in the
  file/byte totals. `python_sha256` binds the final target; Windows binds the
  regular redirector and the isolated probe also binds base executable bytes.
- A root `lib64` link exactly to root `lib`: `{path:"lib64",kind:"alias",target:"lib"}`;
  do not traverse it twice.

Skip only `__pycache__` directories and regular `.pyc`/`.pyo` files. Other links,
reparse points and hardlinks are unsupported. Include a base executable outside
the environment as `{path:"@base-python",kind:"interpreter",target,sha256,size}`.
Cap at 20,000 records and 512 MiB, checking the deadline during traversal and reads.
Missing or empty `pyvenv.cfg`, system site packages, unexpected site paths, editable
dependencies, or unknown/changed `.pth` entries are unchecked. Blank and comment-only
`.pth` lines are allowed. Managed runtimes also allow exact known `a1_coverage.pth`,
`pytest-cov.pth` and `distutils-precedence.pth` payloads, hashed after CRLF-to-LF
normalization. Validate, never execute, their contents. The isolated probe uses
`-I -S -B`; mypy receives verified site directories without startup or project
plugins. Other executable/path-bearing entries or malformed/changed payloads are
unchecked. Update the allowlist only for a changed managed dependency payload.
The host repeats this tree operation for delivery freshness; measure provider and
host rehash costs before activation.

## Background execution, schema 2

Schema 1 remains synchronous with its original maximum ten-second common event
deadline. Schema 2 is additive and exclusively requests a bounded background
invocation. The provider still runs one ordinary supervised CLI process and emits
one final response. It creates no daemon, watcher or persistent event state.

A schema 2 request adds the required fields below to the schema 1 request:

```json
{"schema":2,"mode":"background","event_id":"13e5496e-56be-4a23-883a-26bbfa3f8291","cancel_path":"/absolute/external/quality-cache/edit-types-events/13e5496e-56be-4a23-883a-26bbfa3f8291.cancel"}
```

Schema 2 requires `mode: "background"`, a canonical lowercase UUID `event_id`,
and absolute canonical `cancel_path` equal to
`<canonical cache root>/edit-types-events/<event_id>.cancel`. Cache and signal
ancestors must be ordinary directories without links or reparse points; a present
signal is a regular single-link file, while an absent signal or events directory
is valid. Only the host creates the signal. The provider checks before launch, at
most every 50 ms during work, and before delivery. On cancellation it terminates
and reaps the worker tree and returns exit 2, `unchecked / cancelled`. Work has
one deadline at most 30 seconds ahead, including startup and freshness; cleanup
gets at most three additional seconds. Schema 1 keeps its original deadline.

A valid response has the schema 1 fields plus `schema: 2`, `mode` and matching
`event_id`, including early unavailable or cancelled responses. Invalid envelopes
use schema 1 `invalid_request` and carry no trusted event. Schema 2 hashes the
schema 1 freshness object plus `schema`, `mode` and `event_id`. Its additional
reason is `cancelled`; all other status, limit, privacy and freshness rules remain.
The provider emits a final response, never `pending` or proof of host delivery.

The host owns one worker per root and current event identity. It cancels and reaps
the previous worker before replacement. Pending and clean results stay silent;
findings are delivered at a later active-turn boundary only after validation, event
matching and independent freshness checks within that event's budget. Stop/drain
waits for owned work and reports required incomplete checking. Pending, cancelled,
stale, timed-out or undelivered work never counts as success. Disablement, shutdown
and rollback reap work and retire event state. Registration, trust, reload and
actual delivery acceptance remain host responsibilities.

Exit 0 means `passed` or `not_applicable`; exit 1 means `failed`; exit 2 means
`unchecked`. Any unavailable component makes the entire invocation unchecked;
partial successful evidence is not returned as a clean result. At most 1,024
sources, 16 MiB source bytes, 200 diagnostics and 256 KiB encoded evidence are
supported; overflow is unchecked, never truncated success.

## Scope, prerequisites and compatibility

The v4.5.0 engine supports the released matrix through v4.5.0. The v4.6.0
candidate retains those pins and adds v4.6.0 for isolated testing only with
the exact verified immutable policy in cache.

Managed coverage selects the manifest's exact release, affected components'
declared Python and prepared dependencies. It analyzes each whole component,
including unchanged consumers. Python paths route by component and declared
`source_roots`; manifest/config changes route all components, dependency changes
route their consumers, and deletions use lexical membership and must remain absent.
Files outside components are inapplicable. `python[].typecheck` defaults true; false
is an explicit opt-out. Policies through v3.0.0 reject even an explicit true;
v3.1.0 adds the field. Older manifests retain their policy selection.

Managed discovery skips dot-prefixed names, `__pycache__`, `site-packages` and
`node_modules`, but includes visible `build`, `dist` and `temp`. Capture both `.py`
and same-stem `.pyi` even when mypy analyzes only the latter. Preserve ancestor
package identity in the isolated snapshot. Unmanaged coverage analyzes the explicit
root using only `root/.venv` without system site packages, requires a baseline, and
rejects nested project boundaries or conflicting settings. It additionally excludes
`.git`, `.venv`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`,
`build`, `dist`, `node_modules`, `temp`, and `.quality-gate-edit-*` directories
from discovery and directory evidence. Unsafe links and layouts are unchecked.

The exact released `mypy.ini` applies, including `follow_imports=skip` and
`ignore_missing_imports=True`, without typing waivers. Static preflight never runs
project imports or plugins; it requires each import in captured source, stdlib,
typeshed, installed typed packages or stubs, or a declared local stub root. Missing
or ambiguous context returns `dependencies` despite `ignore_missing_imports`.
Dynamic imports, untyped functions and skipped external bodies remain policy limits.

Managed `stub_roots` are unique existing POSIX-relative directories inside the
component, passed from the snapshot via `MYPYPATH`; their `.pyi` inventory and
membership govern freshness. Unmanaged projects cannot declare them. Policy v3.2.0
or newer is required. `source_roots` require v4.6.0 or newer and are unique, existing
repository-relative directories, without traversal, links, duplicates or overlap
with one another or the component. A component at `.` cannot declare them; separate
components may share them. Sort roots and capture them with the component under the
same limits. Preflight uses repository-qualified and source-root-relative names;
`MYPYPATH` includes only the captured tree and declared source/stub roots.

Changes to shared sources, membership, initializers, `py.typed` contents or import
configuration invalidate results. Hash `.pyi` and existing markers; directory
membership detects additions and removals. Do not list raw stub or marker paths in
`dependency_inputs`: use the supported requirements files or a single
`pyproject.toml`. After publication and explicit consumer pin updates, sync the
exact platform asset and run `edit-setup`, `setup` and `doctor`. Do not select an
unpublished candidate policy.

Setup is explicit: sync the exact immutable policy, run managed `setup` and
`doctor`; unmanaged users prepare `.venv` and its declared dependencies/stubs
explicitly. Editing uses no global Python fallback. Corrupt installed tools or
changed dependencies require explicit repair/setup. Mypy analysis caches are private
to one invocation and removed with its workspace; there is no persistent typing-result
cache, incremental database or daemon. Warm filesystem pages do not reuse prior analysis.
Follow the [interface verification requirements](release.md#working-tree-interface-verification)
for same-release parity, cold/warm measurements and host activation.
