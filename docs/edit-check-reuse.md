# Edit-check reuse and acceptance

## Unchanged-content reuse

Starting with engine v2.6.2, send the same schema 1 request with freshly captured bytes
and a fresh deadline after each edit. The provider alone decides reuse; host verdicts,
tokens and private release/runtime selection rules are not part of this contract.
Earlier schema 1 engines always run Ruff and expose no complete identity-only probe,
so their response alone is insufficient for safe host-owned reuse.

Every invocation freshly resolves the exact selected release and validates its full
inventory, native Ruff bytes, interpreter bytes/version and prepared runtime metadata.
The reuse identity includes the phase, so legacy, write and validate results never share a cache entry.
It also binds engine version, canonical root, ordered complete file batch
and final hashes, managed/baseline coverage, selection identities, configuration,
waiver/dependency inputs and artifact paths/digests. It also observes filesystem facts
under the explicit root and `src` for bounded source identifier/dotted-name candidates:
adding/removing a local module can change Ruff's first-party import classification.
The provider also captures the presence of parent `__init__.py` and `__init__.pyi`
markers for explicit input files. Empty markers in the isolated tree preserve Ruff's
package classification without reading unrelated initializer contents. Marker changes
invalidate reuse, and unsafe marker paths remain unchecked.
Unreadable, unlexable or over-bound import context disables reuse and runs ordinary Ruff.
No imported source contents or repository-wide discovery are needed.

Only complete `passed` or `failed` batches are remembered after publication freshness
checks. Incomplete worker results and wholly inapplicable batches are not saved.
A hit retains residual findings, final coordinates/hashes, selections and inputs;
`input_sha256` is the current request hash, `changed=false`, and
`writes_may_have_occurred=false`. Process count and elapsed time describe the current
invocation. Existing source/configuration/artifact freshness checks still run before
returning. Failed delivery or an outer timeout remains unchecked at the caller even
when the worker completed; receiving a complete current response remains mandatory.

The optional private cache is `edit-results-v1` under the normal user-local Gate cache,
independent of request `cache_dir`. It retains at most 16 direct-mapped JSON records of
256 KiB plus a 512-byte envelope each, a random 32-byte HMAC key and a nonblocking lock.
Records contain schema 1 diagnostics/identities, never source/configuration contents.
The provider authenticates records with HMAC-SHA256; the key and records are written
atomically with private permissions where supported. This detects corrupted/forged
records without treating another process under the same OS account as a security boundary.
Missing, corrupt, oversized, locked or unavailable storage runs ordinary Ruff. Slot
collisions only lose reuse. An abandoned lock can disable reuse until this disposable
directory is removed while checks are idle; required release/runtime caches remain separate.

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
