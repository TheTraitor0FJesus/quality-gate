# Verification evidence protocol (schema 1)

This document defines the provider-owned local evidence contract shared by
`quality-gate check` and `quality-gate edit-check`. A cache hit is an optimization
for a completed provider check. It never grants a caller authority to declare a
result passed.

## Record

The provider stores one canonical JSON record per identity digest in its
`verification-evidence-v1` cache directory. Schema 1 records have this
shape:

```json
{
  "schema": "quality-gate.verification-evidence",
  "version": 1,
  "identity": {"...": "complete canonical check identity"},
  "identity_sha256": "<SHA-256 of canonical identity JSON>",
  "complete": true,
  "producer": {
    "operation": "quality-gate.check",
    "engine": {
      "form": "source",
      "version": "<version>",
      "digest": "<digest of the executing provider engine>"
    },
    "started_at": "<UTC timestamp>",
    "completed_at": "<UTC timestamp>",
    "duration_ms": 0
  },
  "scope": {"...": "exact provider-verified scope"},
  "outcome": {
    "status": "passed",
    "findings": []
  }
}
```

`outcome.status` is only `passed` or `failed`. Findings contain bounded
structured rule identifiers, repository-relative paths, line/column numbers and
the format outcome. They contain no source excerpts, raw tool output, credentials
or secret reports. A result that cannot fit the record bound is returned from the
current invocation and is not persisted. `complete` is written only after the
provider has parsed the complete tool output and confirmed the exact scope.

The internal provider API is `lookup(identity, expected_engine)` and
`publish(identity, producer, scope, outcome)`. `lookup` validates the complete
schema, canonical identity digest, producer engine, exact scope, completion
marker, outcome shape and HMAC before returning an outcome. Any missing, invalid,
partial, incompatible, oversized, stale or unauthenticated record is a miss.
`publish` is called only after a provider-owned check has completed and its result
has been parsed; host-supplied verdicts are not accepted as evidence. Cross-command
reuse is allowed when the full check identity and engine identity match, regardless
of which provider command produced the record.

Schema 1 uses a random 32-byte provider-owned HMAC key, bounded reads, atomic
replacement and the existing nonblocking cross-process lock. Content-addressed
record names avoid direct-mapped collisions. The store retains at most 128 records
of at most 320 KiB each, evicting the oldest valid inactive record before a
publication when full. Lock contention, unavailable storage or unsafe paths skip
reuse and leave ordinary checking authoritative.

The former `edit-results-v1` batch records are never promoted into schema 1. They
are left untouched and ignored. A caller may use an exact whole-request result only
when it is stored in the new envelope and its identity includes that complete
request; it cannot treat one batch record as evidence for individual files.

## Identity and readiness

The canonical identity binds every input that can change the check's meaning:

- record kind and exact check family;
- a hash of the repository's Git common directory, or of its canonical root when
  it is not a Git repository;
- exact root-relative file path, content SHA-256 and normalized file mode for each
  file in scope. Edit batch identities also bind component membership. Gate
  validates staged candidate identity separately before reusing file evidence;
- executing provider engine form (`source` or `wheel`), version and digest of its
  actual code. The form is part of the digest input, so equal version text cannot
  authorize reuse between a mutable source checkout and a released wheel. If the
  engine form or bytes cannot be established, the invocation does not reuse or
  publish evidence;
- exact tool name, version and verified executable digest; exact policy release,
  policy digest and normalized effective options;
- runtime version, executable digest, runtime fingerprint and platform whenever
  they affect execution;
- current configuration discovery, including hashes and absence facts for
  applicable project configuration and ignore/exclusion files; and
- all check-specific local dependencies and the exact eligible file scope found
  during this invocation.

The provider rechecks file bytes and modes, configuration discovery, policy/tool
integrity, engine integrity and runtime readiness before accepting evidence. It
performs current discovery before lookup, including Ruff's current `--show-files`
eligibility decision. Additions, deletions, renames, mode changes, exclusions and
configuration changes therefore update the eligible scope or the identity before
reuse. Raw working-tree bytes and Git-normalized staged bytes have different file
hashes whenever their content differs.

