# Working-tree integrity interface (schema 1)

Paths are relative to the Quality Gate repository root. This contract supports
the `python-quality-hooks` host ticket 03. The host owns event attribution,
post-Ruff hashes, activation and model feedback. The provider owns exact policy
selection, Gate marker semantics, typed waiver resolution and response freshness.
The additive provider candidate is v3.2.0; immutable publication and host
activation remain separate owner-controlled steps.

## Invocation

The command is `quality-gate edit-integrity`. Send one UTF-8 JSON object on
stdin and close stdin. The native verified-release launcher owns installed
activation. Host development tests may load this candidate source through an
explicit source bridge; bind source/commit hashes in that evidence and never
replace an immutable cached wheel with candidate files.

```json
{
  "schema": 1,
  "root": "/absolute/project",
  "files": [{"path": "src/example.py", "sha256": "<64 lowercase hex digits>"}],
  "deadline_unix_ms": 1792400000000,
  "cache_dir": "/absolute/quality-cache",
  "baseline": {"policy_release": "v2.4.2"}
}
```

The caller replaces the illustrative timestamp immediately before launch with
its shared deadline, at most ten seconds ahead. Startup, file verification,
policy selection and response delivery share it. The supervisor owns worker
cleanup within the existing bounded transport grace. Missing, partial or late
output is unchecked. `cache_dir` is optional and uses the ordinary immutable
Gate cache by default. `baseline` is required only if `quality-gate.toml` is
absent. A valid manifest selects its exact `quality.policy_release` and waivers;
invalid manifests and pins never fall back to baseline. No Python runtime or
scanner is selected for integrity checking. Unknown keys, duplicate keys and
unsupported schemas are unchecked.

Paths identify complete regular files explicitly supplied by the host. They
are not globs or tree queries. Raw-byte hashes include BOM and line endings.
Identical duplicate entries coalesce; conflicting hashes are unchecked.
Missing/deleted files, hard links, links, reparse points, escapes, unreadable
files and partial reads are unchecked. Renames use the destination path, so a
standard waiver must match that path. The provider never enumerates the Git
tree or starts Git.

## Gate semantics

The implementation calls `git_conflict_marker_results`, which shares the normal
repository Gate's marker expression and exact `repository.git.conflict_markers`
standard waiver resolver. It returns aggregate and per-file status from one scan.
It binds waivers to the normalized path and applies the existing standard-waiver
rules. It adds no wildcard or content-fingerprint waiver, and omits waived matches
from findings.

The existing Gate treats NUL-bearing bytes and invalid UTF-8 as non-text and
skips marker detection for that file. This route preserves that behavior.
Unreadable or changed files remain unchecked at the edit boundary, so a
partially read batch cannot pass. Empty files pass marker detection. The route
checks only the explicit files; it does not broaden full Gate or history scope.

## Result

The command emits one bounded, source-free JSON object. A completed result has
`schema`, `engine_version`, resolved `root`, `check_id`, `status`, `reason`,
top-level `findings`, per-file `files`, exact `selections`, captured `inputs`,
`process_count` and `writes_may_have_occurred: false`. A finding contains only
the fixed check ID, path, positive line, failed status and fixed action. No
source text, exception, policy diagnostic or waiver reason is returned.

Each requested file has `input_sha256` and freshly verified `sha256` equal to
the request digest, `changed: false`, a selection index, managed/baseline
coverage and its unwaived findings. File status is failed when an unwaived
marker exists, waived when all its markers have an exact active waiver, and
passed when it has no marker. Top-level status is failed if any unwaived marker
exists, waived if markers exist and all are waived, and passed otherwise. An
empty request is `not_applicable` with no selected release. `inputs` contains
the raw `quality-gate.toml` digest, or null when the file is absent. The
selection binds the exact immutable policy `release.toml` and complete typed
waiver-set digest.

| Status | Exit |
| --- | ---: |
| passed, waived, not_applicable | 0 |
| failed | 1 |
| unchecked | 2 |

An unchecked result discards partial coverage and exposes a fixed reason and
action only. The host must validate engine identity, schema, exit/status
agreement, exact path coverage, input hashes, policy selection, root and the
shared deadline before accepting a result. It must also refresh the touched
file hashes before model feedback.

## Bounds and compatibility

The request reuses the shared edit protocol: 64 KiB request, 10 files,
128 KiB/file, 1 MiB total source, bounded manifest capture and 256 KiB result.
The public edit deadline remains ten seconds. There is no verdict cache,
materialization tree, scanner process, dependency setup, download, runtime
probe, full-tree scan, history scan or source-file write. The worker rechecks
the manifest, selected immutable release and all raw file bytes before
publishing.

The v3.2.0 engine retains previously supported policy pins, including published
v3.1.0, and adds its own v3.2.0 release after publication. This capability does not change the selected
policy contents or Ruff/secret/security checks. It does not install Codex
hooks, enable a host setting, replace baseline policy or update consumers.
Actual event delivery, rollback and aggregate latency remain host acceptance
work after owner merge and immutable release publication.
