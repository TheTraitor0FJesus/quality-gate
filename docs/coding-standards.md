# Coding Standards

Canonical standards for code repositories. Copy this file into a repository as
`docs/coding-standards.md` before its code is reviewed. Repository-specific rules may
add explicit deltas; they override this file only where they conflict.

## Scope and design

- Choose the smallest end-to-end change that satisfies the stated requirement.
- Do not add speculative features, abstractions, dependencies, or scope.
- Report unrelated defects; do not repair them as part of the change.
- Prefer a simpler solution when it removes a dependency, a complexity layer, or moving parts.
- Build incrementally: preserve a working path while adding the next capability.
- Before adding code or a dependency, check existing project facilities and maintained libraries, including their documentation and types.
- Clarify an unclear goal. Resolve implementation details autonomously unless they change a public interface, data schema, module boundary, or cross-module contract.

## Code structure

- Give each function and module one focused responsibility.
- Keep control flow shallow; use early returns instead of nesting three or more levels.
- Use precise names. Boolean names use `is_` or `has_`; private module names use `_`; avoid abbreviations and generic placeholders such as `data`, `temp`, `val`, `x`, and `y`.
- Give public functions one-line documentation that states what they do. Comment only a non-obvious constraint, workaround, or decision.
- In typed languages, annotate parameters and return values according to the project's type system.
- Keep user-configurable values in the project's configuration mechanism. Keep secrets only in ignored environment configuration; never hardcode secrets, URLs, timeouts, or environment-specific values in application logic.

## Errors, inputs, and observability

- Use the project's logger, not production `print()` calls. Log startup, shutdown, and external calls at the appropriate level; log or re-raise each caught exception.
- Use exceptions for contract violations and explicit absence/error results for expected outcomes; keep one error-handling style within a module.
- Catch specific exception types. Do not silently suppress failures.
- Validate external input against an allowlist before using it as a key, path component, query element, column, or log interpolation.
- Bound external input at ingestion: ranges for numbers, lengths for strings, sizes for files and bodies, transfer deadlines for network calls, and page/item limits for pagination.
- Isolate failures in batch and fan-out loops so one item cannot abort the rest.

## Architecture and persistence

- Keep dependency direction inward: UI -> application -> domain. Lower layers do not import upper layers.
- Reach across modules only through their public exports. Keep filesystem, database, and HTTP access behind testable interfaces.
- Write multi-step persistent files atomically: write a temporary file in the target directory, then replace the target.
- Protect runtime credentials: use restrictive file permissions, exclude recoverable credentials from off-host backups, and document datastore exclusions when applicable.

## Security and delivery

- Give served HTML a Content Security Policy. Pin external scripts with integrity metadata or self-host them.
- Record any CSP exception in architecture documentation. Turn documented security invariants into a runtime check or test; add malformed-input negative tests for authentication guards.
- Pin third-party CI actions to full commit SHAs, set least-privilege job permissions, and isolate secret-bearing or write-capable jobs from unpinned dependency installation.
- Add concurrency control to workflows that can retrigger themselves. Use locked, bounded, hashed dependencies where the ecosystem supports them.
- Harden deployed system services with `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, and an explicit `CapabilityBoundingSet=`.
- When a change requires a human action outside version control, report a concrete action list in the completion message.

## Verification and maintenance

- Cover new logic with tests; add a regression test for every fixed defect and documented fragile invariant.
- Profile new or changed integration tests that create environments, install packages, or run
  external tools with `pytest --durations`. Do not repeat immutable preparation slower than two
  seconds at function scope. Share it only with proven isolation, while preserving real artifact
  paths, assertions, and failure injection. Compare focused and full wall time; require explicit
  ticket justification for a full-suite regression over 10% or 10 seconds.
- Run the complete project verification suite before committing. Every skipped or expected-failure test states its reason.
- Keep documentation current when behavior, interfaces, architecture, configuration, or feature specifications change.
- When removing a feature, remove its orphaned dependencies, configuration, files, and selectors in the same change.

## Review heuristics

Apply these as judgement calls, not hard violations. A documented repository rule overrides them, and tooling-enforced concerns need not be reported again.

- **Mysterious Name:** a name hides its purpose; rename it or clarify the design.
- **Duplicated Code:** equivalent logic recurs; extract the shared shape when it improves cohesion.
- **Feature Envy:** behavior relies mostly on another object's data; move it nearer to that data.
- **Data Clumps:** fields or parameters travel together; model the concept explicitly.
- **Primitive Obsession:** a primitive represents an important domain concept; introduce a focused type.
- **Repeated Switches:** equivalent branching recurs; centralize the decision or model polymorphism.
- **Shotgun Surgery:** one concern requires scattered edits; gather the concern behind a clearer boundary.
- **Divergent Change:** one module changes for unrelated reasons; split responsibilities.
- **Speculative Generality:** code anticipates no stated need; remove the unused flexibility.
- **Message Chains:** callers navigate deep object chains; expose an operation at the boundary.
- **Middle Man:** an abstraction only delegates; remove it when it adds no policy.
- **Refused Bequest:** inheritance is mostly ignored; prefer composition.
