# Unified Quality Gate Hardening and Fleet Migration

## Problem Statement

The project owner wants one shared Quality Gate release that consistently protects every current repository from high-signal Python defects, unsafe patterns, dependency drift, excessive function complexity, and unbounded JavaScript or CSS growth. The current fleet selects different policy releases, the shared Ruff policy omits several valuable rule families, dependency declarations are not checked against imports, and static web assets are outside the component contract.

Adding every available metric or scanner would not solve the problem. Redundant and heuristic checks can create duplicate findings, false positives, and metric-driven rewrites that make code harder to understand. The gate must therefore enforce a deliberately bounded set of objective invariants, provide narrow governed exceptions, preserve identical local and CI behavior, and migrate the complete fleet to one immutable release pair.

## Solution

Deliver one unified Quality Gate policy for the current repository fleet. Refresh the existing pinned Python toolchain, extend Ruff with calibrated high-signal Bugbear, Ruff-native, and security rules, retain explicit function complexity budgets, and add dependency hygiene through deptry. Extend the manifest with web components that identify project-owned JavaScript and CSS assets, enforce per-file and component-wide size budgets, and run stable Biome lint and formatting checks through verified standalone binaries. Let repositories declare strict supplemental functional tests that remain outside Quality Gate execution while one final test-runner scope combines them with the staged gate without rerunning tests already owned by the gate.

Package the complete policy as one immutable cross-platform release. Migrate each consumer independently to the new policy release and its matching reusable workflow revision, remediate genuine findings without weakening the shared policy, and finish with a fleet-wide convergence audit.

The gate guarantees concrete machine-verifiable invariants. It does not claim to prove subjective architecture quality, complete functional correctness, or the absence of every security defect.

## User Stories

1. As the project owner, I want every repository to use one policy release, so that quality behavior is consistent across the fleet.
2. As the project owner, I want the gate to block high-signal defects, so that an automated coding agent cannot silently deliver obviously unsafe or fragile code.
3. As the project owner, I want noisy and redundant scanners excluded, so that agents fix real problems instead of optimizing for duplicate findings.
4. As the project owner, I want explicit maintainability budgets, so that functions cannot grow without a clear bound.
5. As the project owner, I want JavaScript and CSS included in the shared contract, so that web code is not an unverified blind spot.
6. As the project owner, I want static asset growth bounded, so that a single file or component cannot expand indefinitely.
7. As the project owner, I want the completed program divided into independently executable tickets, so that each stage remains understandable and verifiable.
8. As a coding agent, I want a stable Ruff policy, so that the same Python source produces the same findings locally and in CI.
9. As a coding agent, I want security findings to identify the exact unsafe pattern, so that I can repair the cause instead of suppressing a category.
10. As a coding agent, I want valid test assertions treated differently from production assertions, so that security linting does not remove meaningful tests.
11. As a coding agent, I want narrow waiver semantics, so that framework hooks and dynamic behavior can be represented without weakening unrelated checks.
12. As a coding agent, I want missing quality tools reported as unavailable, so that an incomplete runtime cannot produce a false pass.
13. As a Python maintainer, I want cyclomatic complexity capped at 10, so that control flow remains bounded.
14. As a Python maintainer, I want statements per function capped at 50, so that large procedural functions are rejected before they become entrenched.
15. As a Python maintainer, I want Bugbear findings enforced, so that common design and correctness traps are caught by the existing linter runtime.
16. As a Python maintainer, I want calibrated Ruff-native rules enforced, so that valuable Ruff checks are gained without enabling an indiscriminate rule family.
17. As a Python maintainer, I want calibrated Ruff security rules enforced, so that unsafe Python patterns are blocked without adding a duplicate Bandit execution.
18. As a Python maintainer, I want imports compared with dependency declarations, so that undeclared packages cannot enter through a developer environment by accident.
19. As a Python maintainer, I want transitive-only imports rejected, so that runtime behavior does not depend on another package's private dependency graph.
20. As a Python maintainer, I want runtime and development dependencies distinguished, so that test tools are not misclassified as unused application dependencies.
21. As a Python maintainer, I want unused runtime dependencies reported, so that obsolete packages do not remain in the attack and maintenance surface.
22. As a plugin-based application maintainer, I want dynamic dependencies to use reviewed waivers, so that static analysis does not force removal of real runtime integrations.
23. As a web maintainer, I want project-owned JavaScript and CSS declared as a component, so that the gate checks only the intended asset boundary.
24. As a web maintainer, I want generated and vendor assets excluded only explicitly, so that broad directory patterns cannot hide first-party code.
25. As a web maintainer, I want a JavaScript file capped at 100 KiB, so that one source asset cannot become an unchecked monolith.
26. As a web maintainer, I want a CSS file capped at 50 KiB, so that one stylesheet cannot grow without an architectural decision.
27. As a web maintainer, I want total JavaScript capped at 250 KiB per component, so that growth split across files remains visible.
28. As a web maintainer, I want total CSS capped at 100 KiB per component, so that stylesheet growth split across files remains visible.
29. As a web maintainer, I want JavaScript and CSS linted by one tool, so that raw static assets receive a consistent baseline without a Node project.
30. As a web maintainer, I want formatting checked without modifying the candidate, so that the gate remains read-only.
31. As a release operator, I want every policy tool pinned to an exact version, so that an upstream release cannot change the verdict unexpectedly.
32. As a release operator, I want every platform binary verified by digest, so that the immutable release inventory remains trustworthy.
33. As a release operator, I want the exact candidate validated before publication, so that the published release matches reviewed source and artifacts.
34. As a CI maintainer, I want Windows and Linux to expose the same check surface, so that platform choice cannot weaken the contract.
35. As a repository maintainer, I want migration to update the policy release and workflow revision together, so that local and CI execution cannot select mismatched contracts.
36. As a repository maintainer, I want each migrated project to pass setup, doctor, full verification, and audit, so that migration completion is evidenced at the public boundary.
37. As the fleet owner, I want each repository migrated independently after release publication, so that one difficult remediation does not block work on another repository.
38. As the fleet owner, I want a final convergence audit, so that no active project remains on an older or mismatched policy pair.
39. As a reviewer, I want every policy exclusion to include a concrete rationale, so that convenience does not silently become shared policy.
40. As a future maintainer, I want the supported and excluded checks documented, so that later maintenance starts from the decisions made in this program.
41. As a coding agent, I want one final verification scope, so that repository tests and the staged gate run without duplicate suites or orchestration branches.
42. As a repository maintainer, I want supplemental tests declared in the existing manifest, so that test ownership remains discoverable in one place.
43. As a policy maintainer, I want supplemental declarations validated but not executed by Quality Gate, so that repository-specific functional tests remain owned by their repository and CI.

