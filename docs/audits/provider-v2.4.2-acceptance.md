# Provider v2.4.2 acceptance and self-policy activation

Paths are relative to the Quality Gate repository root. The system-level qualification and
rollout records are in the Entry-point repository under `.scratch/release-system/`, especially
`tickets/04-release-tg.md` and `tickets/10-rollout-dependabot-review.md`.

## Accepted immutable handoff

Reuse published policy **v2.4.2** and external workflow/helper revision **R**
`56d6d9a6252eb3f8edcfe2d5f21c3d9e6bbbae6b`. The
[published release](https://github.com/TheTraitor0FJesus/quality-gate/releases/tag/v2.4.2)
is immutable, non-draft and non-prerelease. Its tag points directly to R, the final source of
[PR 45](https://github.com/TheTraitor0FJesus/quality-gate/pull/45).

The retained receipt binds source/helper R, version 2.4.2, PR 45 and
[release run 37329744498](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/37329744498),
attempt 1. Source notes have SHA-256
`410e1c9f4137ce5508b833d231ecd90b8f2fde6df65d0b36e2dff53fca4e30cb`.
The event head `42d8a6814f3730d3407396cdb58f146f557e0e19` is separate from the released source R.

| Platform ZIP | Bytes | Receipt and GitHub asset SHA-256 |
| --- | ---: | --- |
| `quality-gate-v2.4.2-Linux.zip` | 54,521,044 | `a81618e7db9e968de21476f5357e215f4329ee468e513eebdfa6aed12024a528` |
| `quality-gate-v2.4.2-Windows.zip` | 50,542,561 | `89c8bd9d36dbc8cc0fa4c1ad9ff89035124a56e87ad5b3a7d4f7f434fc1b20e3` |

Both exact-source platform jobs passed `Verify package` and `Upload release evidence`;
the publisher passed. The receipt identifies Linux job `111829673079`, evidence artifact
`11353831873`, and Windows job `111829673180`, artifact `11353832303`.
The accepted package/readback qualification is retained in Entry-point ticket 04. The
2026-10-07 activation readback reconfirmed the live tag, receipt, source notes, asset metadata,
and required producer steps; it did not repeat Linux ZIP-byte qualification. Windows immutable
sync, setup, doctor and validate succeeded and the native launcher reported engine 2.4.2.

## Bounded activation and affected reruns

The separate No-release activation advances the provider root manifest, parity fixture and
existing pin assertions from v2.3.0 to v2.4.2. Product version/projections, packaged code,
published assets and consumer helper R remain unchanged. Same-repository workflow calls remain
self-hosted. The sole non-gating parity waiver, comparator and expected outcomes are preserved.

The approved reviewer workflow has Git blob
`1f9ed9df6e50c52ca0501ea9f6da74613681bb9e` and normalized SHA-256
`8e869293b8fd72c51351d6960e6f6e98f5b75e2bcf47e4feb477b4076450f654`.
It permits only metadata-based owner review with job-scoped `pull-requests: write` and no PR
checkout or execution. Existing policy tests reject changes to its security-sensitive template.
The added shell test executes the actual workflow with GitHub CLI as the controlled external
boundary: missing owner requests review, exact existing owner skips it, a login prefix does
not suppress it, a read error prevents writing, and a write error remains visible.

Typed regression `ticket10-provider-policy-red-20261007-01` failed both updated pin assertions
on the old v2.3.0 selection. Corrected pre-audit tree
`25064fb3212854c561cb591b191fac9542e07fc5` passed current-source full run
`ticket10-provider-source-full-20261007-04`, exit 0, 140,900 ms, no timeout or blockers.
That run covered tests, types, lint/format, workflows, documentation and candidate secrets.
Adding this audit changes the index: the complete documentation-inclusive candidate must pass
a fresh current-source full run, both review axes and the native released-policy commit hook
before PR publication. The historical pre-audit result is not its final whole-candidate evidence.

## Consumer disposition and remaining acceptance

All five consumers select policy v2.4.2 and external helper R. Their publication identities stay
owned by their product tickets; current CI activation does not rewrite historical receipts.

| Consumer | Integrated default source on 2026-10-07 | Reviewer disposition |
| --- | --- | --- |
| ARGUS_TG | `492bb09a6307c7554f78aed0c8e1e1fab2dac879` | [PR 166](https://github.com/TheTraitor0FJesus/ARGUS_TG/pull/166) awaits owner integration and green required CI. Its existing lock fails pip-audit for multidict 6.7.1 / CVE-2026-104874; dependency remediation is separately scoped. |
| ARGUS_tracker_multiuser | `206fa2633c4b3db992222978f76bcdd10948b391` | Approved reviewer workflow is integrated; required main checks passed. Recorded maintainer-PR reviewer runs skipped and do not prove a bot API mutation. |
| ARGUS_TRADING | `abc1fb39729af713acac4d9e7b43c3e57fa15e6b` | Approved reviewer workflow is integrated; Python/shared and supplemental main checks passed. Published v1.0.1 and its original v2.3.0 receipt remain preserved. |
| ARGUS_WEB | `a57635ea7e40b76270db6654d5e2c2f42f0cc265` | [PR 143](https://github.com/TheTraitor0FJesus/ARGUS_WEB/pull/143) is owner-merged; exact reviewed tree and final-main gate passed, release preparation returned no-release. |
| ARGUS_BRAIN | `0930594516f7b859279c392a72b89e93603391c8` | [PR 22](https://github.com/TheTraitor0FJesus/ARGUS_BRAIN/pull/22) is owner-merged; exact reviewed tree, shared gate, both service health/cleanup checks and aggregate passed. Checks-only delivery remains. |

The provider activation requires its own owner merge and final-main checks. No eligible open
Dependabot PR was available across the six repositories at this readback. Each live reviewer
row therefore remains pending a real eligible event, successful reviewer job and API-visible
owner review request; local policy/shell checks are not job-token/API-write proof. Superseded Web
133/134/135 and Brain 18/19/20 are closed through their owning completed replacements. Preserve
completed product releases and keep the system ticket unfinished while these acceptance rows remain open.
