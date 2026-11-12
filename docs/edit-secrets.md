# Check secrets in working-tree changes with `edit-secrets`

`quality-gate edit-secrets` runs the selected Gate scanner on the supplied candidate files. It
reads one UTF-8 JSON object from stdin through EOF and writes one JSON result. It checks only the
explicit file list, not Git history or the rest of the project tree.

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

Replace the deadline placeholder with an integer. Paths are relative to `root`, and hashes cover
raw bytes. Optional fields are an absolute `cache_dir` and
`baseline: {"policy_release": "<release>"}`. For a non-empty file list, baseline is needed only if
`quality-gate.toml` is absent; a valid manifest selects the release and waivers. An invalid
manifest never falls back to baseline.

An empty file list returns `not_applicable`. Otherwise, the scanner and rules come from the exact
immutable release. The check does not download or install dependencies.

## Result

The result includes `status`, `reason`, `files`, `selections`, `inputs`, `process_count`, and
`writes_may_have_occurred: false`. Each file record includes its status, input and final hashes,
`changed: false`, and findings. A finding reports `check_id`, path, waiver target, line, fingerprint,
and a fixed action. Secret values and source text are not returned.

A waiver applies only to the exact `check_id: "secrets.candidate"`, target, and secret fingerprint.
If the scan is incomplete, an input changed, or coverage cannot be confirmed, the result is
`unchecked` and must not be treated as a pass.

| Status | Exit code | Meaning |
| --- | ---: | --- |
| `passed`, `waived`, `not_applicable` | 0 | No findings, all findings are waived, or nothing applies |
| `failed` | 1 | An unwaived secret was found |
| `unchecked` | 2 | Complete scan coverage could not be confirmed |

## Limits

The shared edit protocol allows up to 10 files: requests up to 64 KiB, files up to 128 KiB each,
source data up to 1 MiB total, and results up to 256 KiB. The deadline is at most 10 seconds.
Oversized, unsupported, binary, or stale input returns `unchecked`; results are never truncated
into a pass.
