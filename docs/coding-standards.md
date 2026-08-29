# Coding Standards

Apply these standards to code changes and reviews. Repository-specific rules override
them where they conflict.

## Scope and design

- Choose the smallest end-to-end change that satisfies the stated requirement.
- Do not add speculative features, abstractions, dependencies, or scope.
- Report unrelated defects; do not repair them as part of the change.
- Prefer a simpler solution when it removes a dependency, a complexity layer, or moving parts.
- Build incrementally: preserve a working path while adding the next capability.
- Before adding code or a dependency, check existing project facilities and current project dependencies, including their documentation and types.
- Clarify an unclear goal. Resolve implementation details autonomously unless they change a public interface, data schema, module boundary, or cross-module contract.
- Give an optional parameter one safe, unambiguous meaning when omitted. Require an explicit choice when omission can change correctness.

## Code structure

- Give each function and module one focused responsibility.
- Keep control flow shallow; use early returns instead of nesting three or more levels.
- Prefer deep modules: keep the public interface small and place policy in the implementation. Remove abstractions that only delegate without adding policy.
- Follow the language and project naming conventions. Use precise names; avoid abbreviations and generic placeholders such as `data`, `temp`, `val`, `x`, and `y` when a domain name is available.
- Document a public contract when its constraints, errors, side effects, or purpose are not clear from its signature. Use comments only for non-obvious context that code alone cannot communicate: a constraint, workaround, rationale, or decision.
- In typed languages, annotate parameters and return values according to the project's type system.
- Store user-configurable values, secrets, environment-dependent URLs, and operational timeouts in the project's configuration mechanism. Keep stable protocol constants with the code that owns them. Keep local secret configuration ignored.
- Resolve required configuration before expensive or irreversible work. Add every new environment key to the repository's example configuration in the same change.

## Errors, inputs, and observability

- Use the project's logger for runtime observability. Log an exception once at the boundary that owns its handling; otherwise translate it with its cause intact or let it propagate.
- Use exceptions for contract violations and explicit absence/error results for expected outcomes; keep one error-handling style within a module.
- Catch specific exception types. Do not silently suppress failures.
- Validate external input against an allowlist before using it as a key, path component, query element, column, or log interpolation.
- Bound external input at ingestion: ranges for numbers, lengths for strings, sizes for files and bodies, transfer deadlines for network calls, and page/item limits for pagination.
- Give each batch an explicit failure contract: atomic, fail-fast, or independently isolated per item. Preserve that contract in error handling and tests.

## Architecture and persistence

- Keep dependency direction inward: UI -> application -> domain. Lower layers do not import upper layers.
- Reach across modules only through their public exports. Put filesystem, database, and HTTP policy behind stable boundaries; introduce an interface when it hides policy, permits a required implementation change, or provides a controlled test double.
- When a domain field changes what an entity is, update every affected projection in the same change, including filters, counts, badges, serialization, and access rules.
- Build a path with the semantics of its destination environment. Normalize both operands before comparing paths from different sources.
- Deliver streaming events as they arrive when timeouts, liveness, or process control depend on them.
- Write multi-step persistent files atomically: write a temporary file in the target directory, then replace the target.
- Protect runtime credentials: use restrictive file permissions and exclude recoverable credentials from off-host backups. When a datastore contains recoverable credentials, document its backup exclusions.

## Security and delivery

- For served HTML, apply a Content Security Policy that allows only the resource origins and capabilities required by the deployment. Pin external scripts with integrity metadata or self-host them.
- Record any CSP exception in architecture documentation. Turn documented security invariants into a runtime check or test; add malformed-input negative tests for authentication guards.
- Pin third-party CI actions to full commit SHAs, set least-privilege job permissions, and isolate secret-bearing or write-capable jobs from unpinned dependency installation.
- Add concurrency control to workflows that can retrigger themselves. Use locked, bounded, hashed dependencies where the ecosystem supports them.
- For systemd services, apply `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, and an explicit `CapabilityBoundingSet=` where the service contract permits them; document required exceptions.
- When a change requires a human action outside version control, report a concrete action list in the completion message.

## Verification and maintenance

- Cover new logic with tests; add a regression test for every fixed defect and documented fragile invariant.
- For authorization, financial rules, state transitions, data integrity, and boundary-heavy branching, map each contract rule to an observable outcome. Use the smallest case set that distinguishes every rule and boundary; for a failure, assert both its result and the absence of its forbidden side effect.
- Parameterize tests only when they share one seam, setup, and assertion shape. Treat coverage as execution evidence, not proof of behavior. When mutation testing is configured, run the changed critical scope, strengthen observable assertions, and leave no actionable survivor.
- Before changing fixture ownership in an integration test that creates an environment, installs a package, starts a service, or invokes an external tool, time the smallest affected scope and capture its slowest setup, call, and teardown operations. For every operation slower than two seconds, name its owner and report its time, classification, impact, and next action; classify it as mutable state, immutable infrastructure, or behavior under test.
- Keep mutable test state function-scoped. Share immutable infrastructure only when tests cannot modify it and order, repetition, and parallel execution remain isolated. Require ticket justification when immutable preparation slower than two seconds still repeats per test. When initialization is behavior under test, preserve its real public path and optimize the authoritative production seam instead.
- Preserve the real artifact, integrity checks, installation path, commands, assertions, and failure injection required by the contract. An optimization is valid only when the original failure still makes the test fail and subprocess, timeout, integrity, and stage failures remain fail-closed.
- Measure the focused scope and full suite in the same environment before and after a test optimization. Record wall time, slowest operations, and percentage change; require explicit ticket justification for a full-suite regression over 10% or 10 seconds.
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