## Implementation Decisions

- The shared gate enforces objective invariants and does not claim to certify subjective architecture quality.
- Existing policy tools are refreshed to current compatible stable releases before the new Ruff selection is finalized.
- Ruff remains the single Python lint and formatting engine.
- The shared Ruff policy enables Bugbear and a calibrated stable subset of Ruff-native and security rules.
- The Ruff selection ticket owns calibration against the gate repository and all five consumers. Every excluded candidate rule must record a false-positive, overlap, or applicability rationale.
- Cyclomatic complexity remains capped at 10, and statements per function remain capped at 50.
- Test-only security exceptions are scoped so that the equivalent production finding remains blocking.
- Bandit is not added because its intended policy surface substantially overlaps Ruff security rules.
- deptry is the dependency-hygiene engine for applicable Python components.
- Dependency hygiene covers undeclared imports, transitive-only imports, dependency-group mistakes, and unused runtime dependencies supported by deptry.
- Dynamic imports, plugin entry points, framework discovery, and configuration-driven dependencies use the existing typed waiver model rather than a separate suppression system.
- A missing or unusable required tool produces `unchecked`; it never produces a passing result.
- The manifest gains a web component contract with a bounded root, project-owned JavaScript and CSS assets, and explicit generated or vendor exclusions.
- Manifest validation rejects unsafe paths, assets outside the repository, invalid patterns, and duplicate component identities.
- Default web budgets are 100 KiB per JavaScript file, 50 KiB per CSS file, 250 KiB total JavaScript per component, and 100 KiB total CSS per component.
- Asset budgets measure staged project-owned bytes. They do not attempt to estimate compressed transfer size or runtime performance.
- Biome is the single JavaScript and CSS lint and formatting engine for the current raw-asset fleet.
- Biome is distributed as exact standalone platform binaries; Node.js and a package manager are not required.
- Only stable Biome JavaScript and CSS capabilities are enabled. Nursery rules, experimental language support, SCSS, and embedded-language checks are excluded.
- The runner emits stable component-scoped check identifiers for dependency hygiene, web asset budgets, Biome lint, and Biome formatting.
- Local hooks and CI consume the same immutable policy release and expose the same verdict semantics.
- The manifest may declare supplemental tests through strict runner types and repository-relative targets. It does not accept arbitrary commands.
- Quality Gate validates supplemental declarations but does not execute them during `check`, `audit`, hooks, or the reusable workflow.
- The initial supplemental runner set contains only `node-test`; another runner requires a demonstrated repository need and a contract change.
- The typed test-runner treats `scope: full` as complete repository verification: with a Quality Gate manifest it runs declared supplemental tests and then the staged gate, and without a manifest it runs the discovered complete suite.
- Repository CI remains the enforcement owner for supplemental tests outside the agent workflow.
- The release inventory includes exact versions and digests for every wheel, policy file, and platform binary.
- The complete policy is published as one new immutable release after Python and web slices pass their own verification.
- Each consumer migration updates the policy release and matching reusable workflow revision together.
- ARGUS_BRAIN and ARGUS_TG migrate their Python components.
- ARGUS_tracker_multiuser, ARGUS_TRADING, and ARGUS_WEB migrate both Python components and project-owned static web assets.
- Fleet convergence is complete only when all five consumers use the same release pair and independently pass setup, doctor, full verification, and audit.

