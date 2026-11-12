# Check types in working-tree changes with `edit-types`

`quality-gate edit-types` runs mypy for affected Python components using the selected policy. It
reads one UTF-8 JSON object from stdin through EOF and writes one JSON result. It does not modify
project files or import project code while checking prerequisites.

## Request

Required fields are `schema`, an absolute `root`, `files`, and an integer `deadline_unix_ms`. Each
file has a root-relative `path` and `sha256` of its current bytes. Use `sha256: null` to assert that
a file is absent, such as a deleted file.

```json
{
  "schema": 1,
  "root": "/absolute/project",
  "files": [
    {"path": "app/api.py", "sha256": "<64 lowercase hex digits>"},
    {"path": "app/removed.py", "sha256": null}
  ],
  "deadline_unix_ms": "<current Unix time in milliseconds plus the budget>"
}
```

Replace the deadline placeholder with an integer. `baseline` with the single field `policy_release`
is required only for unmanaged scope. Optional `cache_dir` must be an absolute path outside the
project. An invalid manifest never falls back to the baseline.

Schema 1 allows up to 10 files and a deadline of at most 10 seconds. Background requests use
`schema: 2`, `mode: "background"`, a canonical UUID `event_id`, and an absolute `cancel_path` in the
external cache at `edit-types-events/<event_id>.cancel`; their deadline can be up to 30 seconds.
The host creates that file to cancel the run. The response echoes `event_id` so the host can match
the completion to its request.

## Result

The result includes `status`, `reason`, `action`, `root`, `root_identity`, `files`, `configuration`,
`selections`, `components`, `freshness_sha256`, `process_count`, and `elapsed_ms`. Background
responses also include `schema: 2`, `mode`, and `event_id`. `writes_may_have_occurred` is always
`false`.

Each component record reports its analyzed scope, selected environment, status, and diagnostics. A
diagnostic contains only the path, line, column, and mypy code; messages and source fragments are
not returned. Before showing a result, the host should confirm that its request, files, and
configuration are still current and that `freshness_sha256` matches; rerun the check if inputs
changed.

| Status | Exit code | Meaning |
| --- | ---: | --- |
| `passed`, `not_applicable` | 0 | Checks passed, or there was no affected scope |
| `failed` | 1 | Mypy reported diagnostics |
| `unchecked` | 2 | The check or its completeness could not be confirmed |

An empty file list returns `not_applicable`. Invalid requests, stale hashes, unsupported policies,
or unavailable environments return `unchecked`, never a pass.

## Limits

The shared request is limited to 64 KiB and 10 files; the result is limited to 256 KiB. The command
also bounds source scanning and environment evidence. Exceeding a limit returns `unchecked` without
partial coverage. The response's `action` gives the recovery step for an `unchecked` result.
