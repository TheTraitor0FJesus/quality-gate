# Working-tree Ruff interface (schema 1)

Paths in this document are relative to the Quality Gate repository root. This is the
provider contract for the `skills` repository's `python-quality-hooks` ticket 01.
The provider owns this CLI, policy compatibility, tool preparation and verification.
The host owns event attribution, settings, registrations, delivery and release activation.
An open provider PR does not install a hook or publish an immutable release.

## Invocation and request

Install the interface engine from the corrected Quality Gate **v2.6.1** wheel after
publication. Invoke
`quality-gate edit-check` or `python -I -B -m quality_gate.cli edit-check` with one UTF-8
JSON object on stdin and close stdin. The equivalent entry point is `quality-gate-edit`.
The regular module route dispatches before loading full Gate orchestration and is the
host integration route measured below. A completed transport emits one JSON object and
no raw tool output. Do not import provider internals or its mutable checkout.
The executable engine and the selected policy release are separate identities.

The verified native release launcher may instead put the absolute engine wheel on
`PYTHONPATH` and invoke `python -B -m quality_gate.cli edit-check` without extracting
or installing it. Do not add `-I` to that outer invocation: it ignores `PYTHONPATH`.
The supervised worker still uses `-I -S -B`, imports the explicit engine location,
and starts in its private same-volume workspace. The v2.6.0 engine fails with
`unchecked / io` in this archive-backed launch mode; upgrade the engine explicitly
to v2.6.1 after publication while retaining the selected policy pin.

```json
{
  "schema": 1,
  "root": "/absolute/project",
  "files": [
    {"path": "src/example.py", "sha256": "<64 lowercase hex digits of current bytes>"}
  ],
  "deadline_unix_ms": 1792065605000,
  "cache_dir": "/absolute/quality-cache",
  "baseline": {
    "policy_release": "v2.4.2",
    "python": "/absolute/preinstalled/python",
    "python_version": "3.12"
  }
}
```

`root` is required, absolute, and may be a non-Git directory. Paths are exact,
root-relative files, not globs or directories. Duplicate paths with the same hash are
coalesced; conflicting hashes are invalid. Hashes cover raw bytes, including line endings.
The host computes `deadline_unix_ms` immediately before launching the engine, from its
shared edit budget. The example timestamp is illustrative: send a fresh deadline, at
most 10 seconds ahead. Startup, selection, integrity, tools, writes and delivery share
that deadline. The host must close stdin and supervise the complete invocation with
the same deadline; the CLI also bounds request intake to 10 seconds. Allow a bounded
process-tree cleanup interval on timeout; no result from a timed-out invocation is clean.
`cache_dir` is optional (the normal Gate user cache is the default). Unknown keys and
unsupported schema versions are unchecked. An empty file list is explicitly inapplicable.

For a standalone non-Git baseline smoke, run this from the project using the installed
engine's Python, after provisioning v2.4.2 with `edit-setup`:

```python
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

root = Path.cwd().resolve()
path = "example.py"
request = {
    "schema": 1,
    "root": str(root),
    "files": [{"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}],
    "baseline": {
        "policy_release": "v2.4.2",
        "python": sys.executable,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
    },
    "deadline_unix_ms": int(time.time() * 1000) + 5000,
}
completed = subprocess.run(
    [sys.executable, "-I", "-B", "-m", "quality_gate.cli", "edit-check"],
    input=json.dumps(request).encode(), capture_output=True, timeout=7, check=False,
)
print(completed.returncode, json.loads(completed.stdout))
```

The smoke uses a five-second example budget plus a two-second outer cleanup allowance;
it does not establish the host's final event budget or deliver model-visible feedback.

`baseline` is optional only if every Python path is covered by a valid manifest component.
It is mandatory for unmanaged files, including files outside declared Python components.
It selects an exact policy and a preinstalled Python executable/version; it is never
a fallback for an invalid manifest, incompatible managed pin, or missing managed runtime.

## Selection, preparation and compatibility

| Selected policy | Interface engine | Coverage |
| --- | --- | --- |
| v2.4.2 | v2.6.1 | Supported exact old policy; initial skills consumer pin |
| v2.5.0 | v2.6.1 | Supported exact old policy |
| v2.6.0 | v2.6.1 | Supported exact published policy |
| v2.6.1 | v2.6.1 | Supported candidate policy, usable only after publication |
| Any other version | v2.6.1 | `unchecked / unsupported_policy`; explicit consumer upgrade required |

