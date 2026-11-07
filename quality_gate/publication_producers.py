"""Publisher producers operations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from .publication import PublicationError, image_repository
from .publication_artifacts import json_record
from .publication_config import validate_identities
from .publication_evidence import positive, record, records, verify_job
from .publication_flow_context import _PublisherContext
from .publication_flow_support import (
	canonical,
)


class _Producers(_PublisherContext):
	def _latest_producer_job(
		self,
		job_name: str,
		checks: list[str],
		*,
		minimum_attempt: int = 1,
	) -> tuple[dict[str, object], int]:
		context = self.context
		job = self.artifacts.latest_job(
			context.run_id,
			job_name,
			required_steps=checks,
			minimum_attempt=minimum_attempt,
		)
		attempt = positive(job.get("run_attempt"), "producing attempt")
		verify_job(
			job,
			run_id=context.run_id,
			run_head_sha=context.run_head_sha,
			current_attempt=context.attempt,
			job_name=job_name,
			required_steps=checks,
		)
		return job, attempt

	@staticmethod
	def _artifact_attempt(prefix: str, item: Mapping[str, object]) -> int | None:
		match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)", str(item.get("name")))
		return int(match.group(1)) if match is not None else None

	@classmethod
	def _contains_evidence(
		cls,
		items: list[dict[str, object]],
		prefix: str,
		expected_name: str | None,
	) -> bool:
		return any(
			cls._artifact_attempt(prefix, item) is not None
			and (expected_name is None or item.get("name") == expected_name)
			for item in items
		)

	def _producer_artifacts(
		self,
		producer_name: str,
		prefix: str,
		*,
		attempt: int | None = None,
	) -> list[tuple[int, dict[str, object]]]:
		context = self.context
		expected_name = f"{prefix}{attempt}" if attempt is not None else None
		label = f"required evidence artifact for producer {producer_name}"
		if attempt is not None:
			label += f" attempt {attempt}"
		items = self.artifacts.pages(
			f"/actions/runs/{context.run_id}/artifacts",
			"artifacts",
			accept=lambda values: self._contains_evidence(values, prefix, expected_name),
			wait_label=label,
		)
		result: list[tuple[int, dict[str, object]]] = []
		for item in items:
			item_attempt = self._artifact_attempt(prefix, item)
			if item_attempt is not None:
				result.append((item_attempt, item))
		return result

	def _select_producer_artifact(
		self,
		producer_name: str,
		prefix: str,
		job_name: str,
		checks: list[str],
		latest_job: dict[str, object],
		latest_attempt: int,
		available: list[tuple[int, dict[str, object]]],
	) -> tuple[int, dict[str, object], dict[str, object], int]:
		future_attempts = [attempt for attempt, _item in available if attempt > latest_attempt]
		if future_attempts:
			latest_job, latest_attempt = self._latest_producer_job(
				job_name, checks, minimum_attempt=max(future_attempts)
			)
			if any(attempt > latest_attempt for attempt, _item in available):
				raise PublicationError(
					"producer evidence artifact is newer than the reported job attempt"
				)
		eligible = [(attempt, item) for attempt, item in available if attempt <= latest_attempt]
		if not eligible:
			raise PublicationError(
				f"required evidence artifact for producer {producer_name} is missing "
				f"through job attempt {latest_attempt}"
			)
		attempt = max(item_attempt for item_attempt, _item in eligible)
		matches = [item for item_attempt, item in eligible if item_attempt == attempt]
		if len(matches) != 1:
			raise PublicationError(
				f"evidence artifact for producer {producer_name} is ambiguous at attempt {attempt}"
			)
		artifact = matches[0]
		job = latest_job
		if attempt < latest_attempt:
			attempt_job = self.artifacts.jobs_for_attempt(
				self.context.run_id, job_name, attempt, checks
			)
			if self.artifacts.same_execution(attempt_job, latest_job):
				job = attempt_job
			else:
				latest_matches = self._producer_artifacts(
					producer_name, prefix, attempt=latest_attempt
				)
				if len(latest_matches) != 1:
					raise PublicationError(
						f"evidence artifact for producer {producer_name} is ambiguous "
						f"at attempt {latest_attempt}"
					)
				attempt, artifact = latest_matches[0]
		return attempt, artifact, job, latest_attempt

	def _producer_bundle(
		self,
		candidate: Mapping[str, object],
		producer: Mapping[str, object],
	) -> tuple[dict[str, object], dict[str, bytes], dict[str, object]]:
		context = self.context
		job_name = str(producer.get("job"))
		checks = producer.get("checks")
		if (
			not isinstance(checks, list)
			or not checks
			or not all(isinstance(item, str) for item in checks)
		):
			raise PublicationError("producer must name its required verification steps")
		latest_job, latest_attempt = self._latest_producer_job(job_name, checks)
		producer_name = str(producer.get("name"))
		prefix = f"release-evidence-{producer_name}-"
		available = self._producer_artifacts(producer_name, prefix)
		attempt, artifact_item, job, latest_attempt = self._select_producer_artifact(
			producer_name,
			prefix,
			job_name,
			checks,
			latest_job,
			latest_attempt,
			available,
		)
		name = f"{prefix}{attempt}"
		artifact, files = self.artifacts.read(positive(artifact_item.get("id"), "artifact ID"))
		config = record(candidate.get("config"), "candidate configuration")
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
			name=name,
			checks=checks,
		)
		evidence = json_record(files.get("evidence.json", b""))
		expected = {
			"schema": 1,
			"repository": context.repository,
			"pr": candidate["pr"],
			"source_sha": candidate["source_sha"],
			"version": candidate["version"],
			"provider_sha": context.provider_sha,
			"run_id": context.run_id,
			"attempt": attempt,
			"candidate_sha256": hashlib.sha256(canonical(candidate)).hexdigest(),
		}
		if {key: evidence.get(key) for key in expected} != expected:
			raise PublicationError(
				"product evidence does not bind this candidate and producing run"
			)
		proof["artifact_attempt"] = attempt
		proof["latest_attempt"] = latest_attempt
		proof["producer"] = producer_name
		return evidence, files, proof

	def _verification_context(self, candidate: Mapping[str, object]) -> None:
		run = self.artifacts.run(self.context.run_id, expected_attempt=self.context.attempt)
		if run.get("event") == "pull_request" and (
			self.context.run_id != candidate.get("origin_run_id")
			or self.context.run_head_sha != candidate.get("origin_run_head_sha")
			or self.context.run_source_sha != candidate.get("source_sha")
		):
			raise PublicationError(
				"verification must use the original merge run or default-branch recovery"
			)

	def _image_deliverable_identity(
		self,
		item: Mapping[str, object],
		producer: Mapping[str, object],
		files: Mapping[str, bytes],
		candidate: Mapping[str, object],
		image_receipt: Mapping[str, object] | None,
		proof: Mapping[str, object],
	) -> dict[str, object]:
		if image_receipt is None:
			raise PublicationError("shared image writer receipt is missing")
		name = str(item.get("name"))
		archive_name, archive_sha256 = self._image_archive(item, files)
		writer_image = self._writer_image(
			image_receipt,
			producer=str(producer["name"]),
			name=name,
			archive_name=archive_name,
			archive_sha256=archive_sha256,
			producer_proof=proof,
		)
		package = image_repository(producer.get("image_repository"))
		if writer_image.get("tag") != f"{package}:v{candidate['version']}":
			raise PublicationError("image writer tag differs from reviewed source contract")
		identity = {
			"kind": "image",
			"name": name,
			"sha256": writer_image["sha256"],
			"reference": writer_image["reference"],
			"image_repository": writer_image["image_repository"],
			"archive_sha256": archive_sha256,
		}
		if identity["image_repository"] != package:
			raise PublicationError("image writer destination differs from reviewed source")
		self._verify_deliverable(identity, {}, expected_image_repository=package)
		return identity

	def _deliverables(
		self,
		candidate: Mapping[str, object],
		candidate_artifact_id: int,
	) -> tuple[
		list[dict[str, object]],
		dict[str, bytes],
		list[dict[str, object]],
		dict[str, object] | None,
		dict[str, object] | None,
	]:
		self._verification_context(candidate)
		config = record(candidate.get("config"), "candidate configuration")
		producers = records(config.get("producers"), "required producers")
		if not producers:
			raise PublicationError("at least one product verification producer is required")
		identities: list[dict[str, object]] = []
		contents: dict[str, bytes] = {}
		proofs: list[dict[str, object]] = []
		image_receipt: dict[str, object] | None = None
		image_writer_proof: dict[str, object] | None = None
		if any(producer.get("kind") == "image" for producer in producers):
			image_receipt, image_writer_proof = self._image_receipt(
				candidate, candidate_artifact_id
			)
		for producer in producers:
			evidence, files, proof = self._producer_bundle(candidate, producer)
			items = records(evidence.get("deliverables"), "deliverables")
			required = producer.get("deliverables")
			if not isinstance(required, list) or not required:
				raise PublicationError("producer deliverable names are required")
			expected = {
				str(name).replace("{version}", str(candidate["version"])) for name in required
			}
			if {item.get("name") for item in items} != expected or len(items) != len(expected):
				raise PublicationError(
					"required deliverable identities are incomplete or substituted"
				)
			for item in items:
				name = str(item.get("name"))
				if any(identity.get("name") == name for identity in identities):
					raise PublicationError("duplicate deliverable identity")
				if producer.get("kind") == "image":
					identity = self._image_deliverable_identity(
						item, producer, files, candidate, image_receipt, proof
					)
				else:
					self._verify_deliverable(item, files)
					contents[name] = files[name]
					identity = item
				identities.append(identity)
			proofs.append(proof)
		validate_identities(candidate, identities)
		if image_receipt is not None:
			self._validate_image_writer_receipt(
				candidate,
				identities,
				{
					"verification": {
						"run_id": self.context.run_id,
						"provider_sha": self.context.provider_sha,
						"producers": proofs,
						"image_writer": {
							"receipt": image_receipt,
							"artifact_proof": image_writer_proof,
						},
					}
				},
			)
		return identities, contents, proofs, image_receipt, image_writer_proof
