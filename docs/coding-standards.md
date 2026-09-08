# Coding Standards

Apply these standards to code changes and reviews. Repository-specific rules override
them where they conflict.

## Decision procedure (Ponytail)

Ponytail means efficiency after understanding the problem. Apply this procedure before
writing code; use the sections below as reference for every affected contract.

1. Read the complete task and the affected code. Trace the real producer -> caller ->
   implementation -> consumer flow, including error paths. Continue when the required
   outcome, affected contracts, and authoritative change location are identified.
2. Climb the ladder below in order. Stop at the first rung that fully meets those contracts;
   identify the existing mechanism or the concrete gap before proceeding to a lower rung.
3. Implement the selected solution under the reference rules, then complete the applicable
   checks in **Verification and maintenance**. For reviews, apply the same ladder to the
   proposed change and assess each applicable rule; distinguish verified outcomes from gaps.

### Reuse ladder

1. **YAGNI:** establish whether anything needs building; an already-satisfied requirement
   needs no implementation. If a simpler outcome covers a complex request, explain the
   alternative to the parent agent if it changes the requested outcome or contract.
2. **Codebase:** reuse an existing helper, utility, or established pattern.
3. **Standard library:** use an existing language facility.
4. **Platform:** use a native platform feature.
5. **Dependencies:** use an already-installed dependency, subject to the inspection rule in
   **Scope and design**.
6. **One line:** use a single expression or statement when it remains readable and preserves
   edge-case behavior, validation, and error handling.
7. **Minimum implementation:** write only the code needed to close the identified gap.

## Scope and design

- Make the smallest end-to-end change that satisfies the stated requirement; add features, abstractions, and dependencies only when the requirement or an applicable project contract needs them.
- Report unrelated defects; do not repair them as part of the change.
- Prefer a simpler solution when it removes a dependency, a complexity layer, or moving parts.
- Build incrementally: preserve a working path while adding the next capability.
- Before adding code or a dependency, check existing project facilities and current project dependencies, including their documentation and types.
- Resolve implementation details autonomously within the assigned scope. Escalate ambiguous goals and public interface, data schema, module boundary, or cross-module contract changes outside that scope to the parent agent before dependent work.
- Give an optional parameter one safe, unambiguous meaning when omitted. Require an explicit choice when omission can change correctness.
- Apply the evidence requirements in **Verification and maintenance** before removing behavior.
- Keep abstractions within explicitly requested work or the concrete policy, implementation-change, and test-boundary purposes in **Architecture and persistence**. Apply **Review heuristics** to the affected design; they do not authorize unrelated abstraction work.
- Preserve trust-boundary validation, data-loss-preventing error handling, security, accessibility, and every explicitly requested capability when simplifying. For hardware-facing changes, account for actual calibration, clock drift, sensor error, and measured platform limits rather than assuming ideal specifications.

## Code structure

- Give each function and module one focused responsibility.
- Keep control flow shallow; use early returns instead of nesting three or more levels.
- Prefer deep modules: keep the public interface small and place policy in the implementation. Retain abstractions only for the purposes defined in **Scope and design** and **Architecture and persistence**.
- Follow the language and project naming conventions. Use precise names; avoid abbreviations and generic placeholders such as `data`, `temp`, `val`, `x`, and `y` when a domain name is available.
- Document a public contract when its constraints, errors, side effects, or purpose are not clear from its signature. Use comments only for non-obvious context that code alone cannot communicate: a constraint, workaround, rationale, or decision.
- Mark a deliberate simplification with a known operational ceiling using a `ponytail:` comment at the implementation. Name the ceiling and the upgrade path, for example: `ponytail: O(n^2) scan; use an indexed lookup if measured input sizes exceed the latency budget.` Apply this to real limitations such as global-lock serialization or naive heuristics; ordinary simple code needs no marker.
- In typed languages, annotate parameters and return values according to the project's type system.
- Store user-configurable values, secrets, environment-dependent URLs, and operational timeouts in the project's configuration mechanism. Keep stable protocol constants with the code that owns them. Keep local secret configuration ignored.
- Resolve required configuration before expensive or irreversible work. Add every new environment key to the repository's example configuration in the same change.

## Errors, inputs, and observability

- Use the project's logger for runtime observability. Log an exception once at the boundary that owns its handling; otherwise translate it with its cause intact or let it propagate.
- Use exceptions for contract violations and explicit absence/error results for expected outcomes; keep one error-handling style within a module.
- Catch specific exception types. Do not silently suppress failures.
- Let unexpected failures surface inside trusted code; handle them only for a current recovery, translation, cleanup, protocol, or supported compatibility contract. Require a concrete boundary or invariant for repeated validation. Replace incorrect behavior at its source. Treat missing and invalid required inputs as visible failures unless the contract explicitly permits recovery; accept absence only for inputs declared optional.
- For a bug fix, search every caller of the function being changed and trace each affected path, including sibling paths absent from the report. Fix the root cause once at the narrowest shared authoritative seam when the callers share the faulty contract; retain distinct caller contracts. Continue when each affected caller is accounted for and the chosen seam explains both the reported symptom and sibling behavior.
- Validate external input against its contract. Allowlist selectable identifiers and operations; parameterize query values, enforce path containment, and use structured logging with appropriate encoding.
- Bound external input at ingestion: ranges for numbers, lengths for strings, sizes for files and bodies, transfer deadlines for network calls, and page/item limits for pagination.
- Give each batch an explicit failure contract: atomic, fail-fast, or independently isolated per item. Preserve that contract in error handling and tests.

