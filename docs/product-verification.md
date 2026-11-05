# Read-only product verification

This contract identifies one pull request head and the product or service checks required for it.
It does not authorize publication. The caller owns each build or checks-only job. The shared CI
aggregate must verify the job records and uploaded evidence before it reports full-profile success.
All jobs use read-only permissions, no publication secrets and no publisher or image-writer job.
Fork PRs use the same rules. If a fork lacks a private dependency, its required result is incomplete.

## Request and result, schema 1

The request is a JSON object containing exactly `schema`, `repository`, `base_sha`, `head_sha` and
`obligations`. The repository is `owner/name`; the SHAs are full lowercase Git commit IDs. The head
is the PR head, not GitHub's synthetic merge-test commit. Each obligation has `producer`, `platform`
and `kind` (`artifact` or `checks-only`). Producer and platform are bounded flat identifiers. List
one obligation per required producer and platform, at least one and at most 64. The consumer owns
this list; a provider invocation cannot infer that an omitted platform was intended. For this
repository the obligations are `package/linux` and `package/windows`, both `artifact`.

Each `verification.json` contains `schema`, `repository`, `base_sha`, `head_sha`, `producer`,
`platform`, `kind`, `status`, `applicability`, `artifact` and `evidence`. Success requires
`status: passed` and `applicability: applicable`. An artifact result contains its flat `name`,
SHA-256 `sha256`, and exact `source_sha`. A checks-only result has `artifact: null`; it cannot
satisfy an artifact obligation. Evidence is `run/<run-id>/attempt/<attempt>`. A failed, skipped,
unsupported or missing producer does not produce a successful aggregate. Duplicate identities,
unknown fields, conflicting results and a different source or base fail closed. The result and
request JSON files are limited to 64 KiB each; there are at most 64 results, with at most one
512 MiB artifact per result.

`quality_gate.product_verification.aggregate` validates the request and result identities.
`aggregate_directory` also hashes the downloaded artifact files. Both return `content-verified`,
not a complete CI verdict. Its input is one directory per
producer, each containing only `verification.json` and, for an artifact producer, its declared
file. These functions validate content; neither authenticates the origin of its input.
`verify_transport` separately returns `transport-verified` after checking the GitHub run, latest attempt, producer job, required steps,
artifact metadata, creation time and upload-log receipt. The shared workflow must obtain these
records with its read-only Actions token, select the exact run and attempt, and reject incomplete or
ambiguous API lists. It must download each artifact by its verified ID, validate GitHub's ZIP digest
before extraction, and pass only those extracted files to `aggregate_directory`. It must run both
content and transport validation on the same result records before reporting success. It must also
include the separate full audit and release-readiness obligations in the full-profile aggregate.
A caller must never substitute untrusted
JSON for GitHub API or job-log records. The proof requires job name `Verify product <producer>
<platform>`, successful `Verify product` and `Upload verification evidence` steps, and artifact
name `product-verification-<producer>-<platform>-<attempt>`. The artifact upload receipt binds its
GitHub ID and ZIP SHA-256 to the job. The per-file digest binds the delivered product bytes. A failed
or pending newer attempt blocks a prior success; this contract does not reuse earlier attempts.

The consumer's caller schedules each required product or checks-only job on its own repository and
runs its own adapter there. Its aggregate job depends directly on all required jobs and runs even
when one fails or is skipped, so it can report an incomplete result. The shared workflow checks the
current PR base and head again before reporting success. It must not import commands from the
quality manifest or run PR product code in a privileged job. Checks-only consumers retain their
application/service checks without publishing machinery; each must name its own applicable
producer and platform and emit an explicit checks-only result. An unsupported producer remains
incomplete. Exact run and artifact evidence is not a publication receipt or authorization envelope.

## Quality Gate adapter

On each platform, check out `head_sha` with credentials disabled, then run
`python scripts/release_build.py --pull-request` with `SOURCE_SHA`, `BASE_SHA`, `RELEASE_PLATFORM`,
`GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT` and `RUNNER_TEMP` set from the trusted
workflow context. The adapter rejects a dirty checkout and its build checks the exact Git HEAD. It
builds the actual wheel and platform archive, validates the inventory and hashes, installs in an
isolated runtime, runs setup and audit, and runs the complete repository tests against the candidate
policy. On success it writes `verification.json` and the ZIP under
`$RUNNER_TEMP/quality-gate-release/<platform>/`. Upload only those two files. It does not read a
publication candidate or obtain write credentials.

The Linux and Windows runs exercise different native binaries, wheels and installation paths.
The isolated audit validates the installed product; the repository suite exercises source and
candidate-policy compatibility. These runs have distinct purposes and neither replaces the other.
Release notes and required release documents remain release-readiness checks in
`verify_release_candidate`; `verify_product_candidate` omits those source preconditions. The installed
product audit checks lessons without requiring completion and scans all reachable Git history. The public audit still requires completed lessons. Version and
manifest consistency, archive inventory, digests, isolated installation and audit remain product
checks. Post-merge preparation, original authorization envelope, provenance, publication permissions,
receipts and recovery remain under [the publication contract](publication.md).
