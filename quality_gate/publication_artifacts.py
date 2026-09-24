"""Bounded Actions artifact transport and verified producer selection."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO, Protocol

from .publication import PublicationError
from .publication_evidence import digest, positive, record, records, verify_producer

MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_MEMBERS = 100
PAGE_SIZE = 100
MAX_EVIDENCE_WAIT_SECONDS = 300.0
MAX_EVIDENCE_POLL_SECONDS = 60.0


class ArtifactAPI(Protocol):
	def get(self, path: str) -> object: ...
	def download(self, path: str) -> bytes: ...


def json_record(content: bytes) -> dict[str, object]:
	"""Decode one bounded evidence object."""
	if len(content) > MAX_EVIDENCE_BYTES:
		raise PublicationError("evidence JSON exceeds 1 MiB")
	try:
		return record(json.loads(content), "evidence")
	except (UnicodeError, json.JSONDecodeError) as error:
		raise PublicationError("evidence is not valid UTF-8 JSON") from error


def load_json_record(stream: BinaryIO) -> dict[str, object]:
	"""Read an evidence file with the size bound enforced before decoding."""
	return json_record(stream.read(MAX_EVIDENCE_BYTES + 1))


def unpack(content: bytes, expected_digest: str) -> dict[str, bytes]:
	"""Verify the uploaded archive before reading bounded flat regular-file members."""
	if len(content) > MAX_BUNDLE_BYTES or hashlib.sha256(content).hexdigest() != expected_digest:
		raise PublicationError("downloaded Actions artifact digest or size does not match")
	try:
		with zipfile.ZipFile(io.BytesIO(content)) as archive:
			members = archive.infolist()
			if (
				len(members) > MAX_MEMBERS
				or sum(item.file_size for item in members) > MAX_BUNDLE_BYTES
			):
				raise PublicationError("artifact expansion exceeds the supported bound")
			result: dict[str, bytes] = {}
			for item in members:
				if (
					re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item.filename) is None
					or item.filename in result
					or stat.S_ISLNK(item.external_attr >> 16)
				):
					raise PublicationError("artifact contains duplicate or unsafe members")
				result[item.filename] = archive.read(item)
			return result
	except (zipfile.BadZipFile, RuntimeError) as error:
		raise PublicationError("Actions artifact is not a readable ZIP") from error


class ArtifactReader:
	"""Cross-check IDs, producing attempts, logs, and downloaded archive contents."""

	def __init__(
		self,
		api: ArtifactAPI,
		*,
		wait_seconds: float = 30.0,
		poll_seconds: float = 2.0,
	) -> None:
		if (
			not 0 < wait_seconds <= MAX_EVIDENCE_WAIT_SECONDS
			or not 0 < poll_seconds <= MAX_EVIDENCE_POLL_SECONDS
			or poll_seconds > wait_seconds
		):
			raise PublicationError("Actions evidence wait and poll intervals are invalid")
		self.api = api
		self.wait_seconds = wait_seconds
		self.poll_seconds = poll_seconds

	def _wait(self, deadline: float, label: str) -> None:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise PublicationError(
				f"GitHub {label} remained incomplete; provenance cannot be verified"
			)
		time.sleep(min(self.poll_seconds, remaining))

	def pages(
		self,
		path: str,
		field: str,
		*,
		accept: Callable[[list[dict[str, object]]], bool] | None = None,
		wait_label: str | None = None,
	) -> list[dict[str, object]]:
		deadline = time.monotonic() + self.wait_seconds
		while True:
			result: list[dict[str, object]] = []
			total_count: int | None = None
			incomplete = False
			for page in range(1, 101):
				page_result = self._page(path, field, page)
				if page_result is None:
					incomplete = True
					break
				items, page_total = page_result
				if total_count is not None and total_count != page_total:
					incomplete = True
					break
				total_count = page_total
				result.extend(items)
				if len(result) > total_count:
					incomplete = True
					break
				if len(result) == total_count:
					break
				if len(items) < PAGE_SIZE:
					incomplete = True
					break
			if (
				not incomplete
				and total_count is not None
				and len(result) == total_count
				and (accept is None or accept(result))
			):
				return result
			self._wait(deadline, wait_label or field)

	def _page(self, path: str, field: str, page: int) -> tuple[list[dict[str, object]], int] | None:
		separator = "&" if "?" in path else "?"
		value = self.api.get(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
		if value is None:
			return None
		response = record(value, field)
		raw_items = response.get(field)
		raw_total = response.get("total_count")
		if raw_items is None or raw_total is None:
			return None
		if type(raw_total) is not int or raw_total < 0:
			raise PublicationError(f"GitHub {field} total_count is invalid")
		if raw_total > PAGE_SIZE * 100:
			raise PublicationError(f"{field} exceeds 10,000 entries; narrow the recovery lookup")
		return records(raw_items, field), raw_total

	def run(self, run_id: int, *, expected_attempt: int | None = None) -> dict[str, object]:
		"""Wait for a complete run record, optionally matching the current invocation."""
		positive(run_id, "run ID")
		deadline = time.monotonic() + self.wait_seconds
		label = f"Actions run metadata for ID {run_id}"
		if expected_attempt is not None:
			positive(expected_attempt, "verification attempt")
			label = f"Actions run attempt for ID {run_id} to match publisher context"
		required = {
			"id",
			"run_attempt",
			"head_sha",
			"path",
			"event",
			"repository",
			"head_repository",
			"referenced_workflows",
		}
		while True:
			value = self.api.get(f"/actions/runs/{run_id}")
			if value is not None:
				run = record(value, "run")
				repository = run.get("repository")
				head_repository = run.get("head_repository")
				references = run.get("referenced_workflows")
				complete = (
					required.issubset(run)
					and isinstance(repository, dict)
					and isinstance(repository.get("full_name"), str)
					and isinstance(head_repository, dict)
					and isinstance(head_repository.get("full_name"), str)
					and isinstance(references, list)
					and bool(references)
					and all(
						isinstance(reference, dict)
						and isinstance(reference.get("path"), str)
						and isinstance(reference.get("sha"), str)
						for reference in references
					)
				)
				attempt = run.get("run_attempt")
				if (
					complete
					and type(attempt) is int
					and attempt > 0
					and (expected_attempt is None or attempt == expected_attempt)
				):
					return run
			self._wait(deadline, label)

	def jobs_for_attempt(
		self, run_id: int, name: str, attempt: int, required_steps: Sequence[str]
	) -> dict[str, object]:
		"""Read the job record from the exact run attempt named by its artifact."""
		positive(run_id, "run ID")
		positive(attempt, "producing attempt")
		jobs = self.pages(
			f"/actions/runs/{run_id}/attempts/{attempt}/jobs",
			"jobs",
			accept=lambda items: any(
				item.get("name") == name and self._ready_job(item, required_steps) for item in items
			),
			wait_label=f"producer job {name} for attempt {attempt}",
		)
		matches = [item for item in jobs if item.get("name") == name]
		if len(matches) != 1:
			raise PublicationError(f"producing job identity is ambiguous at attempt {attempt}")
		job = matches[0]
		reported_attempt = job.get("run_attempt")
		if reported_attempt is not None and reported_attempt != attempt:
			raise PublicationError("GitHub returned a producer job from a different attempt")
		return {**job, "run_attempt": attempt}

	@staticmethod
	def _ready_job(item: Mapping[str, object], required_steps: Sequence[str]) -> bool:
		if (
			not {
				"id",
				"run_id",
				"head_sha",
				"name",
				"status",
				"conclusion",
				"started_at",
				"completed_at",
				"steps",
			}.issubset(item)
			or item.get("status") != "completed"
		):
			return False
		job_id = item.get("id")
		run_id = item.get("run_id")
		if (
			type(job_id) is not int
			or job_id <= 0
			or type(run_id) is not int
			or run_id <= 0
			or not isinstance(item.get("head_sha"), str)
			or not isinstance(item.get("name"), str)
			or not isinstance(item.get("conclusion"), str)
			or not isinstance(item.get("started_at"), str)
			or not isinstance(item.get("completed_at"), str)
		):
			return False
		steps = item.get("steps")
		if not isinstance(steps, list):
			return False
		for name in [*required_steps, "Upload release evidence"]:
			matches = [
				step for step in steps if isinstance(step, dict) and step.get("name") == name
			]
			if len(matches) != 1 or matches[0].get("conclusion") is None:
				return False
		return True

	@staticmethod
	def _ready_jobs(
		items: list[dict[str, object]],
		name: str,
		minimum_attempt: int,
		required_steps: Sequence[str],
	) -> bool:
		matches = [item for item in items if item.get("name") == name]
		if not matches:
			return False
		attempts: list[int] = []
		for item in matches:
			attempt = item.get("run_attempt")
			if type(attempt) is not int or not ArtifactReader._ready_job(item, required_steps):
				return False
			attempts.append(attempt)
		latest_attempt = max(attempts)
		latest = [item for item in matches if item.get("run_attempt") == latest_attempt]
		return (
			latest_attempt >= minimum_attempt
			and len(latest) == 1
			and latest[0].get("status") == "completed"
		)

	def latest_job(
		self,
		run_id: int,
		name: str,
		*,
		required_steps: Sequence[str],
		minimum_attempt: int = 1,
	) -> dict[str, object]:
		positive(minimum_attempt, "minimum attempt")
		jobs = self.pages(
			f"/actions/runs/{run_id}/jobs?filter=all",
			"jobs",
			accept=lambda items: self._ready_jobs(items, name, minimum_attempt, required_steps),
			wait_label=f"latest producer job {name}",
		)
		matches = [item for item in jobs if item.get("name") == name]
		attempt = max(positive(item.get("run_attempt"), "attempt") for item in matches)
		latest = [item for item in matches if item.get("run_attempt") == attempt]
		if len(latest) != 1:
			raise PublicationError("producing job identity is ambiguous")
		return latest[0]

	@staticmethod
	def same_execution(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
		"""Identify a successful job record reused unchanged by a later run attempt."""
		keys = (
			"id",
			"run_id",
			"head_sha",
			"name",
			"status",
			"conclusion",
			"started_at",
			"completed_at",
			"steps",
		)
		return all(left.get(key) == right.get(key) for key in keys)

	def read(self, artifact_id: int) -> tuple[dict[str, object], dict[str, bytes]]:
		positive(artifact_id, "artifact ID")
		deadline = time.monotonic() + self.wait_seconds
		while True:
			value = self.api.get(f"/actions/artifacts/{artifact_id}")
			if value is not None:
				artifact = record(value, "artifact")
				if {"id", "expired", "digest", "created_at", "workflow_run"}.issubset(artifact):
					break
			self._wait(deadline, f"artifact metadata for ID {artifact_id}")
		if artifact.get("id") != artifact_id or artifact.get("expired") is not False:
			raise PublicationError(
				"Actions evidence is missing or expired; "
				"use an original-event rerun or a new reviewed PR"
			)
		content = self.api.download(f"/actions/artifacts/{artifact_id}/zip")
		return artifact, unpack(content, digest(artifact.get("digest")))

	def prove(
		self,
		artifact: Mapping[str, object],
		*,
		job: Mapping[str, object],
		repository: str,
		run_id: int,
		run_head_sha: str,
		current_attempt: int,
		provider_repository: str,
		provider_sha: str,
		workflow: str,
		name: str,
		checks: Sequence[str],
	) -> dict[str, object]:
		job_id = positive(job.get("id"), "job ID")
		proof = {
			"run": self.run(run_id),
			"job": dict(job),
			"artifact": dict(artifact),
			"logs": self.api.download(f"/actions/jobs/{job_id}/logs").decode("utf-8"),
		}
		return verify_producer(
			proof,
			repository=repository,
			run_id=run_id,
			run_head_sha=run_head_sha,
			current_attempt=current_attempt,
			provider_repository=provider_repository,
			provider_sha=provider_sha,
			workflow=workflow,
			job_name=str(job.get("name")),
			required_steps=checks,
			artifact_name=name,
		)
