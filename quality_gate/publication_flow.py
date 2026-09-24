"""One shared preparation and publication entry point for all product adapters."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .publication import PublicationError, parse_decision, prepare
from .publication_artifacts import ArtifactReader, json_record
from .publication_config import validate_config, validate_identities
from .publication_evidence import digest, positive, record, records, verify_job

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

	def _deliverables(
		self,
		candidate: Mapping[str, object],
	) -> tuple[list[dict[str, object]], dict[str, bytes], list[dict[str, object]]]:
		self._verification_context(candidate)
		config = record(candidate.get("config"), "candidate configuration")
		producers = records(config.get("producers"), "required producers")
		if not producers:
			raise PublicationError("at least one product verification producer is required")
		identities: list[dict[str, object]] = []
		contents: dict[str, bytes] = {}
		proofs: list[dict[str, object]] = []
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
				self._verify_deliverable(item, files)
				if item.get("kind") != "image":
					contents[name] = files[name]
				identities.append(item)
			proofs.append(proof)
		validate_identities(candidate, identities)
		return identities, contents, proofs

	def _verify_deliverable(self, item: Mapping[str, object], files: Mapping[str, bytes]) -> None:
		name = str(item.get("name"))
		if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None:
			raise PublicationError("unsafe deliverable name")
		expected = digest("sha256:" + str(item.get("sha256")))
		if item.get("kind") == "image":
			reference = str(item.get("reference"))
			if (
				not reference.endswith("@sha256:" + expected)
				or self.api.image_digest(reference) != expected
			):
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
				self._verify_deliverable(item, {})
		return set(expected) - set(actual)

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
			if (
				previous.get("candidate") == candidate
				and previous.get("deliverables") == receipt["deliverables"]
			):
				receipt = previous
		self._readback(release, candidate, receipt, draft=True)
		return receipt

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
		identities, contents, proofs = self._deliverables(candidate)
		receipt: dict[str, object] = {
			"schema": 1,
			"candidate": candidate,
			"deliverables": identities,
			"verification": {
				"run_id": self.context.run_id,
				"provider_sha": self.context.provider_sha,
				"producers": proofs,
			},
		}
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
		for name in sorted(missing):
			asset = self.api.upload(release_id, name, contents[name])
			if digest(asset.get("digest")) != hashlib.sha256(contents[name]).hexdigest():
				raise PublicationError("uploaded asset digest mismatch; draft remains unpublished")
		ready = record(self.api.get(f"/releases/{release_id}"), "ready draft")
		if self._readback(ready, candidate, receipt, draft=True):
			raise PublicationError("draft is not complete; publication refused")
		self.api.patch(f"/releases/{release_id}", {"draft": False})
		self._readback(
			record(self.api.get(f"/releases/{release_id}"), "published release"), candidate, receipt
		)
		return {"status": "published", "version": version, "source_sha": candidate["source_sha"]}
