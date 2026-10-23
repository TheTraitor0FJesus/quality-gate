# Working-tree secret interface (schema 1)

Paths are relative to the Quality Gate repository root. This is the provider
contract for `skills/.scratch/python-quality-hooks/issues/02-secret-hook.md`.
The host owns attribution, Ruff-before-readers ordering, feedback and activation.
The provider owns scanner selection, exact content verification and typed waivers.
The interface was introduced in published v3.0.0; the v3.2.0 candidate adds
`edit-integrity` while retaining this command. Host activation remains a separate
consumer step. The published v2.6.2 engine has no `edit-secrets`.

## Invocation

The candidate command is `quality-gate edit-secrets`. Send one UTF-8 JSON object
on stdin and close stdin. The module route is `python -I -B -m quality_gate.cli
edit-secrets` when the candidate package is explicitly installed for development.
For host tests without installation, use this argument vector with a preinstalled
Python and the separately resolved absolute **candidate repository root**:

```python
command = [
    python, "-I", "-B", "-c",
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from quality_gate.cli import main; raise SystemExit(main())",
    candidate_root, "edit-secrets",
]
```

This is a development-only seam. Bind its source hashes in test evidence. The
native verified-wheel launcher continues to own actual release activation.
Candidate source must never replace an immutable cached wheel or impersonate a
published engine. The v3.0.0 engine introduced this command; source/commit hashes
identify the v3.2.0 candidate until its immutable publication completes.

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

The illustrative timestamp must be replaced immediately before launch with the
caller's shared deadline, at most 10 seconds ahead. Startup, verification, scan
and response delivery share that deadline. The caller also supervises the entry
process and allows at most three additional seconds for owned-tree cleanup.
There is no per-hook budget reset. Missing, partial or late output is unchecked.

`cache_dir` is optional and defaults to the ordinary Gate cache. `baseline` is
required only when `quality-gate.toml` is absent, including outside Git. With a
valid manifest, every requested path uses its exact policy release and waivers,
independently of Python component membership. An invalid manifest or managed pin
never falls back to baseline. No interpreter/runtime belongs in this request.
Unknown keys, duplicate object keys and unsupported schemas are unchecked.

Paths identify complete regular root-relative working-tree files. They are not
globs. Hashes cover raw bytes including BOM and line endings. Identical duplicate
entries coalesce; conflicting hashes are unchecked. Missing/deleted paths are
unchecked; the host omits deletions from live inputs and attributes them itself.
Renames use the destination path and therefore its new `path:line` waiver target.
Links, hardlinks, reparse points, escapes and unreadable inputs are unchecked.

## Result

The command emits exactly one source-free JSON object; stdout/stderr from the
scanner and exception messages are never forwarded. A completed example is:

```json
{
  "schema": 1,
  "engine_version": "3.0.0",
  "root": "/absolute/project",
  "check_id": "secrets.candidate",
  "status": "failed",
  "reason": null,
  "files": [{
    "path": "src/example.py",
    "status": "failed",
    "coverage": "managed",
    "input_sha256": "<request digest>",
    "sha256": "<same freshly verified digest>",
    "changed": false,
    "selection": 0,
    "findings": [{
      "check_id": "secrets.candidate",
      "path": "src/example.py",
      "target": "src/example.py:3",
      "line": 3,
      "fingerprint": "<SHA256 of actual matched secret as UTF-8>",
      "status": "failed",
      "action": "Rotate or revoke the credential and remove it from the touched file."
    }]
  }],
  "selections": [{
    "policy_release": "v2.4.2",
    "release_sha256": "<release.toml digest>",
    "policy_source": "gitleaks-built-in",
    "policy_sha256": "<scanner digest binding its embedded policy>",
    "scanner_version": "8.30.1",
    "scanner_sha256": "<verified native executable digest>",
    "waiver_sha256": "<canonical complete typed-waiver-set digest>"
  }],
  "inputs": [
    {"path": ".gitleaks.toml", "sha256": null},
    {"path": ".gitleaksignore", "sha256": null},
    {"path": "quality-gate.toml", "sha256": "<working-tree manifest digest>"}
  ],
  "process_count": 3,
  "elapsed_ms": 450,
  "writes_may_have_occurred": false
}
```

`coverage` is `managed` or `baseline`. `selection` indexes `selections`; it is
null only when an empty batch has no selection. Findings include both blocking
and waived matches; `status: "waived"` marks an exact current typed waiver.
Each finding has the fixed check ID, an attributed path, positive line, exact
target, lowercase SHA256 and a fixed action. No source, detector message, raw
match, waiver reason, tool diagnostic or report fragment is exposed.

Per-file status is `failed` if any match is unwaived, `waived` if all matches are
waived, `passed` if the completed scanner reports no matches, and
`not_applicable` for an empty file. Top-level precedence is failed, passed,
waived, not_applicable. Empty requests are not_applicable. Published scanner
allowlists and `gitleaks:allow` retain normal scanner semantics; a completed
allowlisted file has no matches and is passed, rather than claiming detector
coverage beyond that policy. No verdict cache is used.

