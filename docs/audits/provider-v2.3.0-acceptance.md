# Provider v2.3.0 acceptance

Paths in this record are relative to the Quality Gate repository root unless another repository
root is named. Ticket 02's qualification record and ticket 03 live under
`.scratch/release-system/` in the Entry-point repository.

## Accepted immutable pair

The accepted policy is **v2.3.0** and the accepted external workflow/helper revision **R** is
`a8dd22879bf5dc63665edecba32ac1b11b7c3457`. Reuse the existing
[published release](https://github.com/TheTraitor0FJesus/quality-gate/releases/tag/v2.3.0).
No delivered shared code changed during qualification or acceptance, and no new provider release
is required for self-policy activation, the parity fixture correction, or this guidance.

| Platform asset | Bytes | SHA-256 of downloaded ZIP |
| --- | ---: | --- |
| `quality-gate-v2.3.0-Linux.zip` | 54,519,599 | `d676a1849a32925bd7ce304fd4d1770665c6f164e81da709b07bf7d3c042847e` |
| `quality-gate-v2.3.0-Windows.zip` | 50,541,085 | `e0370c4fbfb618ce6d2985687cd85a246cc2a023ae55a8388f5d28639801a806` |

On 2026-10-02, typed run `ticket03-exact-helper-publication-readback-20261002-03` passed four
acceptance checks. The verification used source exported from R, checked every exported
`quality_gate/` file against its Git blob identity, and required imports from that exact export.
The transport allowed only GitHub API GET requests; no tag, registry, or release mutation occurred.

`publication_flow.Publisher.prepare(37, ...)` returned `already-complete`. Its existing completed
readback checked the owner-merged source, declaration, version projections, source notes, complete
canonical receipt, tag target, asset names, and metadata digests. Both real ZIP downloads then
matched the receipt digests and sizes; `ci_release.verify_release_asset()` checked native release
immutability, every internal inventory/digest, safe extraction, and the 2.3.0 wheel.

## Publication provenance

The selected tag resolves to the final source of
[PR 37](https://github.com/TheTraitor0FJesus/quality-gate/pull/37), which is R. The complete receipt
remains in the published release body beside the exact source notes. Their SHA-256 is
`bed407fd644e3de3cc6b027755ae94939450edb4f8afcea8af59bcf3cc42f2ba`.

[Release run 36402345314](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/36402345314)
completed successfully on attempt 1. Its original event head is
`dcf582b9a4c323ae4054ff6d26e8705f6a541e96`, while the approved product source and executed
self-hosted preparation/publisher helper are R. These identities have separate roles; the run's
event head is not the released source. Acceptance checked both referenced reusable workflow SHAs,
the receipt's producer identities, successful required package/upload steps, and retained artifact
metadata against the live Actions API.

| Producer | Exact-source package job | Evidence artifact ID |
| --- | --- | ---: |
| Linux | [108863020531](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/36402345314/job/108863020531) | 10961022106 |
| Windows | [108863019722](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/36402345314/job/108863019722) | 10961196471 |

## Integrated activation and remaining fixture integration

The owner merged the no-release activation in
[PR 39](https://github.com/TheTraitor0FJesus/quality-gate/pull/39) at
`77b6c88b1f6e38030176cfc9c658ed87768147e4`. That source selects policy v2.3.0 and removes only
the three obsolete bootstrap workflow waivers. The non-gating parity waiver remains. Product
version, notes, projections, and published helper R are preserved.

[Merged-source Quality Gate 36902411799](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/36902411799)
passed. [Activation release run 36902412744](https://github.com/TheTraitor0FJesus/quality-gate/actions/runs/36902412744)
successfully prepared `no-release`; its package build and publisher jobs were correctly skipped.
The exact R helper independently confirmed that same no-release decision during acceptance.

The remaining ticket 03 correction sets the repository-owned parity fixture to v2.3.0 and checks
its equality with the provider/template selection. Its regression was red on v2.0.6 in
`ticket03-parity-selector-red-20261002-01` and green after correction in
`ticket03-parity-self-policy-green-20261002-04`, together with the existing activation/waiver test
(two tests). It preserves the parity implementation, comparison surface, and expected outcomes.
This correction and the guidance require owner PR integration before ticket 03 is complete;
an open PR is not that integration.

## Qualification handoff and rerun boundaries

Ticket 02 qualified the exact R/v2.3.0 pair and these intended consumer trees. Provider acceptance
does not transfer their green results to another source tree or authorize a product publication.

| Consumer | Qualified intended Git tree | Gate/runtime boundary |
| --- | --- | --- |
| TG | `e4062c9bf55ba1aa08de576809ca86447a1f249b` | Complete local gate and isolated public image build/verify/runtime; local registry, no real GHCR write. |
| Tracker | `747beafbaa12668ccc7f989f15952025a5a66569` | Complete local gate and isolated image/runtime; writer/candidate metadata synthetic. |
| Trading | `f21c88224ff7cb7b7c1f3ca3839e38a201c1e050` | Complete local gate, 19 Node tests, ZIP verification and disposable Windows runtime; candidate provenance synthetic. |
| Web | `6a597b40f12092129c7239b6e715ef1f0ebc867a` | Complete local gate, tar.gz verification and isolated Compose runtime; candidate provenance synthetic. |
| Brain | `1cd4faba89e426ce26af279b61efce24f28d345a` | Complete local gate and isolated service-health fixture; checks-only wiring, no publication. |

The provider's qualified tree is `46685721ab7862955304196056cd710122a3c26a`. It remains preserved
in ticket 02's proposal. Current activation differs only in test details and the outstanding
fixture correction; no shared helper, workflow, or packaged policy change invalidates consumers.
Q01-Q12 are recorded in Entry-point's `tickets/02-qualify-provider.md` under the release-system
specification directory. Q07's Linux/Windows parity and official comparator passed on the qualified
fixture. Windows used the unchanged `build_result()` with a short temporary root to avoid the
outer CLI wrapper's MAX_PATH cleanup failure; this limit is not a successful outer-wrapper check.

On acceptance, Tracker, Trading, Web, and Brain's staged proposals and working files still matched
their qualified trees. TG's exact tree remains in its repository object database, with a separate
retained source archive in the qualification evidence. Its recorded runtime source identity is
`da1328077593ae80cdca9093d19a481e10eaafec`; that original commit is not available in the current
TG object database, so recover the file snapshot by tree, not by assuming that commit is a branch
base. Current TG main differs from the qualified tree, including the run-specific preparation/build
concurrency correction. Ticket 04 must reconcile that correction and rerun the affected workflow,
gate, provenance, and runtime evidence against its actual final candidate.

Consumer tickets preserve their own delivery contracts and requalify any changed source, caller,
helper, package digest, or runtime input at the affected seams. TG/Tracker package Actions access
was owner-reviewed as Admin on 2026-10-01. Effective automatic-token writes, approved-source
candidate provenance, actual publication, and immutable readback A01-A04 remain mandatory for the
four real product releases. Brain requires integrated non-publishing checks. No consumer rollout
proceeds with a missing required matrix cell or stale identity.
