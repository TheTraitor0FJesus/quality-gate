# Incomplete verification diagnostics

Paths on this page are relative to the repository root. This contract belongs to the
checkout host under `codex_hooks/`, independently of published provider protocols.

## Evidence contract

`_shared/quality_diagnostics.py` owns the fixed reason vocabulary, safe projection and
retained diagnostic records. Producers capture metadata before exception handling can
erase it. `quality_feedback.py` renders this host envelope without changing the verdict.
Missing, cancelled, stale and incomplete verification stays `unchecked` and grants no
write authority.

Each diagnostic identifies its check and known reason. Unknown reason strings become
`unknown`. The diagnostic contains an allowlisted operation, phase, error category and
limit dimension when established. Numeric `errno` and `winerror` must be nonnegative
integers. Limit observations and bounds must be finite nonnegative numbers. Unavailable
values appear as `unavailable` in output, rather than inferred causes.

Known host scope consists of the selected project root, bounded relative input paths
and available session, turn and tool-call identities. A background event ID identifies
the background job separately. It is not the original host tool-call ID. Older jobs
without originating call metadata keep that identity unavailable. Input scope does
not identify the exact failing file.

The record excludes source, command text, exception messages, raw provider output,
scanner messages and unknown fields. A writer failure preserves whether writes may
have occurred and asks for inspection of final bytes. Read-only validation reports
no writes. Missing write evidence remains explicitly unavailable.

Every unchecked report attempts to retain a schema 1 `diagnostic-<sha256>.json` record
in the configured evidence directory, normally the user-local hook reports directory.
The digest identifies the sanitized structured record. Short output includes the digest
and path. Overflow output points to the complete source-free report, including the
diagnostic references. Failed storage leaves the digest visible and the path unavailable.
Existing corrupt or unrelated files are preserved. Reading is bounded; unsafe links,
reparse points, hard links and observed file replacement are rejected. State transitions
reuse the existing bounded lock and atomic JSON publication mechanisms.

This is retained diagnostic evidence, not proof that the host consumed a response.
Retention and delivery lifecycle improvements belong to their separate assignments.

## Failed Pre capture

The shell capture reserves a record for the session and tool call. If snapshot capture
fails, it attempts to retain the original safe reason and metadata in that record.
The paired Post checks the root and available owner identities before consuming it.
A different turn cannot consume the recorded failure. Corrupt, oversized or unsafe
state remains unchecked and is preserved when ownership cannot be established.
Interruption or storage failure can prevent retention; a later missing snapshot alone
does not establish missing event delivery or a broken registration.

Limits distinguish changed-file count, individual file bytes and aggregate changed
bytes, and snapshot entries, aggregate snapshot bytes and time. The authoritative
limits remain in `quality_events.py`. A nested shell working directory can select the
same project root and therefore does not promise a smaller snapshot.

## Recovery

1. Read the reason, operation, phase, selected root and retained record. Treat unavailable
   fields as unknown. Do not expose raw exceptions or source while investigating.
2. For settings, receipt, launch, state or input I/O failures, restore the named boundary
   using the available error category and numeric OS identity. Do not reinstall hooks
   merely because a snapshot is missing.
3. For provider, policy, runtime or tool failures, follow the existing explicit setup or
   reviewed upgrade route in `docs/agent-reference.md`. Hook execution does not install
   prerequisites or change release pins.
4. For a background deadline, inspect background work, collection and cleanup. The
   foreground edit budget does not establish its cause. For snapshot limits, inspect
   the reported dimension and selected project root; an attributable patch can avoid
   a shell snapshot without claiming the earlier edit was verified.
5. Inspect final bytes after a possible writer failure, then retry verification on current
   content. Preserve the incomplete verdict until complete current evidence exists.

The diagnostic regression suite uses event/coordinator, feedback and storage seams.
Its code-coverage assertion checks fixed producer reasons and provider reason sets
against the diagnostic vocabulary. These fixtures prove handler behavior, not real-host
delivery, consumption acknowledgement or a runtime cause for earlier incidents.
