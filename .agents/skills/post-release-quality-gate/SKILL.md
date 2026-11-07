---
name: post-release-quality-gate
description: Activate a published Quality Gate release in the local installation.
disable-model-invocation: true
---

# Activate a local Quality Gate release

Run this skill in the Quality Gate provider repository after publication. Repository policy pins and release projections are updated through the rollout PR; this procedure updates the local CLI and release cache only.

1. Read [local tools and CI pins](../../../docs/consumer-setup.md#local-tools-and-ci-pins), [the release guide](../../../docs/release.md), and [the publisher guide](../../../docs/publication.md). Check the Git status and preserve existing changes. Do not change repository version pins, stage files as part of local activation.

2. Query `GET https://api.github.com/repos/TheTraitor0FJesus/quality-gate/releases/latest` during this run and record the UTC retrieval time. Require a stable `vMAJOR.MINOR.PATCH` tag and match the tag-specific API response's release ID and tag. If a specific version was requested, require it to match. Verify the retained publisher receipt, the tag's source commit, the source commit's `.release/version.toml` and `.release/publisher.toml`, the required producers and assets, and every required GitHub asset digest. Stop if any identity or required evidence is missing or inconsistent.

3. Download the exact platform asset and verify its GitHub SHA-256. If absent or mismatched in the local cache, run `quality-gate sync --source <verified-asset> --version <tag>`. Run the release asset verifier from the verified source commit to extract its single `quality_gate` wheel into a temporary directory under this repository's `temp/`. Resolve the Python interpreter owning the active `quality-gate` entry point. Install the verified wheel with `<entry-point-python> -m pip install --no-deps --upgrade <verified-wheel-path>`, directing temporary data under `temp/` where supported. Stop if the asset, wheel, or interpreter identity cannot be established. Confirm `quality-gate version` and the installed module belong to that interpreter and match the target release. Remove temporary files created for activation.

4. Report the release and source identities, API retrieval time, asset digest, and local activation results. Do not alter consumer repositories, publish, commit, or push. Repository rollout and its verification belong to the rollout PR.