Gate accepts local file evidence only when the staged Git blob, current raw
working-tree bytes, and candidate bytes agree. It also verifies the current index
mode and a matching ordinary working-tree mode; unusual or unprovable modes fall
back to execution. The snapshot's read-only filesystem permissions are not the
original source mode. The index and working-tree facts are checked again before
Gate accepts a result. A prepared native Ruff executable is used only when its
bytes match the executable identity in the pinned wheel; otherwise Gate runs its
normal tool without local file evidence.

Stored outcomes do not contain invocation eligibility, readiness or waiver state.
Every consumer reevaluates those decisions using its current manifest, candidate,
configuration and waiver rules. A cache hit cannot make an excluded or ineligible
file applicable, cannot turn a newly valid waiver into a stored pass, and cannot
turn a stale runtime into a ready one.

## Ruff and format eligibility

For the released Ruff policy, file-local lint evidence excludes `I` rules. Ruff's
`I` import classification depends on current source-root/module layout, so those
rules are checked fresh over the complete current component or explicit edit scope
on every invocation. The local lint and format outcome for each eligible file can
be reused separately. The local lint identity binds the remaining exact released
rule set, per-file ignores, inline suppression semantics, policy digest and
effective options. Each file-local record contains distinct lint and format
outcomes for the same currently eligible file.

The provider must review the exact selected rule set before enabling reuse for a
new policy release. In this policy, `I` is the import-classification dependency;
the selected file-local rules inspect the current file, while `RUF101` through
`RUF104` suppression behavior is bound by the current file bytes and policy/config
identity. If a selected rule, format-specific exclusion or option introduces an
unmodeled package, cross-file or discovery dependency, the provider runs the full
current scope and does not publish a local record for that check.

The reviewed selection is `E`, `F`, `I`, `UP`, `N`, `ANN`, `T20`, `E722`,
`C901`, `PLR0915`, `PLR2004`, `ERA`, `B`, `RUF006`, `RUF008`, `RUF009`,
`RUF012`, `RUF016`, `RUF017`, `RUF018`, `RUF024`, `RUF032`, `RUF040`,
`RUF043`, `RUF048`, `RUF049`, `RUF053`, `RUF059`, `RUF060`, `RUF063`,
`RUF064`, `RUF068`, `RUF101`–`RUF104`, `RUF200`, `S101`, `S102`, `S105`–`S108`,
`S110`–`S113`, `S201`, `S202`, `S301`–`S306`, `S308`, `S312`–`S319`,
`S321`, `S323`, `S324`, `S501`–`S509`, `S601`, `S602`, `S604`, `S605`,
`S608`–`S612`, `S701`, `S702` and `S704`. The checked policy also specifies
`W191` as ignored and has no format-only exclusion. Discovery and exclusion
decisions are repeated for the full current scope; `I` is rechecked across that
scope instead of reusing its outcomes.
File-local reuse is enabled only for this reviewed policy's exact LF or CRLF
bytes with Ruff 0.16.4. Other policy bytes or tool versions execute the complete
current scope without publishing file-local evidence until separately reviewed.

Current Ruff discovery and check outcomes remain separate. The provider looks up
evidence only for files currently eligible for that check. It executes all cache
misses in one bounded invocation where safe, checks import-sensitive rules over
the current full scope, combines cached and fresh findings, then preserves the
complete aggregate pass/fail verdict. Failures in unchanged files remain failures.

## Trust boundary and CI

HMAC authentication detects corruption and unsupported record writes under the
existing user-local ownership model. It does not authenticate arbitrary data from
host JSON or protect against an actor who controls the local provider key. No
caller-supplied `passed` flag is trusted. GitHub CI does not read or write this
local store and remains authoritative through its pinned released Quality Gate.

Required positive and negative fixtures cover exact identity matching, schema
migration, partial and forged envelopes, producer mismatch, stale files and
configuration, source-versus-wheel engines, concurrent publication, corruption and
capacity. The public correction-loop fixtures verify ten files, nine unchanged
hits, a fresh check for repaired file two, and a failure preserved in a different
unchanged file.
