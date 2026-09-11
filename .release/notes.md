# Quality Gate v2.0.6

Version: 2.0.6
Impact: PATCH
Changes: owner-merged, source-bound publication of the existing Linux and Windows Quality Gate package assets.
Required adaptation: consumer policy and reusable-workflow pins remain unchanged until a consumer explicitly adopts v2.0.6.

## Interface

The `quality-gate` CLI, schema 2 manifest, native hook, and release-backed runtime keep their existing v2 compatibility boundary. The new release controller is repository-owned release setup, not a consumer CLI contract change.

## Integrations

The reusable `Quality Gate` workflow remains selected by an immutable commit SHA. Post-merge publication consumes the existing owner-merged pull request and required `Quality Gate` check; it does not change consumer workflow triggers.

## Configuration

`.release/version.toml` is the only product-version authority. `pyproject.toml`, runtime `quality_gate.__version__`, `quality-gate.toml`, the template manifest, and the distribution inventory are validated projections.

## Persisted data

N/A — Quality Gate does not own application or runtime data. Its local policy cache and release retention behavior remain under the existing distribution contract.

## Delivery/runtime

The existing exact Linux and Windows ZIP assets remain the delivery format. Each asset is built from the merged source SHA, installed into an isolated disposable runtime, audited, inventory-checked, checksum-checked, and published immutably without changing deployment, rollback, or backup ownership.
