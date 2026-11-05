"""Publisher images operations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from .publication import PublicationError, image_repository
from .publication_artifacts import json_record
from .publication_config import strings
from .publication_evidence import digest, positive, record, records, verify_run
from .publication_flow_context import _PublisherContext
from .publication_flow_support import (
	RECEIPT_MARKER,
	_unique_receipt_image,
	_VerifiedImage,
	canonical,
	sha,
)


class _Images(_PublisherContext):
	@staticmethod
	def _image_archive(item: Mapping[str, object], files: Mapping[str, bytes]) -> tuple[str, str]:
		name = item.get("archive")
		archive_sha256 = item.get("archive_sha256")
		if (
			item.get("kind") != "image"
			or not isinstance(name, str)
			or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
			or name not in files
			or not isinstance(archive_sha256, str)
			or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
			or hashlib.sha256(files[name]).hexdigest() != archive_sha256
		):
			raise PublicationError("image evidence does not bind a valid data-only archive")
		return name, archive_sha256

	@staticmethod
	def _writer_image(
		receipt: Mapping[str, object],
		*,
		producer: str,
		name: str,
		archive_name: str,
		archive_sha256: str,
		producer_proof: Mapping[str, object],
	) -> dict[str, object]:
		images = records(receipt.get("images"), "writer receipt images")
		image = _unique_receipt_image(
			images, name, "writer receipt is missing or duplicates a required image"
		)
		proof = record(image.get("producer_proof"), "writer producer proof")
		if (
			image.get("producer") != producer
			or image.get("archive_name") != archive_name
			or image.get("archive_sha256") != archive_sha256
			or any(
				proof.get(key) != producer_proof.get(source_key)
				for key, source_key in (
					("artifact_id", "artifact_id"),
					("artifact_digest", "digest"),
					("job_id", "job_id"),
					("attempt", "artifact_attempt"),
				)
			)
		):
			raise PublicationError("writer receipt is not bound to the verified image producer")
		return image

	def _image_receipt(
		self, candidate: Mapping[str, object], candidate_artifact_id: int
	) -> tuple[dict[str, object], dict[str, object]]:
		context = self.context
		config = record(candidate.get("config"), "candidate configuration")
		writer_name = "Write image / Push images"
		checks = ["Write images"]
		latest_job, latest_attempt = self._latest_producer_job(writer_name, checks)
		available = self._producer_artifacts("shared image writer", "release-image-receipt-")
		attempt, artifact_item, job, latest_attempt = self._select_producer_artifact(
			"shared image writer",
			"release-image-receipt-",
			writer_name,
			checks,
			latest_job,
			latest_attempt,
			available,
		)
		artifact, files = self.artifacts.read(
			positive(artifact_item.get("id"), "writer receipt ID")
		)
		if set(files) != {"image-receipt.json"}:
			raise PublicationError(
				"image writer receipt artifact must contain only image-receipt.json"
			)
		receipt = json_record(files["image-receipt.json"])
		proof = self.artifacts.prove(
			artifact,
			job=job,
			repository=context.repository,
			run_id=context.run_id,
			run_head_sha=context.run_head_sha,
			current_attempt=context.attempt,
			provider_repository=context.provider_repository,
			provider_sha=context.provider_sha,
			workflow=str(config.get("workflow")),
			name=f"release-image-receipt-{attempt}",
			checks=checks,
			helper_workflow="image-writer.yml",
		)
		if (
			receipt.get("schema") != 1
			or receipt.get("candidate_sha256") != hashlib.sha256(canonical(candidate)).hexdigest()
			or receipt.get("candidate_artifact_id") != candidate_artifact_id
			or receipt.get("repository") != context.repository
			or receipt.get("pr") != candidate.get("pr")
			or receipt.get("source_sha") != candidate.get("source_sha")
			or receipt.get("version") != candidate.get("version")
			or receipt.get("provider_repository") != context.provider_repository
			or receipt.get("provider_sha") != context.provider_sha
			or receipt.get("candidate_provider_repository") != candidate.get("provider_repository")
			or receipt.get("candidate_provider_sha") != candidate.get("provider_sha")
			or receipt.get("writer_run_id") != context.run_id
			or receipt.get("writer_attempt") != attempt
		):
			raise PublicationError("image writer receipt does not identify this candidate and run")
		return receipt, {
			"artifact_id": proof["artifact_id"],
			"artifact_digest": proof["digest"],
			"job_id": proof["job_id"],
			"attempt": proof["attempt"],
			"latest_attempt": latest_attempt,
		}

	def write_images(
		self, number: int, candidate_artifact_id: int, output: Path
	) -> dict[str, object]:
		"""Verify candidate-bound Docker-save archives, push them, and retain a writer receipt."""
		self._recovery_context()
		candidate = self._candidate(
			number, positive(candidate_artifact_id, "candidate artifact ID")
		)
		pull = self._pull(number, merged=True)
		version, config, _projections = self._metadata(sha(candidate["source_sha"]))
		if version != candidate.get("version"):
			raise PublicationError("writer candidate version differs from exact source")
		self._verification_context(candidate)
		workflow_run = self.artifacts.run(
			self.context.run_id, expected_attempt=self.context.attempt
		)
		verify_run(
			workflow_run,
			repository=self.context.repository,
			run_id=self.context.run_id,
			run_head_sha=self.context.run_head_sha,
			provider_repository=self.context.provider_repository,
			provider_sha=self.context.provider_sha,
			workflow=str(config.get("workflow")),
			helper_workflow="image-writer.yml",
		)
		producer_configs = records(config.get("producers"), "required producers")
		images = [producer for producer in producer_configs if producer.get("kind") == "image"]
		if not images:
			raise PublicationError("image writer was called for a candidate without image outputs")
		verified: list[_VerifiedImage] = []
		for producer in images:
			evidence, files, producer_proof = self._producer_bundle(candidate, producer)
			required = strings(producer.get("deliverables"), "required image")
			items = records(evidence.get("deliverables"), "image deliverables")
			if (
				len(required) != 1
				or len(items) != 1
				or items[0].get("kind") != "image"
				or items[0].get("name") != required[0].replace("{version}", version)
			):
				raise PublicationError(
					"image producer evidence does not match source configuration"
				)
			item = items[0]
			archive_name, archive_sha256 = self._image_archive(item, files)
			verified.append(
				_VerifiedImage(
					producer,
					item,
					archive_name,
					archive_sha256,
					files[archive_name],
					producer_proof,
				)
			)
		prior_digests = self._prior_image_digests(version, candidate, verified)
		existing_source = self._tag(f"v{version}")
		if existing_source is not None and existing_source != sha(candidate["source_sha"]):
			raise PublicationError("version tag identifies a different source")
		for image in verified:
			self.api.preflight_image_archive(
				image.archive_bytes,
				sha(candidate["source_sha"]),
				image_repository(image.producer.get("image_repository")),
				version,
				str(image.producer["name"]),
				expected_digest=prior_digests.get(str(image.item["name"])),
			)
		writer_images: list[dict[str, object]] = []
		for image in verified:
			package = image_repository(image.producer.get("image_repository"))
			published = self.api.publish_image_archive(
				image.archive_bytes,
				sha(candidate["source_sha"]),
				package,
				version,
				str(image.producer["name"]),
				expected_digest=prior_digests.get(str(image.item["name"])),
			)
			digest_value = digest("sha256:" + str(published.get("sha256")))
			reference = str(published.get("reference"))
			if reference != f"{package}@sha256:{digest_value}":
				raise PublicationError("image writer returned a mutable or substituted reference")
			writer_images.append(
				{
					"producer": image.producer["name"],
					"name": image.item["name"],
					"image_repository": package,
					"archive_name": image.archive_name,
					"archive_sha256": image.archive_sha256,
					"reference": reference,
					"sha256": digest_value,
					"tag": published.get("tag"),
					"producer_proof": {
						"artifact_id": image.producer_proof["artifact_id"],
						"artifact_digest": image.producer_proof["digest"],
						"job_id": image.producer_proof["job_id"],
						"attempt": image.producer_proof["artifact_attempt"],
					},
				}
			)
		receipt = {
			"schema": 1,
			"candidate_sha256": hashlib.sha256(canonical(candidate)).hexdigest(),
			"candidate_artifact_id": candidate_artifact_id,
			"repository": self.context.repository,
			"pr": pull["number"],
			"source_sha": candidate["source_sha"],
			"version": version,
			"provider_repository": self.context.provider_repository,
			"provider_sha": self.context.provider_sha,
			"candidate_provider_repository": candidate["provider_repository"],
			"candidate_provider_sha": candidate["provider_sha"],
			"writer_run_id": self.context.run_id,
			"writer_attempt": self.context.attempt,
			"images": writer_images,
		}
		output_path = Path(output)
		output_path.mkdir(parents=True, exist_ok=True)
		temporary = output_path / "image-receipt.json.tmp"
		temporary.write_bytes(canonical(receipt))
		temporary.replace(output_path / "image-receipt.json")
		return {"status": "images-written", "images": writer_images}

	def _prior_image_digests(
		self,
		version: str,
		candidate: Mapping[str, object],
		verified: list[_VerifiedImage],
	) -> dict[str, str]:
		"""Require any retained draft to identify this exact candidate and archive."""
		release = self._existing(version)
		if release is None:
			return {}
		if release.get("draft") is not True:
			raise PublicationError("an existing published release blocks image-tag writes")
		body = str(release.get("body", ""))
		if body.count(RECEIPT_MARKER) != 1 or not body.endswith("-->\n"):
			raise PublicationError("existing draft lacks its retained candidate receipt")
		receipt = json_record(body.split(RECEIPT_MARKER)[1].removesuffix("-->\n").encode("utf-8"))
		if receipt.get("candidate") != candidate:
			raise PublicationError("existing draft identifies a different retained candidate")
		self._readback(release, candidate, receipt, draft=True)
		verification = record(receipt.get("verification"), "existing draft verification")
		image_writer = record(verification.get("image_writer"), "existing draft image writer")
		writer_receipt = record(image_writer.get("receipt"), "existing draft image receipt")
		previous_images = records(writer_receipt.get("images"), "existing draft images")
		result: dict[str, str] = {}
		for image in verified:
			previous = _unique_receipt_image(
				previous_images,
				image.item.get("name"),
				"existing draft image identity is missing or duplicated",
			)
			package = image_repository(image.producer.get("image_repository"))
			if (
				previous.get("producer") != image.producer.get("name")
				or previous.get("image_repository") != package
				or previous.get("archive_sha256") != image.archive_sha256
				or previous.get("tag") != f"{package}:v{version}"
			):
				raise PublicationError("existing draft image conflicts with the current archive")
			result[str(image.item["name"])] = digest("sha256:" + str(previous.get("sha256")))
		return result

	def _verify_deliverable(
		self,
		item: Mapping[str, object],
		files: Mapping[str, bytes],
		*,
		expected_image_repository: str | None = None,
	) -> None:
		name = str(item.get("name"))
		if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None:
			raise PublicationError("unsafe deliverable name")
		expected = digest("sha256:" + str(item.get("sha256")))
		if item.get("kind") == "image":
			reference = str(item.get("reference"))
			if expected_image_repository is None:
				raise PublicationError("image repository is not configured by the verified source")
			package = image_repository(expected_image_repository)
			if (
				item.get("image_repository") != package
				or reference != f"{package}@sha256:{expected}"
			):
				raise PublicationError(
					"image repository or reference differs from source configuration"
				)
			if self.api.image_digest(reference) != expected:
				raise PublicationError("image reference is not resolvable at its verified digest")
		elif (
			item.get("kind") not in {"archive", "package"}
			or name not in files
			or hashlib.sha256(files[name]).hexdigest() != expected
		):
			raise PublicationError("deliverable content or kind does not match verified evidence")
