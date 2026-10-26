---
name: post-release-quality-gate
description: Verify the latest stable Quality Gate release and activate it in this provider repository.
disable-model-invocation: true
---

# Activate the latest Quality Gate release

Run this skill in the Quality Gate provider repository after its release workflow publishes a release. It updates this repository, the installed CLI package, and the current user's local release cache. It does not update consumer repositories.

## Procedure

1. Read `docs/architecture.md`, `docs/agent-reference.md`, `docs/release.md`, and `docs/publication.md`. Read `docs/quality-policy-changes.md` if the release requires a shared-behavior or policy implementation change. Check the current branch, `HEAD`, and Git status. Preserve pre-existing edits and staged files. Stop only if they conflict with files this procedure must update.

2. Query GitHub's REST API `GET https://api.github.com/repos/TheTraitor0FJesus/quality-gate/releases/latest` during this run and use its JSON response as the authoritative latest-release selector. Record the retrieval time in UTC. Treat rendered release pages, search results, and cached browser content as non-authoritative; if they disagree with the API response, record the discrepancy in the audit and follow the API after validating it. If the API request fails, stop instead of falling back to a rendered page. Require a `vMAJOR.MINOR.PATCH` tag, `draft: false`, and `prerelease: false`. Read the tag-specific release through the API and require its release ID and tag to match the latest-release response. Read its retained publisher receipt. Resolve the Git tag to a commit and require it to match the receipt's source SHA. Read that source commit's `.release/version.toml` and `.release/publisher.toml`, then verify that the declared version, repository, required producers, and platform assets match the receipt and release. Verify every required GitHub asset digest. Do not infer the version from a local branch, package file, tag sort order, or an unreleased candidate. Stop if the receipt, source identity, or any required asset is missing or inconsistent.

3. Compare the verified release with the provider's authorities and projections. Read `.release/publisher.toml` before deciding which projections apply. Reconcile:
   - `.release/version.toml` with the release version;
   - every declared product projection, including `pyproject.toml`, `quality_gate/__init__.py`, `templates/quality-gate.toml`, and `quality_gate/release_contract.py`;
   - `UNIFIED_RELEASE`, `UNIFIED_RELEASE_PACKAGE_VERSION`, and the exact release entry in `UNIFIED_RELEASE_PACKAGE_VERSIONS`;
   - `quality_gate/edit_protocol.py` supported policy releases only when the published release supports those edit protocols;
   - the provider's active policy in `quality-gate.toml`, the matching `tests/fixtures/release-parity/quality-gate.toml` pin, the active assertion in `tests/test_ci_workflow.py`, and the self-policy assertion/name in `tests/test_integrity.py`.
   Keep the one approved non-gating parity waiver and its reason unchanged.

4. Follow the native hook and CI selection. The pre-commit wrapper selects the release from the staged `quality-gate.toml` and verifies that exact release in the local cache. It has no separate policy-version pin. Verify the actual wrapper through the repository's configured hook route before proposing a hook code change. Download the exact platform asset and verify its GitHub SHA-256. If the release is absent or mismatched in the local cache, run `quality-gate sync --source <verified-asset> --version <tag>`.

   Use the same verified platform asset to update the installed CLI package. Run the release asset verifier at the verified release source commit to extract and return its single `quality_gate` wheel into a temporary directory outside the repository and release cache. Resolve the Python interpreter that owns the active `quality-gate` entry point; do not assume that `python` on `PATH` is the same interpreter. Install the verified wheel with `<entry-point-python> -m pip install --no-deps --upgrade <verified-wheel-path>`. Confirm that `quality-gate version` reports the target tag and that the entry point and imported `quality_gate` module belong to that interpreter. Remove the temporary extraction after installation. Stop without installing if the verifier cannot establish the asset, wheel, or entry-point interpreter identity.

   Then run `quality-gate validate`, `quality-gate setup`, and `quality-gate doctor` for this repository. Confirm the manifest pin and `setup` output identify the target release. `doctor` verifies that release and its runtimes; it does not print the selected version. `quality-gate version` reports the installed engine package version.

   `.release/release-tools.toml`'s `[bootstrap].policy_release` is an emergency self-CI fallback used only when the provider's selected release API returns 404. Do not advance this fallback during normal activation. If CI reports a v2 policy, inspect the staged manifest and the workflow's `policy_bootstrap` output. A report with `policy_bootstrap=true` means CI used the fallback; record that evidence and resolve the missing release instead of silently changing the fallback.

5. Update current-state documentation and add `docs/audits/provider-v<version>-acceptance.md` from verified evidence. Record the release API retrieval time and ID, release source SHA, release workflow/run and producer results, asset names and digests, changed self-policy locations, and the verification performed for this activation. Mark owner-merge or post-merge checks as pending until they actually pass. Preserve older release examples, compatibility pins, and dated audit findings as history. Do not replace every `2.*` or `3.*` occurrence; use `rg` to classify remaining version literals as active selection, compatibility support, test fixture, tool version, or bootstrap fallback.

6. Keep the scope inside this provider repository. Do not edit consumer manifests, consumer workflows, or consumer projects. Do not commit, push, publish, or change GitHub release data.

7. Stage the intended candidate only after identifying any pre-existing staged work. Use the configured test-runner with `scope: full` and `snapshot: working-tree`, following the request schema in `$CODEX_HOME/agents/test-runner.toml` (or `.codex/agents/test-runner.toml` when `CODEX_HOME` is unset). The run must include manifest preflight, every declared supplemental test, and the staged Quality Gate. Finish when the report is green, `quality-gate version` equals the verified release tag, the active provider pins and projections equal the verified release, the local cache contains the verified release and doctor reports ready, and the audit distinguishes completed evidence from pending integration. Leave all work uncommitted.