## Testing Decisions

- Tickets 01 through 06 verify source behavior, exact source pins, candidate inventory inputs, and platform-neutral contracts with focused tests and the full suite. They do not claim parity for an unpublished release.
- Ticket 07 is the single owner of final release assembly, immutable publication, exact released tool inventory, and release-backed Windows/Linux parity. Earlier tickets must name this deferred boundary instead of requiring the current published release to contain their changes.
- Tests assert external behavior and verdicts rather than command construction or private helper structure.
- The primary seam is the public Quality Gate command operating on staged fixture repositories. A deliberately invalid candidate must fail with the expected stable result, and a corrected candidate must pass.
- Manifest fixtures cover valid web components, invalid roots and patterns, duplicate identities, explicit exclusions, default budgets, and boundary values at and above each budget.
- Python fixtures cover the selected Ruff findings, valid test-only exceptions, preserved complexity thresholds, each supported dependency-hygiene finding, dynamic-dependency waivers, and unavailable-tool outcomes.
- Web fixtures cover JavaScript lint failures, CSS lint failures, formatting failures, clean assets, explicit exclusions, per-file budget failures, total budget failures, and unavailable or corrupt Biome binaries.
- Existing runner and CLI contract tests are the prior art for stable check identifiers, verdict aggregation, timeout behavior, redaction, and `unchecked` semantics.
- Supplemental manifest fixtures cover valid `node-test` targets, duplicate identities, unknown runners, unsafe paths, and empty matches. Test-runner contract tests prove that every supplemental result is collected before the staged gate and that repositories without a manifest retain ordinary full-suite behavior.
- Existing release and distribution tests are the prior art for inventory validation, digest verification, safe extraction, corrupt artifact handling, and immutable release selection.
- The release seam validates the exact candidate with the release controller and compares the complete check surface, tool inventory, verdicts, and redaction behavior on Windows and Linux.
- Each implementation slice runs its smallest safe affected suite before the final full suite.
- The unified release is eligible for publication only after one successful full suite, one staged Quality Gate run, audit, and release validation on the final candidate. After immutable publication, the matching release-backed Windows/Linux parity run must pass before ticket 07 is complete or consumer migration begins.
- The fleet seam runs setup, doctor, full verification, and audit independently in every consumer after migration.
- The final fleet result records the verified policy release, workflow revision, platforms, and repository set.

## Out of Scope

- LOC-per-file limits.
- ABC or incorrectly named ABS scores.
- Vulture dead-code analysis.
- A separate Bandit execution.
- Ruff SIM and C4 rule families.
- Preview or unstable Ruff rules.
- Node.js, npm, package-manager lockfiles, and bundler execution inside Quality Gate. Explicit repository-owned `node:test` remains supplemental.
- Production bundle generation, compressed transfer-size budgets, and runtime performance budgets.
- TypeScript, SCSS, Sass, Less, Vue, Svelte, Astro, and embedded-language analysis.
- Automatic proof of architecture quality, functional correctness, or complete security.
- Migration of repositories outside the five consumers identified in this program.

## Further Notes

- The current consumer fleet contains five repositories. Three contain raw project-owned JavaScript and CSS without a package manifest or bundler configuration.
- Current observed web assets fit within the selected default budgets: the largest JavaScript file is approximately 81 KiB, the largest CSS file approximately 37 KiB, and the largest component JavaScript total approximately 165 KiB.
- Several current JavaScript and CSS assets are stored as one physical line. Biome formatting is expected to create deliberate migration diffs before the web checks become green.
- The local issue tracker contains thirteen vertical-slice tickets. Policy implementation can proceed on independent unblocked slices; consumer migrations begin only after the unified immutable release is published.
- Future gate work is triggered by a material platform or stack change, such as a new Python version, a new frontend language, a bundler-based application, a supported-platform change, or a security-tool maintenance requirement.