The native Gitleaks ignore identity differs from a Gate waiver identity. Normal
directory mode receives an absolute materialization root, and Gitleaks 8.30.1
uses an absolute `File:RuleID:line` in `.gitleaksignore` matching. A root-relative
native-ignore entry therefore does not suppress that normal candidate or this
provider. Its bytes are preserved, not rewritten to add suppressions. Gate
typed waivers use the normalized project-relative `path:line` and actual SHA256.

| Status | Exit |
| --- | ---: |
| passed, waived, not_applicable | 0 |
| failed | 1 |
| unchecked | 2 |

An unchecked reply has `schema`, `engine_version`, `check_id`, `status`, fixed
`reason`, fixed `action`, `files: []`, `writes_may_have_occurred: false` and
`elapsed_ms`; `stage` may identify the failed stage. Discard partial coverage.
It may omit root/selections/inputs/counters when failure precedes their capture.
Reasons include invalid_request, deadline, unsafe_path, limits, stale_input,
manifest, unsupported_policy, release, tool, configuration, coverage,
tool_output and io. Readers must validate schema, types, exact path coverage,
hashes, selections, exit/status consistency and deadline before accepting clean.

`inputs` records absence with null; values are raw configuration hashes.
For `.gitleaks.toml`, policy_source is `project` and policy_sha256 is that file's
digest. The scanner digest still binds its built-in defaults and implementation.
waiver_sha256 hashes the complete parsed waiver set with sorted JSON keys and
ISO date strings, including standard waivers, even though only exact secret
waivers apply. The manifest digest independently binds its original bytes.
process_count counts entry, supervised worker and scanner starts; elapsed_ms
starts in the entry function. The host measures outer launch-to-delivery time.

## Materialization and compatibility

The supported exact pins are v2.4.2, v2.5.0, v2.6.0, v2.6.1, v2.6.2 and v3.0.0.
Published v3.1.0 is also supported. The v3.2.0 candidate adds its own pin,
usable only after immutable publication.
The complete immutable release inventory is verified offline without repairing
or quarantining it. Its native Gitleaks 8.30.1 and embedded policy are selected;
there is no dependency setup, download, Python runtime probe, history scan,
pytest or full-tree discovery on the edit path.

The scanner receives a private tree containing only requested bytes at their
original project-relative paths and the bounded root `.gitleaks.toml` and
`.gitleaksignore` inputs. It runs directory mode, matching normal Gate candidate
materialization. Working-tree configuration, requested bytes and selected
artifacts are checked again before publication. No project file is rewritten.
Project configs with external `extend.path` or `extend.url` are explicitly
unsupported/unchecked; inline rules and `extend.useDefault` remain available.
This avoids reads/downloads outside the captured policy boundary.
Every case-insensitive `extend` table is checked before scanner startup,
including configurations containing both `[extend]` and `[EXTEND]`.

UTF-8, ASCII and UTF-8 BOM are supported without decoding or rewriting scanner
input. Invalid UTF-8, NUL-bearing/binary and UTF-16/32 inputs are coverage
unchecked rather than silently accepted. All requested text extensions are
eligible. There is no supplemental detector in this provider. Preserve legacy
host protection separately until its narrow remaining coverage is reviewed.

The exact scanner runs with bounded trace diagnostics in memory. The provider
recognizes only its pinned lifecycle/severity format, requires the completed
byte-count summary, rejects unexpected warnings/errors and reports a native
binary/archive skip as coverage unchecked. This catches NUL-free, UTF-8-valid
application signatures too (for example a PDF header), which Gitleaks would
otherwise skip with a successful exit. Console text is neither forwarded nor
written to disk, including on these failures.

Bounds reuse the edit protocol: 64 KiB request, 10 entries, 128 KiB/file,
1 MiB total source, 256 KiB/configuration, 1 MiB total configuration, 512 KiB
per scanner stream, 200 findings, 256 KiB result. Excess is unchecked, never
truncated into a clean result. Reports exist only in bounded process memory.
The supervisor removes its isolated tree and owns worker/scanner descendants,
including cancellation and children whose immediate parent exits.

## Fingerprint correction and release boundary

The historical normal Gate passes `--redact=100`, which Gitleaks 8.30.1 applies
to JSON `Secret` too. Hashing that field loses the actual credential identity.
The prepared correction computes Gate's existing SHA256 from the unredacted
in-memory report, then exposes only locations/fingerprints. No raw report/log
file is created. This corrects the shared normal candidate/history/audit
transport as well as the additive edit command; detector rules and exact typed
waiver matching remain unchanged.

Waivers issued for the actual secret digest become effective. A historical
waiver for SHA256("REDACTED") does not suppress a different actual secret and
must be reviewed/reissued for its exact target and actual digest. There is no
compatibility alias accepting the broken digest. This persisted-waiver migration
requires MAJOR v3.0.0 relative to v2.6.2. Old immutable engines remain
unchanged; same-release comparisons must identify this historical discrepancy
separately from detector/path parity. Release preparation and consumer rollout
must explicitly carry this correction. Local candidate tests do not authorize
immutable publication or host activation.

The provider acceptance record is [here](audits/edit-secrets-provider.md).
