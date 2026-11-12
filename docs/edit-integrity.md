# Check conflict markers with `edit-integrity`

`quality-gate edit-integrity` checks explicitly supplied files for Git conflict markers. It reads
one UTF-8 JSON object from stdin through EOF and writes one JSON result. It checks only the supplied
files; it does not run Git, scan Git history, or walk the project tree.

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

Replace the deadline placeholder with an integer. Paths are relative to `root` and identify
existing regular files; hashes cover raw bytes. Optional fields are an absolute `cache_dir` and
`baseline: {"policy_release": "<release>"}`. For a non-empty file list, baseline is needed if
`quality-gate.toml` is absent. The manifest selects the exact policy and waivers; an invalid
manifest never falls back to baseline. Missing or deleted files are not checked.

## Result

The result includes `check_id: "repository.git.conflict_markers"`, `status`, `reason`, `findings`,
`files`, `selections`, `inputs`, and `writes_may_have_occurred: false`. A finding reports its path,
line, status, and action; source text is not returned. Standard Gate waivers apply to the exact
path. NUL-containing and invalid UTF-8 files are treated as non-text, matching the full Gate.

| Status | Exit code | Meaning |
| --- | ---: | --- |
| `passed`, `waived`, `not_applicable` | 0 | No markers, all markers are waived, or no files apply |
| `failed` | 1 | An unwaived marker was found |
| `unchecked` | 2 | The complete file list could not be checked |

File hashes and the selected release are rechecked before the result is returned. Stale inputs or
incomplete coverage return `unchecked`, without partial success.

## Limits

The shared edit protocol allows up to 10 files and 1 MiB of source data: requests are limited to
64 KiB, files to 128 KiB each, and results to 256 KiB. The deadline is at most 10 seconds. The
command does not create runtimes, install tools, or modify project files.
