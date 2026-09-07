# Quality Gate

Quality Gate applies the same versioned quality contract to the staged Git candidate and to GitHub Actions.

## Requirements

- Git.
- Python 3.11 or newer for the launcher.
- The `quality-gate` launcher and native Git hooks installed on the workstation.
- Access to the immutable release asset selected for the repository. Network access is needed only for the first synchronization.

## Configure a repository

1. Copy [templates/quality-gate.toml](templates/quality-gate.toml) to `quality-gate.toml` in the repository root.
2. Set the repository name, required documents, limits, timeouts, and immutable `quality.policy_release`.
3. Add one `[[python]]` table for each Python component. Omit these tables when the repository has no Python code.

   ```toml
   [[python]]
   name = "application"
   path = "src"
   python_version = "3.11"
   dependency_inputs = ["pyproject.toml"]
   test_paths = ["tests"]
   tests_applicable = true
   timeout_seconds = 300
   ```

4. Add one `[[web]]` table for each project-owned JavaScript/CSS boundary. Patterns and explicit exclusions are relative to the component root.

   ```toml
   [[web]]
   name = "frontend"
   root = "static"
   javascript = ["js/**/*.js"]
   css = ["css/**/*.css"]
   exclude = ["vendor/**", "generated/**"]
   ```

   Optional `[web.limits]` values are expressed in KiB. Defaults are 100 per JavaScript file, 50 per CSS file, 250 total JavaScript, and 100 total CSS.

5. Copy [templates/quality.yml](templates/quality.yml) to `.github/workflows/quality.yml` and replace `<40-character-commit-sha>` with the exact commit SHA of the reusable workflow.
6. Copy [templates/dependabot.yml](templates/dependabot.yml) to `.github/dependabot.yml`.
7. Synchronize the release named by `quality.policy_release`, prepare its isolated runtimes, and verify the repository.

   ```powershell
   quality-gate sync --url "<release-asset-url>" --version <release>
   quality-gate validate
   quality-gate setup
   quality-gate doctor
   quality-gate audit
   ```

`quality-gate audit` performs the initial full-history security scan. Normal work uses `quality-gate check`:

```powershell
git add <files>
quality-gate check
git commit
```

The manual check is the first line of defense. The commit hook repeats the check against the exact staged candidate. GitHub Actions runs the same contract again in CI.

## Checks

### Repository and Git candidate

- The manifest uses schema 2 and contains valid repository, Python, and web component declarations.
- Declared component paths, test paths, dependency inputs, limits, and timeouts are valid.
- The staged candidate has no unresolved index merge entries or intent-to-add entries.
- The Git index does not change while the check is running.
- Text files contain no unresolved merge-conflict markers.
- Tracked files contain no caches, temporary files, editor backups, or other known repository junk.
- Git blobs do not exceed `repository.limits.max_blob_size_mib`, which defaults to 5 MiB.
- Tracked paths do not collide on case-insensitive file systems.
- Symbolic links do not use absolute targets or resolve outside the repository.

### Security and GitHub Actions

- Gitleaks scans the staged candidate for passwords, tokens, keys, and other secrets.
- `audit` scans all reachable Git history; CI scans the verified base-to-head range.
- Secret values are redacted from normal and verbose output.
- Every external action and reusable workflow is pinned to a full 40-character commit SHA.
- Workflows declare explicit top-level permissions and do not grant broader-than-read access without an isolated deployment case.
- Write-capable jobs are not reachable from pull requests and use only permitted deployment scopes.
- Non-reusable jobs have a positive `timeout-minutes` value.
- Workflows declare explicit concurrency cancellation; pull-request workflows cancel superseded runs.
- Exactly one stable `Quality Gate` job runs for pull requests and default-branch pushes.

### Documentation

- Every document listed in `repository.required_documents` exists, is a readable file, and is not empty.
- Markdown files are readable UTF-8, and their relative links resolve inside the repository.
- Every Python component path declared in the manifest appears in Markdown documentation.

Quality Gate checks only these objective documentation properties. It does not grade writing style, visual design, badges, or the subjective completeness of a document.

### Python components

- Ruff checks each declared component for lint violations using the policy release configuration.
- Ruff checks formatting without changing files.
- mypy checks types in every declared component.
- deptry compares imports with declared runtime and development dependencies for components that declare dependency inputs.
- pytest runs the declared test paths sequentially and treats collection errors, failures, and timeouts as blocking results.
- Coverage is report-only and never blocks the verdict. It runs only when the selected policy release includes a pinned coverage provider.
- Each component runs with its declared Python version in an isolated, fingerprinted runtime built from its dependency inputs and pinned policy tools.

### Web components

- Each component declares a bounded root and JavaScript and/or CSS asset patterns.
- Generated and vendor assets remain in scope unless an explicit component exclusion matches them.
- Staged file bytes are checked against per-file and component-total budgets.
- The pinned standalone Biome binary lints and checks formatting for every declared asset without Node.js.
- Biome uses only stable JavaScript and CSS rules; nursery, experimental, SCSS, and embedded-language checks are excluded.
- Findings use repository-relative forward-slash paths on every supported platform.

### Escaped-defect lessons

- Lesson files must use the defined Markdown front matter, contain every required field, have unique IDs, and stay within the file and collection limits.
- Normal checks keep incomplete lessons visible without blocking the verdict.
- `audit` and release validation require every recorded lesson to contain both a policy adaptation and verification evidence.
