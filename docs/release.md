# Quality Gate release procedure

This document defines the release boundary for the public Quality Gate repository. The release
controller is `python -m quality_gate.release`; it is separate from the consumer `sync` command.

## Preconditions

The source tree must satisfy all of these conditions:

- `quality-gate.toml` uses schema 2 and its `quality.policy_release` matches the package version
  in `pyproject.toml`.
- Every required document is present and non-empty.
- `quality-gate audit` passes, including full reachable-history secret scanning and lesson
  completion.
- The complete supported-platform and failure-injection test suite passes on the release source.
- The artifact contains a policy wheel, every declared policy or dependency file, and every
  declared external tool with its SHA-256 digest in `release.toml`.

The source must not contain a real credential. Rotate or revoke a real credential before release;
do not hide it with a baseline or waiver.

## Candidate validation

Build the platform-specific release directory or archive with the exact wheel and tool inventory
that will be published. The controller accepts either form. Run it after the audit and tests:

```text
python -m quality_gate.release --root . --artifact <release-directory-or-zip> --version vMAJOR.MINOR.PATCH
```

The controller checks the schema 2 manifest, package version, required documents, and release
lessons. It then installs the candidate into a temporary policy cache through the same locked,
integrity-checked distribution path used by consumers. The exact wheel is installed into one
disposable workspace, and the installed wheel runs both `setup` and `audit` from its isolated
environment. The workspace never uses the user's active policy cache. Windows chooses a writable
workspace with a verified final path and reserved path-length headroom; Linux uses the normal
system temporary directory. If automatic Windows selection is unavailable, pass an existing short
directory explicitly as `--workspace-parent D:\qgtmp`. An unsuitable parent is an unchecked
release condition. The controller rejects a missing, malformed, mismatched, corrupt, or
non-executable artifact and leaves the user cache unchanged. A successful result is:

```text
release: ready - vMAJOR.MINOR.PATCH
```

The artifact is not ready for publication when the controller returns `unchecked`.

## Human publication boundary

A human reviews the controller output, the exact archive names and digests, and the release notes.
Publish the already-validated artifacts to one immutable GitHub Release. Do not replace an asset
under an existing version. A release version change requires a new semantic version, a new source
manifest identity, a rebuilt artifact, and a new controller run.

The first v2 release is `v2.0.1`. Its Linux and Windows assets are immutable and are selected by
the reusable workflow from `quality.policy_release`. Consumer workflows must pin the reusable
workflow to a full commit SHA and must not use a mutable branch reference.

## Public branch protection

The public repository's `main` branch requires an active GitHub ruleset with these observable
controls:

- pull requests are required;
- at least one human approval is required;
- the unique required status is `Quality Gate`;
- force-push and branch deletion are disabled.

The ruleset is GitHub repository configuration, not a consumer manifest setting. Verify it after
any repository administration change. Private GitHub Free consumers retain the documented
pre-push and CI detection limitation.

## Rollback and retention

The distribution cache keeps the active release and preceding releases. Select the preceding
release without network access:

```text
quality-gate sync --rollback
quality-gate doctor
quality-gate check
```

Use an explicit version only when the retained release is known to be valid. Preview old entries
with `quality-gate sync --prune`; confirmed pruning is a separate maintenance action and never
runs as part of a commit.
