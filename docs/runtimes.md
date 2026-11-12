# Runtime inventory command

`quality-gate runtimes --json` reports existing Python environments declared by a repository's
`quality-gate.toml`. Pass `--root <repository>` outside the repository and `--cache-dir <path>` only
when inspecting a non-default Quality Gate cache:

```text
quality-gate --root <repository> runtimes --json
```

The command is read-only. It validates the manifest, verifies the exact selected policy release,
and inspects each component's exact runtime identity using the same fingerprint and paths as
Quality Gate setup. It checks that the recorded interpreter file is present but does not execute it;
`available` means the runtime metadata matches the manifest and the executable path exists, not that
the interpreter has been started or its binary integrity has been verified. It does not select or
quarantine policy entries, create cache directories, prepare environments, install dependencies,
run tests, or create a temporary workspace.

## Schema 1

The command writes exactly one JSON object to stdout. Diagnostics for an unavailable result go to
stderr. Components remain in manifest order, including ready components when another component or
the selected policy is unavailable.

```json
{
  "schema": 1,
  "engine_version": "4.8.0",
  "root": "/absolute/repository/path",
  "policy": {
    "release": "v4.7.0",
    "available": true,
    "reason_code": null,
    "reason": null
  },
  "components": [
    {
      "name": "service",
      "path": "src/service",
      "python_version": "3.12",
      "available": true,
      "python_executable": "/absolute/cache/path/python",
      "reason_code": null,
      "reason": null
    }
  ],
  "error": null
}
```

For unavailable components, `python_executable` is `null`, and `reason_code` and `reason` are
non-empty strings. Defined component reason codes are `runtime_missing`, `runtime_stale`,
`runtime_state_corrupt`, `runtime_state_unreadable`, and `runtime_inputs_unavailable`. Policy reason
codes are `invalid_release`, `release_unavailable`, and `release_mismatch`. A repository-level
error uses an empty `components` array, `policy: null`, and an `error` object containing
`reason_code` and `reason`; current codes are `root_not_found`, `repository_not_found`,
`manifest_invalid`, and `inspection_failed`.
If the command cannot resolve the current directory, `root` is `null`. It cannot report an absolute
path without querying that directory again.

Exit code `0` means the selected policy is verified and every declared Python runtime is ready.
Exit code `2` means at least one prerequisite is unavailable or the repository could not be
inspected. Partial inventories still use exit code `2` and include every component that could be
read. Argparse uses its standard exit code `2` for invalid command-line arguments; such a parsing
failure does not emit a result object. `--help` is handled by argparse and has no side effects.

Consumers must require a Quality Gate engine release that supports `runtimes --json` with schema
1. `engine_version` identifies that engine, independently of the manifest's policy release; do not
infer support from the policy version alone.
