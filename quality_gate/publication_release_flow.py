"""Publisher releaseflow operations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .publication import PublicationError, image_repository, prepare
from .publication_artifacts import json_record
from .publication_config import strings, validate_identities
from .publication_evidence import digest, positive, record, records
from .publication_flow_context import _PublisherContext
from .publication_flow_support import (
	RECEIPT_MARKER,
	_authority,
	_unique_receipt_image,
	canonical,
	sha,
)


class _ReleaseFlow(_PublisherContext):
	def _body(self, candidate: Mapping[str, object], receipt: Mapping[str, object]) -> str:
		return (
			str(candidate["notes"]) + RECEIPT_MARKER + canonical(receipt).decode("utf-8") + "-->\n"
		)

	def _readback(
		self,
		release: Mapping[str, object],
		candidate: Mapping[str, object],
		receipt: Mapping[str, object],
		*,
		draft: bool = False,
	) -> set[str]:
		tag_source = self._tag(f"v{candidate['version']}")
		if (
			release.get("tag_name") != f"v{candidate['version']}"
			or release.get("draft") is not draft
			or release.get("prerelease") is not False
			or (tag_source != candidate["source_sha"] and not (draft and tag_source is None))
			or release.get("body") != self._body(candidate, receipt)
			or (draft and release.get("target_commitish") != candidate["source_sha"])
		):
			raise PublicationError(
				"publication source, tag, notes or state conflicts with candidate"
			)
		items = records(receipt.get("deliverables"), "receipt deliverables")
		validate_identities(candidate, items)
		self._validate_image_writer_receipt(candidate, items, receipt)
		config = record(candidate.get("config"), "candidate configuration")
		image_repositories: dict[str, str] = {}
		for producer in records(config.get("producers"), "required producers"):
			if producer.get("kind") != "image":
				continue
			package = image_repository(producer.get("image_repository"))
			for template in strings(producer.get("deliverables"), "required deliverables"):
				name = template.replace("{version}", str(candidate["version"]))
				image_repositories[name] = package
		expected = {
			str(item["name"]): str(item["sha256"]) for item in items if item.get("kind") != "image"
		}
		actual: dict[str, str] = {}
		for asset in records(release.get("assets"), "release assets"):
			name = str(asset.get("name"))
			if name in actual or expected.get(name) != digest(asset.get("digest")):
				raise PublicationError("existing release contains a conflicting asset")
			actual[name] = digest(asset.get("digest"))
		if not draft and actual != expected:
			raise PublicationError("completed publication is missing required assets")
		for item in items:
			if item.get("kind") == "image":
				self._verify_deliverable(
					item,
					{},
					expected_image_repository=image_repositories.get(str(item.get("name"))),
				)
		return set(expected) - set(actual)

	@staticmethod
	def _image_producer_proofs(
		candidate: Mapping[str, object], verification: Mapping[str, object]
	) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
		config = record(candidate.get("config"), "candidate configuration")
		producers = records(config.get("producers"), "required producers")
		producer_proofs = records(verification.get("producers"), "retained producer proofs")
		if len(producers) != len(producer_proofs):
			raise PublicationError("retained producer proofs do not match source configuration")
		proof_by_name: dict[str, dict[str, object]] = {}
		image_producer_by_deliverable: dict[str, str] = {}
		# Proofs are emitted in source configuration order; new receipts also name each proof.
		for producer, proof in zip(producers, producer_proofs, strict=True):
			name = producer.get("name")
			if not isinstance(name, str) or name in proof_by_name:
				raise PublicationError("retained producer proof identity is ambiguous")
			if proof.get("producer") not in {None, name}:
				raise PublicationError("retained producer proof differs from source configuration")
			proof_by_name[name] = proof
			if producer.get("kind") != "image":
				continue
			required = strings(producer.get("deliverables"), "required image deliverable")
			if len(required) != 1:
				raise PublicationError("image producer must retain one required deliverable")
			deliverable = required[0].replace("{version}", str(candidate.get("version")))
			if deliverable in image_producer_by_deliverable:
				raise PublicationError("image producer deliverable identity is ambiguous")
			image_producer_by_deliverable[deliverable] = name
		return proof_by_name, image_producer_by_deliverable

	@staticmethod
	def _matches_image_producer_proof(
		image_proof: Mapping[str, object], producer_proof: Mapping[str, object]
	) -> bool:
		return all(
			(
				positive(image_proof.get("artifact_id"), "writer producer artifact ID")
				== positive(producer_proof.get("artifact_id"), "producer artifact ID"),
				digest("sha256:" + str(image_proof.get("artifact_digest")))
				== digest("sha256:" + str(producer_proof.get("digest"))),
				positive(image_proof.get("job_id"), "writer producer job ID")
				== positive(producer_proof.get("job_id"), "producer job ID"),
				positive(image_proof.get("attempt"), "writer producer attempt")
				== positive(producer_proof.get("artifact_attempt"), "producer artifact attempt"),
			)
		)

	@classmethod
	def _validate_image_writer_receipt(
		cls,
		candidate: Mapping[str, object],
		items: list[dict[str, object]],
		receipt: Mapping[str, object],
	) -> None:
		"""Validate retained writer evidence against the candidate and image deliverables.

		Raise PublicationError for invalid or conflicting receipt identities, missing
		image coverage, or mismatched producer proofs. Archive-only receipts must
		contain no image writer evidence."""
		images = [item for item in items if item.get("kind") == "image"]
		verification = record(receipt.get("verification"), "publication verification")
		writer = verification.get("image_writer")
		if not images:
			if writer is not None:
				raise PublicationError(
					"archive-only receipt must not contain image writer evidence"
				)
			return
		writer_record = record(writer, "image writer evidence")
		writer_receipt = record(writer_record.get("receipt"), "retained image writer receipt")
		artifact_proof = record(writer_record.get("artifact_proof"), "image writer artifact proof")
		producer_proof_by_name, image_producer_by_deliverable = cls._image_producer_proofs(
			candidate, verification
		)
		expected_candidate = hashlib.sha256(canonical(candidate)).hexdigest()
		identity_matches = all(
			(
				writer_receipt.get("schema") == 1,
				writer_receipt.get("candidate_sha256") == expected_candidate,
				writer_receipt.get("repository") == candidate.get("repository"),
				writer_receipt.get("pr") == candidate.get("pr"),
				writer_receipt.get("source_sha") == candidate.get("source_sha"),
				writer_receipt.get("version") == candidate.get("version"),
				writer_receipt.get("candidate_provider_repository")
				== candidate.get("provider_repository"),
				writer_receipt.get("candidate_provider_sha") == candidate.get("provider_sha"),
				writer_receipt.get("provider_repository") == candidate.get("provider_repository"),
				sha(writer_receipt.get("provider_sha")) == sha(verification.get("provider_sha")),
				writer_receipt.get("writer_run_id") == verification.get("run_id"),
				positive(writer_receipt.get("writer_attempt"), "writer attempt")
				== positive(artifact_proof.get("attempt"), "writer artifact attempt"),
				positive(artifact_proof.get("artifact_id"), "writer artifact ID") > 0,
				positive(artifact_proof.get("job_id"), "writer job ID") > 0,
			)
		)
		if not identity_matches:
			raise PublicationError("retained image writer receipt identity is invalid")
		_ = digest("sha256:" + str(artifact_proof.get("artifact_digest")))
		writer_images = records(writer_receipt.get("images"), "retained writer images")
		if len(writer_images) != len(images):
			raise PublicationError("retained writer receipt does not cover every image")
		for item in images:
			image = _unique_receipt_image(
				writer_images,
				item.get("name"),
				"retained writer receipt image identity is ambiguous",
			)
			producer_name = image_producer_by_deliverable.get(str(item.get("name")))
			if producer_name is None or image.get("producer") != producer_name:
				raise PublicationError("retained image producer differs from source configuration")
			producer_proof = producer_proof_by_name[producer_name]
			image_proof = record(image.get("producer_proof"), "retained writer producer proof")
			if (
				image.get("image_repository") != item.get("image_repository")
				or image.get("reference") != item.get("reference")
				or image.get("sha256") != item.get("sha256")
				or image.get("archive_sha256") != item.get("archive_sha256")
				or image.get("tag") != f"{item.get('image_repository')}:v{candidate.get('version')}"
				or not cls._matches_image_producer_proof(image_proof, producer_proof)
			):
				raise PublicationError("retained image and writer receipt identities conflict")

	def _draft_receipt(
		self,
		release: Mapping[str, object],
		candidate: Mapping[str, object],
		receipt: dict[str, object],
	) -> dict[str, object]:
		# Fresh verification may resume a draft while preserving its original receipt.
		body = str(release.get("body", ""))
		if body.count(RECEIPT_MARKER) == 1 and body.endswith("-->\n"):
			previous = json_record(body.split(RECEIPT_MARKER)[1].removesuffix("-->\n").encode())
			if previous.get("candidate") == candidate:
				# A recovery rebuild can produce different ZIP bytes. Keep the receipt
				# already bound to this draft; _readback verifies its existing assets.
				receipt = previous
		self._readback(release, candidate, receipt, draft=True)
		return receipt

	def _publish_release(
		self,
		version: str,
		candidate: Mapping[str, object],
		receipt: dict[str, object],
		contents: Mapping[str, bytes],
	) -> dict[str, object]:
		tag = f"v{version}"
		release = self._existing(version)
		tag_sha = self._tag(tag)
		if tag_sha is not None and tag_sha != candidate["source_sha"]:
			raise PublicationError("existing tag identifies a different source")
		if release is not None:
			receipt = self._draft_receipt(release, candidate, receipt)
		if tag_sha is None:
			self.api.post("/git/refs", {"ref": f"refs/tags/{tag}", "sha": candidate["source_sha"]})
		if release is None:
			release = self.api.post(
				"/releases",
				{
					"tag_name": tag,
					"target_commitish": candidate["source_sha"],
					"name": tag,
					"body": self._body(candidate, receipt),
					"draft": True,
					"prerelease": False,
				},
			)
		missing = self._readback(release, candidate, receipt, draft=True)
		release_id = positive(release.get("id"), "release ID")
		expected = {
			str(item["name"]): digest("sha256:" + str(item["sha256"]))
			for item in records(receipt.get("deliverables"), "receipt deliverables")
			if item.get("kind") != "image"
		}
		for name in sorted(missing):
			content = contents.get(name)
			if content is None or hashlib.sha256(content).hexdigest() != expected.get(name):
				raise PublicationError(
					f"existing draft is missing {name}; current verified output "
					"does not match its recorded digest"
				)
			asset = self.api.upload(release_id, name, content)
			if digest(asset.get("digest")) != hashlib.sha256(content).hexdigest():
				raise PublicationError("uploaded asset digest mismatch; draft remains unpublished")
		ready = record(self.api.get(f"/releases/{release_id}"), "ready draft")
		if self._readback(ready, candidate, receipt, draft=True):
			raise PublicationError("draft is not complete; publication refused")
		self.api.patch(f"/releases/{release_id}", {"draft": False})
		self._readback(
			record(self.api.get(f"/releases/{release_id}"), "published release"),
			candidate,
			receipt,
		)
		return {"status": "published", "version": version, "source_sha": candidate["source_sha"]}

	def publish(self, number: int, candidate_artifact_id: int) -> dict[str, object]:
		"""Publish only from a verified original envelope and this run's successful producers."""
		self._recovery_context()
		candidate = self._candidate(number, candidate_artifact_id)
		pull = self._pull(number, merged=True)
		version, config, projections = self._metadata(sha(candidate["source_sha"]))
		completed = self._completed(
			pull, sha(candidate["source_sha"]), version, config, projections
		)
		if completed is not None:
			return completed
		prepare(
			body=str(candidate["body"]),
			version=version,
			base_version=_authority(
				self.api.source(sha(candidate["base_sha"]), ".release/version.toml")
			),
			baseline=self._baseline(),
			projections=projections,
		)
		identities, contents, proofs, image_receipt, image_writer_proof = self._deliverables(
			candidate, candidate_artifact_id
		)
		verification: dict[str, object] = {
			"run_id": self.context.run_id,
			"provider_sha": self.context.provider_sha,
			"producers": proofs,
		}
		if image_receipt is not None:
			verification["image_writer"] = {
				"receipt": image_receipt,
				"artifact_proof": image_writer_proof,
			}
		receipt: dict[str, object] = {
			"schema": 1,
			"candidate": candidate,
			"deliverables": identities,
			"verification": verification,
		}
		return self._publish_release(version, candidate, receipt, contents)
