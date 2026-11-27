# Independent testing

The v5 Gate checks code quality. Pytest runs independently, using the same manifest test paths and
prepared component runtimes. `setup` still supplies pytest; `runtimes --json` and test-path dependency
analysis keep their existing contracts. Coverage execution and reporting are outside Gate.

| Event | Quality checks | Full pytest |
| --- | --- | --- |
| Local commit | Exact staged candidate | Agent-selected development tests only |
| Feature push | Existing shared quality route when called | No automatic full run |
| PR to a non-default branch | PR range | No automatic full run |
| PR to the default branch | PR range | Proposed merge of current base and head |
| Ordinary default-branch push or merge | Existing detection check | No automatic full run |
| Product verification before publication | Installed candidate audit | Exact source and candidate policy on Windows and Linux |

The shared workflow has two independent execution jobs, each with its own environment preparation.
The lightweight `Quality Gate` aggregate retains the existing required-check name and requires both
results for default-target PRs. Strict up-to-date protection prevents reuse after a base change.
The workflow also checks merge parents and rechecks current PR identity before reporting success.
A retargeted PR requires a fresh check. Callers with explicit activity lists must include `edited`.

`quality_gate.pytest_ci` runs every applicable Python component and every declared test path, without
affected-test selection or environment provisioning. Assertion failures, collection errors, empty
or entirely skipped mandatory selections, unavailable runtimes, interruption, timeout and missing or
inconsistent JUnit evidence prevent success. Explicit `tests_applicable = false` components produce
`not_applicable` with their declared reason. Supplemental non-pytest runners retain their separate
execution ownership.

CI uploads `result.json`, per-component logs and JUnit reports. The result records source SHA,
engine version, policy release, manifest digest, component arguments, exit codes and totals. Source
changes during testing invalidate the result. These records explain the test job; they do not replace
the required job's actual Actions conclusion or authorize publication.

For local development, agents choose focused, expanded or full tests under the global instructions
and use the existing test-runner CLI. A successful Gate alone does not prove behavior. See
[operations](operations.md) for Gate commands and [release verification](release.md) for product tests.
