# Edit-check reuse and handoff evidence

## Unchanged-content reuse

The provider uses the shared [verification evidence protocol](verification-evidence.md)
for both complete edit responses and file-local Ruff/format results. Send the same
schema 1 request with freshly captured bytes and a fresh deadline after each edit.
The provider alone decides eligibility and reuse; host verdicts, tokens and private
release/runtime selection rules are not evidence.

Every invocation freshly resolves the exact selected release and validates its full
inventory, native Ruff bytes, interpreter bytes/version and prepared runtime metadata.
An exact whole-request edit hit still binds its phase and complete ordered batch.
File-local lint and format evidence binds each current file separately, including its
path, mode, bytes, coverage/component, engine form/digest, policy/tool/runtime identity,
effective options and discovered configuration. Ruff import-classification rules are
checked fresh over the complete current edit scope; adding or removing a module cannot
authorize reuse of those rules. The provider captures parent `__init__.py` and `__init__.pyi`
markers for selected files and recreates empty markers in the isolated workspace. Marker
changes invalidate reuse; unsafe paths remain unchecked. Unreadable, unlexable or over-bound import context
disables local reuse and runs ordinary Ruff.

Only complete `passed` or `failed` results are remembered after publication freshness
checks. Incomplete worker results and wholly inapplicable files are not saved. A hit
retains structured findings and exact scope;
`input_sha256` is the current request hash, `changed=false`, and
`writes_may_have_occurred=false`. Process count and elapsed time describe the current
invocation. Existing source/configuration/artifact freshness checks still run before
returning. Failed delivery or an outer timeout remains unchecked at the caller even
when the worker completed; receiving a complete current response remains mandatory.

The provider-owned store is independent of request `cache_dir`. The provider authenticates
bounded records with HMAC-SHA256 and atomic replacement. It retains a bounded number of
content-addressed records, so distinct files do not evict one another through direct-slot
collisions. Old `edit-results-v1` records are ignored and never promoted. Missing, corrupt,
oversized, locked or unavailable storage runs ordinary Ruff. The cache is optional and does
not change required release/runtime caches.

Host integration keeps its exact policy pin and normal result validation, including
current `input_sha256`, actual final bytes, exact root/engine, and configuration identities.
Session/event attribution and deduplicating delivered messages remain host responsibilities.

## Handoff and acceptance evidence

The reuse feature shipped as PATCH v2.6.2. The separate secret interface shipped in
v3.0.0. Published engine v4.1.0 retains the exact policy compatibility matrix above and
adds explicit writer and read-only validation phases while retaining the schema 1 request.
The v4.0.0 engine and older releases reject the new `phase` field. The v4.1.0 release is
published; host installation and acceptance remain separate. The host must record the
engine wheel and release identities, retain and provision `edit-setup` for its baseline
and managed pins, validate its result reader, and complete real event and latency
acceptance. No policy pin migration is required, but the host must select the v4.1.0
engine before sending either phase. Provider publication does not activate host settings
or change consumer pins.

Follow the [interface verification requirements](release.md#working-tree-interface-verification)
for real-wheel parity, host events, latency and cleanup.
