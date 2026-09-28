"""One shared preparation and publication entry point for all product adapters."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .publication import PublicationError, image_repository, parse_decision, prepare
from .publication_artifacts import ArtifactReader, json_record
from .publication_config import strings, validate_config, validate_identities
from .publication_evidence import digest, positive, record, records, verify_job, verify_run

RECEIPT_MARKER = "\n<!-- shared-publisher-receipt-v1\n"
PAGE_SIZE = 100
MAX_NOTES_BYTES = 64 * 1024


class GitHub(Protocol):
	"""External GitHub and registry operations, controlled by behavioral fixtures."""

	def get(self, path: str) -> object: ...
	def source(self, sha: str, path: str) -> str: ...
	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...
	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...
	def upload(self, release_id: int, name: str, content: bytes) -> dict[str, object]: ...
	def download(self, path: str) -> bytes: ...
	def image_digest(self, reference: str) -> str: ...
	def preflight_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> None: ...
	def publish_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> dict[str, str]: ...


def sha(value: object) -> str:
	"""Require a full immutable Git source identity."""
	if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
		raise PublicationError("source and provider revisions must be full lowercase commit SHAs")
	return value


def canonical(value: object) -> bytes:
	"""Stable UTF-8 encoding for retained envelopes and receipts."""
	return (
		json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
	).encode("utf-8")


def _toml(content: str) -> dict[str, object]:
	if len(content.encode("utf-8")) > 1024 * 1024:
		raise PublicationError("source metadata exceeds 1 MiB")
	try:
		return record(tomllib.loads(content), "source TOML")
	except tomllib.TOMLDecodeError as error:
		raise PublicationError("source metadata is invalid TOML") from error


def _authority(content: str) -> str:
	fields = _toml(content)
	if set(fields) != {"version"} or not isinstance(fields["version"], str):
		raise PublicationError(
			"version authority must contain exactly one top-level version string"
		)
	return fields["version"]


def _projection(content: str, projection: Mapping[str, object]) -> str:
	kind, key = projection.get("format"), projection.get("key")
	value: object
	if not isinstance(key, str):
		raise PublicationError("projection key is required")
	if kind == "python":
		values = [
			node.value.value
			for node in ast.parse(content).body
			if isinstance(node, ast.Assign)
			and isinstance(node.value, ast.Constant)
			and any(isinstance(target, ast.Name) and target.id == key for target in node.targets)
		]
		if len(values) != 1:
			raise PublicationError("Python projection requires one literal assignment")
		value = values[0]
	else:
		value = json.loads(content) if kind == "json" else _toml(content)
		if kind not in {"json", "toml"}:
			raise PublicationError("unknown projection format")
		for part in key.split("."):
			value = record(value, "projection").get(part)
	prefix = projection.get("prefix", "")
	if not isinstance(value, str) or not isinstance(prefix, str) or not value.startswith(prefix):
		raise PublicationError("version projection is missing or invalid")
	return value.removeprefix(prefix)


@dataclass(frozen=True)
class Context:
	"""Actual Actions context supplied by the reusable wrapper, never by a product file."""

	repository: str
	provider_repository: str
	provider_sha: str
	helper_sha: str
	run_id: int
	attempt: int
	run_source_sha: str
	run_head_sha: str


@dataclass(frozen=True, slots=True)
class _VerifiedImage:
	producer: Mapping[str, object]
	item: Mapping[str, object]
	archive_name: str
	archive_sha256: str
	archive_bytes: bytes
	producer_proof: Mapping[str, object]


def _unique_receipt_image(
	images: list[dict[str, object]], name: object, failure_message: str
) -> dict[str, object]:
	matches = [image for image in images if image.get("name") == name]
	if len(matches) != 1:
		raise PublicationError(failure_message)
	return matches[0]


class Publisher:
	"""Validate original authorization, verify evidence, and resume one exact candidate."""

	def __init__(
		self,
		api: GitHub,
		context: Context,
		*,
		evidence_wait_seconds: float = 30.0,
		evidence_poll_seconds: float = 2.0,
	) -> None:
		self.api = api
		self.context = context
		self.artifacts = ArtifactReader(
			api,
			wait_seconds=evidence_wait_seconds,
			poll_seconds=evidence_poll_seconds,
		)
		if sha(context.provider_sha) != sha(context.helper_sha):
			raise PublicationError("executed helper differs from reusable workflow revision")
		sha(context.run_source_sha)
		sha(context.run_head_sha)
		positive(context.run_id, "run ID")
		positive(context.attempt, "run attempt")

	def _releases(self) -> list[dict[str, object]]:
		result: list[dict[str, object]] = []
		for page in range(1, 101):
			items = records(self.api.get(f"/releases?per_page=100&page={page}"), "releases")
			result.extend(items)
			if len(items) < PAGE_SIZE:
				return result
		raise PublicationError("release history exceeds 10,000 entries")

	def _baseline(self) -> str:
		versions = [
			str(item["tag_name"])[1:]
			for item in self._releases()
			if not item.get("draft")
			and not item.get("prerelease")
			and re.fullmatch(
				r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", str(item.get("tag_name"))
			)
		]
		if not versions:
			raise PublicationError(
				"published stable baseline is unavailable; explicit bootstrap is required"
			)
		version = max(versions, key=lambda item: tuple(int(part) for part in item.split(".")))
		if self._tag(f"v{version}") is None:
			raise PublicationError("published baseline tag is missing")
		return version

	def _tag(self, tag: str) -> str | None:
		value = self.api.get(f"/git/ref/tags/{tag}")
		if value is None:
			return None
		for _ in range(4):
			identity = record(record(value, "tag").get("object"), "tag object")
			commit = sha(identity.get("sha"))
			if identity.get("type") == "commit":
				return commit
			if identity.get("type") != "tag":
				break
			value = self.api.get(f"/git/tags/{commit}")
		raise PublicationError("tag does not resolve to a commit within four indirections")

	def _pull(self, number: int, *, merged: bool) -> dict[str, object]:
		positive(number, "PR number")
		repository = record(self.api.get("/"), "repository")
		pull = record(self.api.get(f"/pulls/{number}"), "pull request")
		base = record(pull.get("base"), "PR base")
		if (
			pull.get("number") != number
			or repository.get("full_name") != self.context.repository
			or base.get("ref") != repository.get("default_branch")
			or record(base.get("repo"), "base repository").get("full_name")
			!= self.context.repository
		):
			raise PublicationError("PR does not target this repository's default branch")
		if merged and (
			pull.get("merged") is not True
			or not pull.get("merged_at")
			or record(pull.get("merged_by"), "PR merger").get("login")
			!= record(repository.get("owner"), "owner").get("login")
		):
			raise PublicationError("publication requires an owner-merged PR")
		return pull

	def _metadata(self, source: str) -> tuple[str, dict[str, object], list[str]]:
		version = _authority(self.api.source(source, ".release/version.toml"))
		config = _toml(self.api.source(source, ".release/publisher.toml"))
		validate_config(config)
		projections = [
			_projection(self.api.source(source, str(item.get("path"))), item)
			for item in records(config.get("projections", []), "projections")
		]
		return version, config, projections

	def prepare(
		self,
		number: int,
		*,
		event: Mapping[str, object],
		validation_only: bool = False,
	) -> dict[str, object]:
		"""Resolve exact PR source and return validated, no-release, or a candidate envelope."""
		pull = self._pull(number, merged=not validation_only)
		original = record(event.get("pull_request"), "original PR event")
		if original.get("number") != number or original.get("body") != pull.get("body"):
			raise PublicationError(
				"current PR decision differs from the original event; restore its body"
			)
		source = (
			sha(record(pull.get("head"), "PR head").get("sha"))
			if validation_only
			else sha(pull.get("merge_commit_sha"))
		)
		if not validation_only and (
			event.get("action") != "closed"
			or original.get("merged") is not True
			or original.get("merge_commit_sha") != source
			or source != self.context.run_source_sha
			or record(original.get("head"), "original PR head").get("sha")
			!= self.context.run_head_sha
		):
			raise PublicationError("event does not authorize the exact final merged source")
		version, config, projections = self._metadata(source)
		base = sha(record(pull.get("base"), "PR base").get("sha"))
		completed = (
			self._completed(pull, source, version, config, projections)
			if not validation_only
			else None
		)
		if completed is not None:
			return completed
		if not validation_only and self._original_artifacts(number):
			return self.recover(number)
		result = prepare(
			body=str(original.get("body") or ""),
			version=version,
			base_version=_authority(self.api.source(base, ".release/version.toml")),
			baseline=self._baseline(),
			projections=projections,
		)
		if validation_only:
			return result
		if not result["release"]:
			return {**result, "status": "no-release"}
		notes = self.api.source(source, ".release/notes.md")
		if not notes.strip() or len(notes.encode("utf-8")) > MAX_NOTES_BYTES:
			raise PublicationError("source release notes must be nonempty and at most 64 KiB")
		candidate = {
			"schema": 1,
			"repository": self.context.repository,
			"pr": number,
			"source_sha": source,
			"base_sha": base,
			"merged_at": original.get("merged_at"),
			"body": original.get("body"),
			"version": version,
			"notes": notes,
			"notes_sha256": hashlib.sha256(notes.encode("utf-8")).hexdigest(),
			"provider_repository": self.context.provider_repository,
			"provider_sha": self.context.provider_sha,
			"origin_run_id": self.context.run_id,
			"origin_attempt": self.context.attempt,
			"origin_run_head_sha": self.context.run_head_sha,
			"config": config,
		}
		return {
			"status": "build-required",
			"version": version,
			"source_sha": source,
			"candidate": candidate,
		}

	def _existing(self, version: str) -> dict[str, object] | None:
		matches = [item for item in self._releases() if item.get("tag_name") == f"v{version}"]
		if len(matches) > 1:
			raise PublicationError("duplicate conflicting releases or drafts")
		return matches[0] if matches else None

	def _completed(
		self,
		pull: Mapping[str, object],
		source: str,
		version: str,
		config: Mapping[str, object],
		projections: list[str],
	) -> dict[str, object] | None:
		# No-release decisions must not inspect an older publication's legacy receipt.
		if "No release" in parse_decision(str(pull.get("body") or "")):
			return None
		release = self._existing(version)
		if release is None or release.get("draft") is True:
			return None
		body = str(release.get("body", ""))
		if body.count(RECEIPT_MARKER) != 1 or not body.endswith("-->\n"):
			raise PublicationError("existing publication lacks the original candidate receipt")
		receipt = json_record(body.split(RECEIPT_MARKER)[1].removesuffix("-->\n").encode("utf-8"))
		candidate = record(receipt.get("candidate"), "completed candidate")
		self._validate_candidate(candidate, pull)
		if candidate.get("config") != config or any(item != version for item in projections):
			raise PublicationError("completed release source projections or contract conflict")
		self._readback(release, candidate, receipt)
		return {"status": "already-complete", "version": version, "source_sha": source}

	def _validate_candidate(
		self, candidate: Mapping[str, object], pull: Mapping[str, object]
	) -> None:
		source = sha(candidate.get("source_sha"))
		version, config, projections = self._metadata(source)
		notes = self.api.source(source, ".release/notes.md")
		if parse_decision(str(candidate.get("body") or "")).get("Version") != version:
			raise PublicationError("original candidate decision does not authorize its version")
		if (
			candidate.get("schema") != 1
			or candidate.get("repository") != self.context.repository
			or candidate.get("provider_repository") != self.context.provider_repository
			or candidate.get("pr") != pull.get("number")
			or source != pull.get("merge_commit_sha")
			or candidate.get("base_sha") != record(pull.get("base"), "PR base").get("sha")
			or candidate.get("origin_run_head_sha")
			!= record(pull.get("head"), "PR head").get("sha")
			or candidate.get("body") != pull.get("body")
			or candidate.get("merged_at") != pull.get("merged_at")
			or candidate.get("version") != version
			or candidate.get("config") != config
			or candidate.get("notes") != notes
			or candidate.get("notes_sha256") != hashlib.sha256(notes.encode("utf-8")).hexdigest()
			or any(item != version for item in projections)
		):
			raise PublicationError(
				"retained original candidate conflicts with PR decision or exact source; "
				"restore original body"
			)

	def _candidate(self, number: int, artifact_id: int) -> dict[str, object]:
		artifact, files = self.artifacts.read(artifact_id)
		if set(files) != {"candidate.json"}:
			raise PublicationError("candidate envelope artifact must contain only candidate.json")
		candidate = json_record(files["candidate.json"])
		self._validate_candidate(candidate, self._pull(number, merged=True))
		run_id = positive(candidate.get("origin_run_id"), "original run ID")
		attempt = positive(candidate.get("origin_attempt"), "original attempt")
		job = self.artifacts.jobs_for_attempt(
			run_id, "Prepare release / Prepare candidate", attempt, ["Prepare candidate"]
		)
		config = record(candidate.get("config"), "candidate configuration")
		self.artifacts.prove(
			artifact,
			job=job,
			repository=self.context.repository,
			run_id=run_id,
			run_head_sha=sha(candidate.get("origin_run_head_sha")),
			current_attempt=attempt,
			provider_repository=str(candidate.get("provider_repository")),
			provider_sha=sha(candidate.get("provider_sha")),
			workflow=str(config.get("workflow")),
			name=f"release-candidate-pr-{number}-{run_id}-{attempt}",
			checks=["Prepare candidate"],
		)
		return candidate

	def _original_artifacts(self, number: int) -> list[dict[str, object]]:
		artifacts = self.artifacts.pages("/actions/artifacts", "artifacts")
		return [
			item
			for item in artifacts
			if re.fullmatch(rf"release-candidate-pr-{number}-[0-9]+-[0-9]+", str(item.get("name")))
			and item.get("expired") is False
		]

	def recover(self, number: int) -> dict[str, object]:
		"""Recover by PR identity using proven original envelopes, never an editable body alone."""
		self._recovery_context()
		pull = self._pull(number, merged=True)
		source = sha(pull.get("merge_commit_sha"))
		version, config, projections = self._metadata(source)
		completed = self._completed(pull, source, version, config, projections)
		if completed is not None:
			return completed
		matches = self._original_artifacts(number)
		if not matches:
			raise PublicationError(
				"original candidate envelope is missing or expired; "
				"rerun its original merge event or prepare a new reviewed PR"
			)
		candidates = [
			(
				positive(item.get("id"), "candidate artifact ID"),
				self._candidate(number, positive(item.get("id"), "candidate artifact ID")),
			)
			for item in matches
		]
		if len({canonical(candidate) for _, candidate in candidates}) != 1:
			raise PublicationError("original candidate envelope lookup is ambiguous")
		artifact_id, candidate = min(candidates, key=lambda item: item[0])
		prepare(
			body=str(candidate["body"]),
			version=version,
			base_version=_authority(
				self.api.source(sha(candidate["base_sha"]), ".release/version.toml")
			),
			baseline=self._baseline(),
			projections=projections,
		)
		return {
			"status": "build-required",
			"version": version,
			"source_sha": source,
			"candidate": candidate,
			"candidate_artifact_id": artifact_id,
		}

	def _recovery_context(self) -> None:
		run = self.artifacts.run(self.context.run_id, expected_attempt=self.context.attempt)
		if run.get("event") == "workflow_dispatch":
			repository = record(self.api.get("/"), "repository")
			if run.get("head_branch") != repository.get("default_branch"):
				raise PublicationError("recovery tooling must run from the default branch")
			if run.get("head_sha") != self.context.run_source_sha:
				raise PublicationError("recovery tooling source differs from its Actions run")

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
		cls: type[Publisher],
		candidate: Mapping[str, object],
		items: list[dict[str, object]],
		receipt: Mapping[str, object],
	) -> None:
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
