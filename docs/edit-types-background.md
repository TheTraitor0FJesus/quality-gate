# Edit-types background execution and compatibility

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
freshness evidence; owned cleanup has the same separate maximum three seconds.
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

The published v4.5.0 engine retains the released exact policy matrix through
v4.5.0. This v4.6.0 candidate retains every historical supported pin and adds
its own v4.6.0 policy for isolated candidate-only testing. A candidate wheel may
use v4.6.0 only when its verified cache contains that exact immutable policy.

Managed: use the working-tree manifest's exact release, each affected component's
declared Python and already prepared Gate runtime/dependencies. Analyze the whole
component, including unchanged consumers. Python paths route by component path
and each managed component's declared `source_roots`; manifest and recognized
configuration changes route every component, and declared dependency changes
route their consumers. Deletions retain their old path for routing and must still
be absent at completion. Files outside managed components are explicitly
inapplicable; they do not override a component's opt-out.

Policy v3.1.0 added optional schema 2 `python[].typecheck: boolean` (default
true). Full Gate and edits honor false. Policies through v3.0.0 do not support
this field: explicitly using it with an older selected policy is unchecked,
including explicit true. Existing manifests without it retain their exact
v2.4.2/v2.5.0/v2.6.0/v2.6.1/v2.6.2/v3.0.0 policy selection. Host activation
remains a separate operation.

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
Before mypy runs, the import-context preflight requires imported modules to be
available from captured component sources, the standard library, typeshed, an
installed typed package, an installed `*-stubs` package, or a declared local stub
root. Missing typing context returns `dependencies`, even when the selected mypy
flags use `ignore_missing_imports = true`. The preflight does not change the flags
passed to mypy. External implementation bodies skipped by the policy, untyped
functions allowed by it, dynamic imports and runtime-dependent typing remain policy
limits, not demonstrated correctness.

Managed components may declare local stubs with `stub_roots` in their `python[]`
manifest table. Each entry is a unique POSIX-relative directory path from the
component root, such as `stub_roots = ["stubs"]`. The directory must exist and be
inside the component tree captured by the provider. Absolute paths, traversal,
missing directories, and paths outside the component make the result unchecked.
The provider passes these roots from its captured component snapshot to isolated
mypy through `MYPYPATH`, separated with the platform's path separator. Stub `.pyi`
files are part of component inventory and directory evidence, so
content or membership changes invalidate freshness. Unmanaged projects do not accept
local stub roots. Installed typed dependencies and `*-stubs` packages continue to
resolve from the selected environment. The setting requires policy v3.2.0 or newer.

Managed components may declare `source_roots` as unique repository-root-relative
POSIX directory trees, for example `source_roots = ["scripts", "apscheduler"]`. The
roots are captured together with `component.path` using the same managed
exclusions, source/byte/directory limits and read-only isolated snapshot. They
must exist as ordinary directories inside the repository. Absolute paths,
traversal, links, duplicate or overlapping roots, and overlap with the component
path are unchecked. A component with `path = "."` cannot declare source roots.
Source roots are sorted for deterministic evidence; separate
components may declare the same shared tree.

The provider preflights imports against repository-qualified module names and
source-root-relative names, then adds only the isolated captured project tree,
declared source roots and component-local stub roots to `MYPYPATH`. This supports
layouts such as `bot` importing `scripts.rendering`, with a repository-root
`apscheduler` stub package; it never searches arbitrary working-tree modules.
Changes to shared files, additions/removals/renames, package initializers,
`py.typed` contents and import-resolution configuration invalidate the result.
`.pyi` and existing `py.typed` bytes are hashed; marker additions/removals inside
included directories are covered by directory membership. Do not put raw
`.pyi` or `py.typed` paths in `dependency_inputs`. Keep dependency metadata to the
project's supported requirements files or its single `pyproject.toml`. This
setting requires policy v4.6.0 or newer.

An ARGUS-style managed component can declare its existing sibling trees directly:

```toml
[[python]]
name = "bot"
path = "bot"
python_version = "3.12"
dependency_inputs = ["bot/requirements.txt", "bot/requirements-dev.txt"]
test_paths = ["bot/tests"]
tests_applicable = true
source_roots = ["scripts", "apscheduler"]
```

Use only the requirements files that the consumer maintains. If its dependency
source is `pyproject.toml`, declare that file alone instead of mixing it with
requirements inputs. Keep APScheduler in the real dependency metadata and place
its local `.pyi` files and `py.typed` marker under the repository-root `apscheduler/`.
In the current ARGUS layout, the root `pyproject.toml` describes the separate stub
distribution; the bot still declares its dependencies through the two bot requirements
files, so do not add that `pyproject.toml` to the bot's `dependency_inputs`. After
publication and the consumer's explicit policy/workflow update, use the v4.6.0 Windows
asset and prepare the consumer:

```powershell
quality-gate sync --url https://github.com/TheTraitor0FJesus/quality-gate/releases/download/v4.6.0/quality-gate-v4.6.0-Windows.zip --version v4.6.0
quality-gate edit-setup --policy-release v4.6.0
quality-gate setup
quality-gate doctor
```

Do not add stub paths to
`dependency_inputs` or select the candidate before publication.

Setup is explicit: sync the exact immutable policy, run managed `setup` and
`doctor`; unmanaged users prepare `.venv` and its declared dependencies/stubs
explicitly. Editing uses no global Python fallback. Corrupt installed tools or
changed dependencies require explicit repair/setup. Mypy analysis caches are private
to one invocation and removed with its workspace; there is no persistent typing-result
cache, incremental database or daemon. Warm filesystem pages do not reuse prior analysis.
Follow the [interface verification requirements](release.md#working-tree-interface-verification)
for same-release parity, cold/warm measurements and host activation.
