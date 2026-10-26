# Local Gate queue

The local full Gate uses two machine-wide slots shared by repositories for the current user. A
run acquires one slot before building its candidate snapshot and keeps it through `check` or
`audit`. Other runs display a waiting message and continue automatically when a slot opens. The
operating system releases a slot when its process exits, including after a crash.

The queue applies to `check`, `audit`, and bootstrap check or audit runs in a queue-aware Quality
Gate version. It does not apply to `format`, setup, validation, doctor, or the working-tree edit
commands. GitHub Actions skips this local wait so its existing job deadline remains in control.

Queue time has a separate budget from Gate execution. The default is 120 seconds. Set
`QUALITY_GATE_QUEUE_TIMEOUT_SECONDS` to a whole number from 0 through 240 to change it. Zero makes
the command try an available slot once and stop immediately if both slots are occupied. If the
budget expires, Quality Gate reports that no check ran. It does not build a snapshot or return a
pass.

Ruff lint and format checks inside a full Gate use four Rayon worker threads by default. Set
`QUALITY_GATE_RUFF_THREADS` to a whole number from 1 through 64 to choose another limit. Quality
Gate validates this setting and passes it to Ruff as `RAYON_NUM_THREADS`. Other child processes do
not receive the Rayon setting. The standalone `quality-gate format` command retains its previous
environment behavior.

For example, PowerShell can set both values for the current session:

```powershell
$env:QUALITY_GATE_QUEUE_TIMEOUT_SECONDS = "180"
$env:QUALITY_GATE_RUFF_THREADS = "8"
quality-gate check
```

Shell variables also work on Unix:

```sh
QUALITY_GATE_QUEUE_TIMEOUT_SECONDS=180 QUALITY_GATE_RUFF_THREADS=8 quality-gate check
```

Consumers select immutable Quality Gate releases. A consumer pinned to an older release does not
join this queue until it activates a published release that contains the admission code. Its
stable native wrapper must allow the configured queue wait in addition to the existing Gate
execution timeout. See the [agent reference](agent-reference.md) for release activation.

## Ownership and isolation

The slot file stores a 128-byte lease record and locks the byte immediately after that record.
Unbuffered reads keep the record readable through a second handle on Windows without reading
ahead into the locked byte. Descendants receive only a
canonical admission root, slot, and token from an active lease. They must confirm a live slot
lock and the matching token under the control lock before reusing it. An explicit provider cache
root selects an isolated namespace.

Release waits at most 0.5 seconds for the control lock, then closes its OS slot handle even if
the control lock remains busy. The next owner replaces stale metadata. Safe child environments
retain their isolated cache roots and omit ambient CI flags.
CI descendants receive an internal admission-only marker so nested Gate runs also skip the local
queue while the ordinary child environment stays isolated from ambient CI settings.

## Native-wrapper handoff

The installed stable bridge resolves the staged policy wheel and verifies its digest before
launching `python -m quality_gate.cli` with that wheel on `PYTHONPATH`. It does not call
`quality_gate.hooks.run_commit_gate`. Updating that helper alone does not update the installed
bridge.

Before activating a queue-aware release, adapt the bridge's `run_released_command` to derive its
outer timeout from the same staged manifest bytes used to select the wheel. Validate schema 2,
read `repository.defaults.gate_timeout_seconds`, and add the validated local queue wait outside
CI. Preserve the existing UTF-8 transport, wheel verification, return codes, and process ownership.
The bridge must not acquire another admission slot or import mutable checkout source.

`quality_gate.hooks.commit_gate_timeout_seconds` provides the staged-manifest Gate-plus-queue
calculation for release-backed callers. A standalone bridge can perform the same small calculation
with the standard library. The isolated adapter in `tests/test_native_gate_queue.py` exercises
that proposed change through the actual native bridge and a verified temporary candidate wheel.
It covers waiting, automatic continuation into successful repository checks, and queue exhaustion.
The published wheel is also launched independently. These tests do not install the adapter or
replace a cached release.

The enclosing launcher must retain time for preparation and cleanup beyond the Gate and queue
allowances. This provider's 2400-second Gate budget requires an explicitly enlarged enclosing
verification budget. With a 3000-second envelope, the default 120-second queue wait leaves 480
seconds for preparation and cleanup; the maximum 240-second wait leaves 360 seconds. The normal
1500-second typed-runner envelope is too short for this provider budget and must be overridden
explicitly before dispatch. These are budget bounds, not measured run durations.
