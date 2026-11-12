"""Related behavior tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from publication_flow_fixture import BASE, NOTES, ORIGINAL_RUN, PR_HEAD, PROVIDER, SOURCE, GitHub
from test_publication_flow import (
	REPAIRED_PROVIDER,
	REPAIRED_SOURCE,
	BaselineGitHub,
	MultiImageRegistryGitHub,
	PublicationFailureGitHub,
	RegistryGitHub,
	RepairedGitHub,
	TagForbiddenRegistryGitHub,
	_event,
	_publisher,
	_ready,
)

from quality_gate.publication import PublicationError
from quality_gate.publication_flow import RECEIPT_MARKER, Context, Publisher


def test_historical_403_reuses_original_image_writer_on_publisher_only_rerun(
	tmp_path: Path,
) -> None:
	api = TagForbiddenRegistryGitHub()
	publisher, candidate_artifact_id = _ready(api)
	assert publisher.write_images(7, candidate_artifact_id, tmp_path)["status"] == "images-written"
	api.artifact(
		"release-image-receipt-1",
		{"image-receipt.json": (tmp_path / "image-receipt.json").read_bytes()},
		"Write image / Push images",
		["Write images"],
	)

	with pytest.raises(PublicationError, match="HTTP 403"):
		publisher.publish(7, candidate_artifact_id)
	assert "v2.1.0" not in api.tags

	api.tags["v2.1.0"] = SOURCE
	api.deny_tag_creation = False
	api.run_overrides["run_attempt"] = 2
	rerun = _publisher(api, attempt=2, evidence_wait_seconds=0.05, evidence_poll_seconds=0.001)

	assert rerun.publish(7, candidate_artifact_id)["status"] == "published"
	assert api.tags["v2.1.0"] == SOURCE
	assert api.writes.count("/git/refs") == 1
	assert len(api.image_preflights) == len(api.image_pushes) == 1
	receipt_json = str(api.releases[-1]["body"]).split(RECEIPT_MARKER, 1)[1].removesuffix("-->\n")
	receipt = json.loads(receipt_json)
	verification = receipt["verification"]
	assert verification["run_id"] == ORIGINAL_RUN
	assert verification["image_writer"]["receipt"]["writer_run_id"] == ORIGINAL_RUN
	assert verification["image_writer"]["receipt"]["writer_attempt"] == 1
	assert verification["image_writer"]["artifact_proof"]["attempt"] == 1
	assert verification["producers"][0]["artifact_attempt"] == 1


def test_shared_protocol_publishes_and_reads_back_package_delivery() -> None:
	api = RegistryGitHub("package")
	publisher, artifact_id = _ready(api)
	assert publisher.publish(7, artifact_id)["status"] == "published"
	assert len(api.uploads) == 1
	before = copy.deepcopy(api.writes)
	assert publisher.prepare(7, event=_event(api))["status"] == "already-complete"
	assert api.writes == before


def test_writer_receipt_reference_must_use_source_configured_package(tmp_path: Path) -> None:
	api = RegistryGitHub("image")
	publisher, artifact_id = _ready(api)
	assert publisher.write_images(7, artifact_id, tmp_path)["status"] == "images-written"
	image_receipt = json.loads((tmp_path / "image-receipt.json").read_bytes())
	image = image_receipt["images"][0]
	assert image["image_repository"] == "ghcr.io/o/product"
	assert image["tag"] == "ghcr.io/o/product:v2.1.0"
	image["reference"] = f"ghcr.io/o/other@sha256:{image['sha256']}"
	api.artifact(
		"release-image-receipt-1",
		{"image-receipt.json": json.dumps(image_receipt).encode()},
		"Write image / Push images",
		["Write images"],
	)

	with pytest.raises(PublicationError, match="image repository or reference differs"):
		publisher.publish(7, artifact_id)
	assert api.writes == []


def test_completed_readback_binds_image_reference_to_source_package(tmp_path: Path) -> None:
	api = RegistryGitHub("image")
	publisher, artifact_id = _ready(api)
	assert publisher.write_images(7, artifact_id, tmp_path)["status"] == "images-written"
	api.artifact(
		"release-image-receipt-1",
		{"image-receipt.json": (tmp_path / "image-receipt.json").read_bytes()},
		"Write image / Push images",
		["Write images"],
	)
	assert publisher.publish(7, artifact_id)["status"] == "published"
	release = api.releases[-1]
	body = str(release["body"])
	prefix, marker, encoded = body.partition(RECEIPT_MARKER)
	assert marker and encoded.endswith("-->\n")
	receipt = json.loads(encoded.removesuffix("-->\n"))
	item = receipt["deliverables"][0]
	writer_image = receipt["verification"]["image_writer"]["receipt"]["images"][0]
	assert item["image_repository"] == writer_image["image_repository"] == "ghcr.io/o/product"
	assert writer_image["tag"] == "ghcr.io/o/product:v2.1.0"
	foreign_reference = f"ghcr.io/o/other@sha256:{item['sha256']}"
	item["reference"] = foreign_reference
	writer_image["reference"] = foreign_reference
	release["body"] = (
		prefix
		+ marker
		+ json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		+ "\n-->\n"
	)
	previous_writes = copy.deepcopy(api.writes)

	with pytest.raises(PublicationError, match="image repository or reference differs"):
		publisher.prepare(7, event=_event(api))
	assert api.writes == previous_writes


@pytest.mark.parametrize(
	("field", "value"),
	[
		("provider_sha", "f" * 40),
		("provider_repository", "o/other-provider"),
		("image_producer", "substituted"),
		("image_producer_proof_artifact_id", 999),
	],
)
def test_completed_readback_rejects_unbound_image_writer_provenance(
	field: str, value: object, tmp_path: Path
) -> None:
	api = RegistryGitHub("image")
	publisher, artifact_id = _ready(api)
	assert publisher.write_images(7, artifact_id, tmp_path)["status"] == "images-written"
	api.artifact(
		"release-image-receipt-1",
		{"image-receipt.json": (tmp_path / "image-receipt.json").read_bytes()},
		"Write image / Push images",
		["Write images"],
	)
	assert publisher.publish(7, artifact_id)["status"] == "published"
	release = api.releases[-1]
	prefix, marker, encoded = str(release["body"]).partition(RECEIPT_MARKER)
	assert marker and encoded.endswith("-->\n")
	receipt = json.loads(encoded.removesuffix("-->\n"))
	writer = receipt["verification"]["image_writer"]["receipt"]
	if field == "image_producer":
		writer["images"][0]["producer"] = value
	elif field == "image_producer_proof_artifact_id":
		writer["images"][0]["producer_proof"]["artifact_id"] = value
	else:
		writer[field] = value
	release["body"] = (
		prefix
		+ marker
		+ json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		+ "\n-->\n"
	)
	previous_writes = copy.deepcopy(api.writes)

	with pytest.raises(PublicationError):
		publisher.prepare(7, event=_event(api))
	assert api.writes == previous_writes


def test_source_archive_contract_rejects_image_evidence_before_release_writes() -> None:
	api = RegistryGitHub("image")
	api.files[(SOURCE, ".release/publisher.toml")] = api.files[
		(SOURCE, ".release/publisher.toml")
	].replace('kind = "image"\nimage_repository = "ghcr.io/o/product"', 'kind = "archive"')
	publisher, artifact_id = _ready(api)
	with pytest.raises(PublicationError, match="SHA-256 digest"):
		publisher.publish(7, artifact_id)
	assert api.writes == []


def test_conflicting_retained_draft_blocks_image_tag_write(tmp_path: Path) -> None:
	api = RegistryGitHub("image")
	publisher, artifact_id = _ready(api)
	api.releases.append(
		{
			"id": 2,
			"tag_name": "v2.1.0",
			"draft": True,
			"prerelease": False,
			"target_commitish": SOURCE,
			"body": NOTES
			+ RECEIPT_MARKER
			+ json.dumps({"candidate": {"source_sha": "f" * 40}})
			+ "-->\n",
			"assets": [],
		}
	)

	with pytest.raises(PublicationError, match="different retained candidate"):
		publisher.write_images(7, artifact_id, tmp_path)

	assert api.image_pushes == []
	assert api.writes == []


def test_conflicting_version_tag_blocks_image_tag_write(tmp_path: Path) -> None:
	api = RegistryGitHub("image")
	publisher, artifact_id = _ready(api)
	api.tags["v2.1.0"] = BASE

	with pytest.raises(PublicationError, match="version tag identifies a different source"):
		publisher.write_images(7, artifact_id, tmp_path)

	assert api.image_pushes == []
	assert api.writes == []


def test_second_image_tag_conflict_is_found_before_any_image_push(tmp_path: Path) -> None:
	api = MultiImageRegistryGitHub()
	publisher, artifact_id = _ready(api)
	api.conflicting_image_repository = "ghcr.io/o/second"

	with pytest.raises(PublicationError, match="different image bytes"):
		publisher.write_images(7, artifact_id, tmp_path)

	assert api.image_preflights == ["ghcr.io/o/first", "ghcr.io/o/second"]
	assert api.image_pushes == []
	assert api.writes == []


@pytest.mark.parametrize(
	("field", "value"),
	[
		("candidate_sha256", "f" * 64),
		("candidate_artifact_id", 999),
		("source_sha", "f" * 40),
		("provider_sha", "f" * 40),
		("writer_run_id", 101),
		("image_producer", "substituted"),
		("image_producer_proof_artifact_id", 999),
		("image_archive_sha256", "f" * 64),
		("image_registry_digest", "f" * 64),
	],
)
def test_image_receipt_conflicts_fail_before_github_release_mutations(
	field: str, value: object, tmp_path: Path
) -> None:
	api = RegistryGitHub("image")
	publisher, artifact_id = _ready(api)
	assert publisher.write_images(7, artifact_id, tmp_path)["status"] == "images-written"
	receipt = json.loads((tmp_path / "image-receipt.json").read_bytes())
	image = receipt["images"][0]
	if field == "image_registry_digest":
		image["sha256"] = value
		image["reference"] = f"ghcr.io/o/product@sha256:{value}"
	elif field == "image_producer_proof_artifact_id":
		image["producer_proof"]["artifact_id"] = value
	elif field.startswith("image_"):
		image[field.removeprefix("image_")] = value
	else:
		receipt[field] = value
	api.artifact(
		"release-image-receipt-1",
		{"image-receipt.json": json.dumps(receipt).encode()},
		"Write image / Push images",
		["Write images"],
	)

	with pytest.raises(PublicationError):
		publisher.publish(7, artifact_id)
	assert api.writes == []


def test_repaired_provider_verifies_original_source_in_a_new_run() -> None:
	api = RepairedGitHub()
	original = _publisher(api).prepare(7, event=_event(api))["candidate"]
	assert isinstance(original, dict)
	artifact_id = api.persist(original)
	publisher = Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=REPAIRED_PROVIDER,
			helper_sha=REPAIRED_PROVIDER,
			run_id=101,
			attempt=1,
			run_source_sha=REPAIRED_SOURCE,
			run_head_sha=REPAIRED_SOURCE,
		),
	)
	assert publisher.recover(7)["candidate"] == original
	api.build(original)
	assert publisher.publish(7, artifact_id)["source_sha"] == SOURCE
	assert api.tags["v2.1.0"] == SOURCE
	assert original["provider_sha"] == PROVIDER
	assert REPAIRED_PROVIDER in str(api.releases[-1]["body"])


def test_unrelated_pull_request_run_cannot_publish_an_old_candidate() -> None:
	api = GitHub()
	_, artifact_id = _ready(api)
	publisher = Publisher(
		api,
		Context(
			repository="o/r",
			provider_repository="o/provider",
			provider_sha=PROVIDER,
			helper_sha=PROVIDER,
			run_id=100,
			attempt=1,
			run_source_sha=REPAIRED_SOURCE,
			run_head_sha=PR_HEAD,
		),
	)
	with pytest.raises(PublicationError):
		publisher.publish(7, artifact_id)
	assert api.writes == []


@pytest.mark.parametrize("failure", ["draft-source", "upload-digest", "missing-ready-asset"])
def test_failed_draft_readiness_never_promotes_a_public_release(failure: str) -> None:
	api = PublicationFailureGitHub(failure)
	publisher, artifact_id = _ready(api)
	with pytest.raises(PublicationError):
		publisher.publish(7, artifact_id)
	assert api.releases[-1]["draft"] is True
	assert "/releases/2" not in api.writes
	if failure == "draft-source":
		assert api.uploads == {}


def test_annotated_baseline_is_accepted_through_preparation() -> None:
	api = BaselineGitHub("annotated")
	assert _publisher(api).prepare(7, event=_event(api))["status"] == "build-required"
	assert api.writes == []