The compatibility adapter uses schema 2 and the same component path membership and
released Ruff configuration as these releases. Their Python manifest/runtime/Ruff
semantics are identical. It verifies the complete exact release inventory and every
SHA-256 through Gate's distribution verifier, including the old wheel, without executing
or modifying that wheel. It does not substitute the engine's policy for an older pin.
Cached releases are not quarantined or repaired during editing. An incompatible pin
uses the [existing explicit consumer update](agent-reference.md#consumer-release-update).

Preparation is explicit and may use the network; editing is offline:

1. Install the released interface engine wheel using the existing trusted release
   installation route, separately from any consumer's policy pin.
2. Run `quality-gate sync --source <platform-release.zip> --version <exact-policy>`
   (or the normal explicit `--url` route). Keep the previous release for rollback.
3. Run `quality-gate edit-setup --policy-release <exact-policy>` once per selected
   policy/platform/cache. Add `--cache-dir <cache>` consistently when using a custom cache.
   This verifies the release, extracts only its native Ruff wheel payload into an
   independent cache entry, and prints the tool path. It never changes a release.
   The native digest/size come from the verified wheel's RECORD; setup verifies the
   extracted bytes against that identity. Editing verifies the whole release and native
   executable, without decompressing its executable payload on every invocation.
4. Managed project: run `quality-gate --root <project> setup`, then `doctor` using
   the project's existing policy pin. Only the relevant already-current runtime is
   inspected by edits; unrelated components are never prepared or launched.
5. Baseline project: install Python 3.11+ explicitly using the normal system provisioning
   route. Configure its absolute executable and exact major.minor version in `baseline`.
   No Ruff pip installation, project dependency installation or virtual environment is
   needed for baseline linting: `edit-setup` supplies the verified standalone Ruff binary.

During each invocation the selected runtime is probed with isolated, no-site Python
(`-I -S`), so installed `.pth` startup hooks do not run. The supervised worker also skips
site initialization before reading its request. The selected interpreter's actual
major.minor must match the declaration. The response binds the interpreter bytes and
runtime fingerprint. POSIX interpreter symlinks are supported: their resolved target
and executable digest are captured before probing and rechecked before publication.
Source-file symlinks remain rejected. Managed identity includes working-tree dependency
hashes using Gate's existing runtime fingerprint normalization. A dependency change requires explicit
`setup`; the edit command cannot install or create environments. Ruff's `target-version`
remains the released `py311`, independent of component `python_version`.

## Checking and writes

Only explicit complete `.py` and `.pyi` files are considered. Existing violations are
included. Non-Python regular files are inapplicable. Missing/deleted inputs, symlinks,
junctions, hardlinks, escapes, unreadable inputs and stale hashes are unchecked before
writing. The host omits deleted paths from its live-file request and reports deletions
in its own attribution envelope.

Managed membership is the first declared component whose `path` contains the file,
matching the release's formatting selection. `test_paths` outside that component are
baseline coverage. Schema 2 has no Python component exclusion/typecheck/Ruff-waiver
fields. Ruff's released path exclusions are resolved for these exact files with
`--force-exclude`; an excluded file is inapplicable. Git ignore files do not suppress
explicit file arguments in the supported Ruff release. Ordinary Gate also checks its
tracked index snapshot outside the Git worktree. Ignore files are still captured as
working-tree configuration identities. The
explicit released configuration overrides project Ruff settings just as ordinary Gate
does. Released per-file test exceptions, inline `noqa` and file-level Ruff suppressions
remain active. Typed Gate waivers do not suppress Ruff or formatting in these policies.

Eligible files are batched by selected release and runtime. Work happens on isolated
copies with the same relative names in a supervisor-owned `.quality-gate-edit-*` directory
under the explicit root. Keeping this workspace on the same volume permits atomic file
replacement. The supervisor removes it after stopping the worker, including on timeout.
The pipeline is:

1. Ruff safe fix: only reviewed `I001` import ordering, `--no-unsafe-fixes`, explicit
   `F401` unfixable. All other enabled lint rules still run; unused imports survive.
2. Ruff format, even when the first step reports residual lint errors.
3. Ruff lint without fixes, JSON findings, then Ruff format `--check` on final bytes.

No tests, mypy, dependency/history/secret scanning, Git-index candidate, full-repository
discovery or setup is part of this operation. Relevant working-tree manifest,
configuration/dependency/ignore inputs are hashed. Original files and configuration
are checked for freshness before publishing. Each changed file is replaced atomically;
multi-file publication is fail-fast, not a cross-file transaction. An IO error, timeout
or concurrent change can leave a partially published batch, always `unchecked`; capture
fresh bytes and retry the entire batch. Results never claim checks for a stale version.

## Result and exit status

```json
{
  "schema": 1,
  "engine_version": "2.6.0",
  "root": "/absolute/project",
  "status": "failed",
  "reason": null,
  "files": [{
    "path": "src/example.py",
    "status": "failed",
    "coverage": "managed",
    "component": "app",
    "input_sha256": "<request digest>",
    "sha256": "<final checked bytes digest>",
    "changed": true,
    "selection": 0,
    "findings": [{"check_id":"ruff","rule":"F401","line":1,"column":8,"action":"Resolve Ruff F401 in the final file."}],
    "format": "passed"
  }],
  "selections": [{
    "policy_release": "v2.4.2",
    "release_sha256": "<release.toml digest>",
    "policy_sha256": "<released ruff.toml digest>",
    "ruff_version": "0.16.4",
    "ruff_sha256": "<native binary digest>",
    "python": "/absolute/component-runtime/python",
    "python_version": "3.12",
    "python_sha256": "<interpreter digest>",
    "runtime_fingerprint": "<managed runtime digest or baseline selection digest>"
  }],
  "inputs": [{"path":"quality-gate.toml","sha256":"<configuration digest>"}],
  "process_count": 8,
  "elapsed_ms": 480,
  "writes_may_have_occurred": true
}
```

The schema is additive only within version 1: readers must reject unknown schema
versions and validate required fields/types/status consistency before accepting results.
`selection` indexes `selections`. `inputs` contains configuration identities, never
contents. Absence of a relevant optional configuration file is recorded with `sha256: null`.
`process_count` counts direct provider starts (entry, worker, runtime probe and Ruff
commands). It excludes the host bridge, console launchers and OS interpreter redirectors;
the host must measure those separately for complete process accounting.
`elapsed_ms` starts at the JSON entry function, after Python module initialization;
measure external launch-to-response wall time for complete startup and delivery cost.
The final file hash, not the input hash, is the identity for subsequent read-only hooks.
The result is not a substitute for full Gate verification.

| Status | Exit | Meaning |
| --- | ---: | --- |
| `passed` | 0 | Every eligible file passed final lint and format checks |
| `failed` | 1 | Complete checks found residual lint/syntax/format failures |
| `not_applicable` | 0 | No eligible files (empty, non-Python or policy excluded) |
| `unchecked` | 2 | Incomplete verification; never accept as clean |

Unchecked replies use fixed reason/action codes, no exception text. `files: []` on
failure means discard earlier coverage, and `writes_may_have_occurred: true` requires
fresh capture before retry. Reasons include `invalid_request`, `deadline`, `unsafe_path`,
`limits`, `stale_input`, `manifest`, `unsupported_policy`, `release`, `runtime`, `tool`,
`configuration`, `tool_output` and `io`.
If output delivery blocks or fails, the process exits 2 at the common deadline and may
leave no complete JSON object. Missing, partial or malformed output, inconsistent exit
status and any outer timeout are also unchecked at the caller; never accept a partial reply.

Findings contain only rule IDs, final file/line/column and fixed corrective actions;
Ruff messages, source snippets, fix edits, raw stdout/stderr, TOML parser details and
subprocess exception text are never emitted or retained in reports. Syntax failures
use `invalid-syntax`. Raw outputs exist only in bounded process memory.

Bounds: 64 KiB request, 10 file entries before deduplication, 128 KiB per file, 1 MiB aggregate source;
256 KiB per configuration input and 1 MiB aggregate configuration; 512 KiB stdout and
stderr per tool; 200 final findings; 256 KiB JSON result. Over-limit requests/results
are unchecked with no silent truncation. The source, tool and result bounds are
checked before publishing writes. The provider does not cache clean file verdicts:
configuration, dependency, suppression and policy changes cannot reuse stale success.

## Handoff and acceptance evidence

The provider candidate targets a backward-compatible MINOR release from live v2.5.0
to v2.6.0. The provider source manifest remains on published v2.5.0. Owner merge and
the existing Windows/Linux release verification/publication must finish before host
installation. Then the host must record the engine wheel/release identities, explicitly
select its baseline, run `edit-setup` for both baseline and managed pins, validate its
result reader and complete real Codex-event and latency acceptance. No consumer pin
migration is required for the initial v2.4.2 consumer. Other pins require the matrix
check above; this PR does not activate host settings or change consumer pins.

Provider test commands, real release comparison, cleanup, timing evidence and limitations
are recorded in [the provider acceptance record](audits/edit-check-provider.md).
