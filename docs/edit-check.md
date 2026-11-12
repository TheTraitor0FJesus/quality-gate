# Check working-tree changes with `edit-check`

`quality-gate edit-check` checks explicitly supplied files with the selected Ruff policy. It reads
one UTF-8 JSON object from stdin through EOF and writes one JSON result. It checks only the supplied
files; it does not replace the full Gate or scan Git history.

## Request

Send an absolute `root`, each file's current byte hash, and the shared deadline:

```json
{
  "schema": 1,
  "root": "/absolute/project",
  "files": [{"path": "src/example.py", "sha256": "<64 lowercase hex digits>"}],
  "deadline_unix_ms": "<current Unix time in milliseconds plus the budget>"
}
```

The deadline value is explanatory; send it as an integer. File paths are explicit paths relative
to `root`, not globs. SHA-256 covers the file's raw bytes. `cache_dir` may be supplied as an
absolute path.

For Python files outside components managed by `quality-gate.toml`, supply `baseline` with
`policy_release`, `python`, and `python_version`. No baseline is needed when all checked Python
files belong to managed components. An invalid manifest or unsupported selected release never
falls back to the baseline.

By default the command applies approved fixes and formatting, then runs diagnostics. A two-phase
integration can use:

- `"phase": "write"` to apply fixes and formatting without final diagnostics;
- `"phase": "validate"` to run final checks without writing files.

The `validate` phase requires an engine version that supports it. After the write phase, pass fresh
file hashes to validate the resulting content.

## Result

The result includes `status`, `reason`, `files`, `selections`, `inputs`, `process_count`, and
`writes_may_have_occurred`; `engine_version` and `root` identify the run. Each file record reports
its path, status, input and final hashes, whether it changed, findings, and the `format` status.
Requests using `write` or `validate` also return `phase`.

| Status | Exit code | Meaning |
| --- | ---: | --- |
| `passed`, `not_applicable` | 0 | Checks passed, or no applicable Python files were supplied |
| `failed` | 1 | Ruff or formatting findings remain |
| `unchecked` | 2 | Complete coverage could not be confirmed; do not treat it as a pass |

Send current hashes immediately before launch. When the result is `unchecked`, refresh the inputs
or resolve its reported cause and retry the full batch.

## Limits

Requests are limited to 64 KiB and 10 files; each file is limited to 128 KiB and the batch to 1 MiB.
The deadline is at most 10 seconds for the default and `write` phases, and 30 seconds for
`validate`. Results are limited to 256 KiB. Exceeding a limit returns `unchecked`, not a partial
pass.
