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
{
  "schema": 1,
  "check_id": "mypy",
  "engine_version": "3.1.0",
  "status": "failed",
  "reason": null,
  "action": null,
  "writes_may_have_occurred": false,
  "root": "/absolute/project",
  "root_identity": {"device": 1, "inode": 123},
  "files": [{"path": "app/api.py", "input_sha256": "<sha256>", "final_sha256": "<sha256>"}],
  "configuration": [{"path": "quality-gate.toml", "sha256": "<sha256>"}],
  "selections": [{
    "policy_release": "v2.4.2",
    "release_sha256": "<sha256>",
    "policy_sha256": "<sha256>",
    "mypy_version": "2.3.1",
    "mypy_sha256": "<sha256>",
    "python": "/external/runtime/python",
    "python_version": "3.12",
    "python_sha256": "<sha256>",
    "base_python": "/system/python",
    "base_python_sha256": "<sha256>",
    "runtime_fingerprint": "<sha256>",
    "environment_sha256": "<sha256>",
    "environment": {"path": "/external/runtime", "sha256": "<sha256>", "file_count": 100, "bytes": 1000000}
  }],
  "components": [{
    "name": "app",
    "path": "app",
    "coverage": "managed",
    "status": "failed",
    "reason": null,
    "selection": 0,
    "inventory": [{"path": "app/api.py", "sha256": "<sha256>"}, {"path": "app/consumer.py", "sha256": "<sha256>"}],
    "directories": [{"path": "app", "entries": [{"name": "api.py", "kind": "file"}, {"name": "consumer.py", "kind": "file"}]}],
    "scope_sha256": "<sha256>",
    "dependency_inputs": [{"path": "requirements.txt", "sha256": "<sha256>"}],
    "diagnostics": [{"path": "app/consumer.py", "line": 4, "column": 9, "code": "arg-type"}],
    "source_count": 2,
    "import_count": 1,
    "analysis_ms": 120
  }],
  "freshness_sha256": "<sha256>",
  "process_count": 4,
  "elapsed_ms": 450
}
```

The example hashes and timestamp are placeholders. `engine_version` is the
executing engine's package version; its immutable wheel identity remains the
host launcher's trust responsibility. `root` is canonical absolute, and
`root_identity` contains nonnegative integer `device`/`inode` from stat before
analysis, revalidated at completion. `files` includes every normalized request
path with nullable input/final hashes; this interface is read-only, so they match.
`configuration` captures existing and absent named configuration files using
nullable hashes. `selections` contains exact policy/release/tool/interpreter
identities, major.minor Python version, runtime fingerprint, and an absolute
environment tree record described below. `python` must be inside `environment.path`
at `Scripts/python.exe` (Windows) or `bin/python` (POSIX). For managed coverage the
environment path is the prepared runtime selected by the working-tree component
contract; for baseline coverage it is exactly `root/.venv`. Multiple components
may share a selection only when every provenance field is identical. Policy files
and tools belong to the verified cache's `releases/<policy_release>` inventory;
they never come from the project, baseline substitution, or the engine's policy.
`base_python` is the canonical absolute base executable reported by the isolated
probe; `base_python_sha256` binds its bytes. Managed environment ownership is
`<cache>/runtimes/<repository-key>/<runtime_fingerprint>`, where repository-key is
the first 32 hex digits of SHA-256 of the canonical root string casefolded and
UTF-8 encoded, matching existing Gate setup. The host validates this ownership
and baseline `.venv` ownership before opening environment evidence paths.

`components[].path` is root-relative (`.` for unmanaged), `coverage` is `managed`
or `baseline`, and `selection` is a zero-based index or null for an opt-out.
`inventory` contains every analyzed `.py`/`.pyi` source; `directories` captures
the scanned directory membership, including empty directories. `scope_sha256`
hashes `{inventory,directories}`. `dependency_inputs` uses raw-byte hashes of
declared dependency files. A diagnostic has exactly `path`, positive integer
`line`/`column` (one-based), and nonempty mypy `code`. It can refer to an unchanged
source in that component's inventory. Diagnostics contain no messages, identifiers
from source, source fragments, revealed types, literal values or exceptions.
`source_count`, `import_count`, `analysis_ms`, `process_count` and `elapsed_ms`
are nonnegative integers. `import_count` counts static import statements in the
analyzed source, not mypy's transitive dependency graph size.

`freshness_sha256` hashes `{root,root_identity,files,configuration,selections,components}`
with component `analysis_ms` removed. The host first validates schema, request
coverage and selection indices, then rechecks root stat, request absences/hashes,
configuration, environment tree and source inventory. To detect added/deleted
sources, it re-enumerates each recorded directory and compares its immediate
`entries` exactly (name and kind); a new directory therefore also invalidates the
result. This is a fixed filesystem freshness operation, not policy routing.
The coverage-specific exclusion set below applies identically to recorded and
rechecked directory entries. Every component has its own root directory record
and exactly one directory record for every reachable included child directory;
duplicate paths, missing children, orphan records, duplicate entries and an
inventory inconsistent with the directory records invalidate the certificate.
Every included `.py`/`.pyi` entry has exactly one inventory record, including a
`.py` shadowed by `.pyi` under normal mypy discovery. Any mismatch rejects the
complete result. Evidence does not promise
atomicity after the host's final check; subsequent edits require a new event.

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
inside root. Union these with every affected component's declared dependency
inputs, request configuration/dependency paths, and ancestor `__init__.py`/
`__init__.pyi` files above a component root (record absent ancestors too).
Deduplicate by relative path. Configuration mutation routes all components;
declared dependency mutation routes all components declaring that input. A deleted
Python path routes by its lexical component membership. Unrecognized non-Python
paths are inapplicable. Project mypy config does not override released policy;
malformed or ambiguous dependency/project settings are unchecked.

`environment` has exactly absolute canonical directory `path`, `sha256`, integer
`file_count`, integer `bytes`. `environment_sha256` equals its `sha256`. Compute
the digest by recursively collecting these records, sorted by relative `path`:

- Directory: `{path,kind:"directory"}`, including `path:"."`.
- Regular file: `{path,kind:"file",sha256,size}`; hash raw bytes and sum `size`.
- POSIX interpreter alias under `bin/`, basename matching `python`, `python3`, or
  `python3.<digits>`: `{path,kind:"interpreter",target,sha256,size}`. Resolve the
  complete link chain to an executable regular file, bind canonical absolute
  `target` and its bytes, and revalidate both link resolution and bytes. Count the
  record in `file_count`/`bytes`. The selected `python_sha256` is its final target
  digest; on Windows it binds the regular venv redirector and the isolated runtime
  probe additionally binds the actual base executable bytes in this tree's digest.
- POSIX root `lib64` linking exactly to root `lib`:
  `{path:"lib64",kind:"alias",target:"lib"}`; do not traverse twice.

Skip directory `__pycache__` and regular filenames ending `.pyc`/`.pyo` only.
Other environment links/reparse points/hardlinks are unsupported. Include the
isolated probe's actual base Python executable as
`{path:"@base-python",kind:"interpreter",target,sha256,size}` when it is outside
the environment. Hash the canonical JSON array of records with the identity
serialization above. Cap at 20,000 records and 512 MiB total bytes; check the
remaining deadline while walking and reading. An empty/missing `pyvenv.cfg`,
`include-system-site-packages` other than false, unexpected site paths, `.pth`
files or editable dependency contexts are unchecked. A `.pth` containing only
blank lines or comments is allowed; executable/path-bearing `.pth` is unchecked.
The probe runs isolated
with `-I -S -B` and emits only standard-library metadata; mypy gets explicit
verified site directories without executing site startup or project plugins.
The host performs the same tree operation for final delivery freshness; caps
bound work, not a latency guarantee. Measure provider initial/final and host
rehash together before activation.

## Background execution, schema 2

Schema 1 remains synchronous with its original maximum ten-second common event
deadline. Schema 2 is additive and exclusively requests a bounded background
invocation. The provider still runs one ordinary supervised CLI process and emits
one final response. It creates no daemon, watcher or persistent event state.

```json
{
  "schema": 2,
  "mode": "background",
  "event_id": "13e5496e-56be-4a23-883a-26bbfa3f8291",
  "cancel_path": "/absolute/external/quality-cache/edit-types-events/13e5496e-56be-4a23-883a-26bbfa3f8291.cancel",
  "root": "/absolute/project",
  "files": [{"path": "app/api.py", "sha256": "<64 lowercase hex digits>"}],
  "deadline_unix_ms": 1792512030000,
  "baseline": {"policy_release": "v2.4.2"},
  "cache_dir": "/absolute/external/quality-cache"
}
```

The three additional required keys are exactly `mode: "background"`, canonical
lowercase UUID string `event_id`, and absolute canonical `cancel_path`. The path
must equal `<canonical cache root>/edit-types-events/<event_id>.cancel`, with the
same cache selection as the request. Cache and signal ancestors must be regular
directories without links/reparse points; a present signal is a regular file with
one hardlink. An absent signal is supported, including an absent events directory.
The host creates the signal to cancel; the provider never creates or removes it.
The provider checks it before launch, at most every 50 ms while waiting for its
worker, and before delivery. Cancellation terminates/reaps the owned worker and
descendant tree before returning `unchecked`, `reason: "cancelled"`, exit 2.
Work has one absolute deadline at most 30 seconds ahead, including startup and
provider evidence; owned cleanup has the same separate maximum three seconds.
Neither this allowance nor a later delivery event grants schema 1 more time.

Every valid schema 2 response has all schema 1 result fields, `schema: 2`, and
exactly two additional fields, `mode: "background"` and matching `event_id`.
This includes early unavailable/cancelled results after envelope validation.
Unparseable/invalid envelopes use the schema 1 `invalid_request` failure shape;
they carry no trusted event identity. Schema 2 `freshness_sha256` hashes the same
schema 1 freshness object plus top-level `schema`, `mode` and `event_id`.
All reason enums remain available, with `cancelled` added only for schema 2.
Status/exit semantics, limits, source-free diagnostics, inventories, selection
ownership and filesystem algorithms are unchanged. A provider result has no
`pending` status and does not itself establish host delivery.

The host owns one bounded worker per root, event IDs and current-input ownership.
A new event cancels and reaps the preceding worker before replacement. Pending
work is explicit in internal state and silent after each edit. Complete clean
results are silent. Findings enter model context at a later active-turn hook
boundary after host validation, request/event matching and independent freshness
checks within that delivery event's remaining common budget. A managed Stop/drain
must await/reap remaining work and report required unavailable/incomplete checking
before turn completion. Pending, cancelled, timed-out, stale or undelivered work
never counts as an immediate successful edit check. Disablement, shutdown and
rollback cancel/reap owned work and retire event state. Exact registration/trust,
reload, actual active-turn delivery and Stop/drain acceptance belong to the host.

Exit 0 means `passed` or `not_applicable`; exit 1 means `failed`; exit 2 means
`unchecked`. Any unavailable component makes the entire invocation unchecked;
partial successful evidence is not returned as a clean result. At most 1,024
sources, 16 MiB source bytes, 200 diagnostics and 256 KiB encoded evidence are
supported; overflow is unchecked, never truncated success.

## Scope, prerequisites and compatibility

The published v4.0.0 engine retains the previously supported exact policies through
v3.2.0 and supports its own v4.0.0 policy.

Managed: use the working-tree manifest's exact release, each affected component's
declared Python and already prepared Gate runtime/dependencies. Analyze the whole
component, including unchanged consumers. Python paths route by component path;
manifest and recognized configuration changes route every component, and declared
dependency changes route their consumers. Deletions retain their old path for
routing and must still be absent at completion. Files outside managed components
are explicitly inapplicable; they do not override a component's opt-out.

Policy v3.1.0 added optional schema 2 `python[].typecheck: boolean` (default
true). Full Gate and edits honor false. Policies through v3.0.0 do not support
this field: explicitly using it with an older selected policy is unchecked,
including explicit true. Existing manifests without it retain their exact
v2.4.2/v2.5.0/v2.6.0/v2.6.1/v2.6.2/v3.0.0 policy selection. The provider manifest
selects v4.0.0. Host activation remains a separate operation.

Managed discovery matches the pinned mypy directory discovery: skip names starting
`.` and names `__pycache__`, `site-packages`, `node_modules`; include `build`,
`dist`, `temp` and every other visible directory. `.pyi` shadows same-stem `.py`
for analysis, but both are captured for freshness. No unmanaged exclusion is
silently added to this managed certificate. Ancestor package identity is preserved
in an isolated source snapshot; mypy never analyzes mutable project files directly.

Unmanaged/non-Git: the explicit root is the single supported project; analyze
all its Python sources using only `root/.venv`, with system-site-packages disabled.
The baseline must be explicit. Nested project boundaries or conflicting settings
are unchecked. In addition to managed discovery skips, directory names `.git`, `.venv`, `__pycache__`, `.mypy_cache`,
`.pytest_cache`, `.ruff_cache`, `build`, `dist`, `node_modules`, `temp`, and names
starting `.quality-gate-edit-` are excluded from recursive discovery and directory
evidence. Source links and unsupported layouts are unchecked.

The exact released `mypy.ini` is used, including `follow_imports=skip` and
`ignore_missing_imports=True`; no typing waivers are added. An import prerequisite
preflight statically checks available source/stub/dependency context without
executing project imports or plugins. Missing/ambiguous context is unchecked.
This preflight does not tighten the released checker flags. External implementation
bodies skipped by the policy, untyped functions allowed by it, dynamic imports and
runtime-dependent typing remain policy limits, not demonstrated correctness.

Setup is explicit: sync the exact immutable policy, run managed `setup` and
`doctor`; unmanaged users prepare `.venv` and its declared dependencies/stubs
explicitly. Editing uses no global Python fallback. Corrupt installed tools or
changed dependencies require explicit repair/setup. Analysis caches and measured
cold/warm behavior are documented in the provider evidence before release.