## Architecture and persistence

- Keep dependency direction inward: UI -> application -> domain. Lower layers do not import upper layers.
- Reach across modules only through their public exports. Put filesystem, database, and HTTP policy behind stable boundaries; introduce an interface when it hides policy, permits a required implementation change, or provides a controlled test double.
- When a domain field changes what an entity is, update every affected projection in the same change, including filters, counts, badges, serialization, and access rules.
- For schema or identity changes, enumerate and update every public reader, including CLIs, converters, visualizers, and resume paths. Establish compatibility from an explicit contract rather than inferring it from an absent field or file.
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

- Cover new logic, fixed defects, and documented fragile invariants with behavioral tests. Prefer strengthening an existing test when it can distinguish the regression; add a test when an independently meaningful outcome would otherwise lack protection. Derive expected results from the contract or an independent invariant, not by repeating the implementation.
- Leave at least one runnable behavioral check for every non-trivial logic change. Use the existing test infrastructure; where none exists, use an assert-based self-check instead of adding a framework or fixtures solely for that check. Cover every distinct outcome required below. A trivial one-line change needs no new test when it adds no logic, fixes no defect, and affects no invariant or boundary requiring protection.
- Before test cleanup changes production behavior, map affected tests to their contract, production branch, observable outcome, independent expected result, and failure domain. Retain the strongest evidence for each distinct outcome, preferably at a public seam. Preserve separate success, rejection, security, concurrency, persistence, protocol, resource, numerical, and supported compatibility semantics. Remove redundant tests and their exclusive fixtures and helpers; removing production behavior requires separate evidence.
- Before removing a production/test cluster, resolve its purpose against explicit current requirements, real callers, public contracts, specifications, history, or boundary invariants. Trace runtime reachability from a non-test producer; test-injected configuration alone is insufficient. Preserve the cluster while evidence is unresolved. Remove code and tests that only justify each other once the absence of an independent purpose is established. A test can itself specify public behavior; change that behavior only with positive evidence that its contract has changed or ended. Explicit current requirements take precedence over conflicting historical tests.
- For checksums, receipts, manifests, and validators, identify the failure or trust boundary they detect. Shared implementation or inputs warrant investigation; preserve required integrity checks. When removal is justified, remove the obsolete mechanism and its exclusive tests and support together. Verify cleanup with existing checks; add a replacement test only for a real protection gap with an independent expected result.
- When cleanup crosses layers, trace each current producer -> reader -> consumer path and retain a hermetic integration test for each distinct delivery path. Use repository-managed or test-created inputs and temporary outputs; skip for an optional dependency only where the test actually requires it. After test removal or consolidation, compare collection and run the surviving suite; resolve unexpected skips, deselections, or an empty collection. Compare worktree state before and after runs that may generate files.
- For authorization, financial rules, state transitions, data integrity, and boundary-heavy branching, map each contract rule to an observable outcome. Use the smallest case set that distinguishes every rule and boundary; for a failure, assert both its result and the absence of its forbidden side effect.
- Parameterize tests only when they share one seam, setup, and assertion shape. Treat coverage as execution evidence, not proof of behavior. When mutation testing is configured, run the changed critical scope, strengthen observable assertions, and leave no actionable survivor.
- Before changing fixture ownership in an integration test that creates an environment, installs a package, starts a service, or invokes an external tool, time the smallest affected scope and capture its slowest setup, call, and teardown operations. For every operation slower than two seconds, name its owner and report its time, classification, impact, and next action; classify it as mutable state, immutable infrastructure, or behavior under test.
- Keep mutable test state function-scoped. Share immutable infrastructure only when tests cannot modify it and order, repetition, and parallel execution remain isolated. Require ticket justification when immutable preparation slower than two seconds still repeats per test. When initialization is behavior under test, preserve its real public path and optimize the authoritative production seam instead.
- Preserve the real artifact, integrity checks, installation path, commands, assertions, and failure injection required by the contract. An optimization is valid only when the original failure still makes the test fail and subprocess, timeout, integrity, and stage failures remain fail-closed.
- Measure the focused scope and full suite in the same environment before and after a test optimization. Record wall time, slowest operations, and percentage change; require explicit ticket justification for a full-suite regression over 10% or 10 seconds.
- Run the complete project verification suite before committing. Every skipped or expected-failure test states its reason.
- Keep documentation current when behavior, interfaces, architecture, configuration, or feature specifications change.
- When removing a feature, remove its orphaned dependencies, configuration, files, and selectors in the same change.
- Finish only when every applicable rule above is satisfied, required checks have returned, and each claimed outcome has supporting evidence. Report a failed or unavailable check with its affected contract and next action; an unavailable check does not establish completion.

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
- **Middle Man:** an abstraction only delegates; remove it when it serves none of the purposes in **Scope and design** and **Architecture and persistence**.
- **Refused Bequest:** inheritance is mostly ignored; prefer composition.
