"""Bounded Actions artifact transport and verified producer selection."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Mapping, Sequence
from typing import BinaryIO, Protocol

from .publication import PublicationError
from .publication_evidence import digest, positive, record, records, verify_producer

MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_MEMBERS = 100
PAGE_SIZE = 100


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

	def __init__(self, api: ArtifactAPI) -> None:
		self.api = api

	def pages(self, path: str, field: str) -> list[dict[str, object]]:
		result: list[dict[str, object]] = []
		separator = "&" if "?" in path else "?"
		for page in range(1, 101):
			response = record(self.api.get(f"{path}{separator}per_page=100&page={page}"), field)
			items = records(response.get(field), field)
			result.extend(items)
			if len(items) < PAGE_SIZE:
				return result
		raise PublicationError(f"{field} exceeds 10,000 entries; narrow the recovery lookup")

	def jobs(self, run_id: int) -> list[dict[str, object]]:
		return self.pages(f"/actions/runs/{run_id}/jobs?filter=all", "jobs")

	def latest_job(self, run_id: int, name: str) -> dict[str, object]:
		matches = [item for item in self.jobs(run_id) if item.get("name") == name]
		if not matches:
			raise PublicationError(f"required producing job is missing: {name}")
		attempt = max(positive(item.get("run_attempt"), "attempt") for item in matches)
		latest = [item for item in matches if item.get("run_attempt") == attempt]
		if len(latest) != 1:
			raise PublicationError("producing job identity is ambiguous")
		return latest[0]

	def read(self, artifact_id: int) -> tuple[dict[str, object], dict[str, bytes]]:
		positive(artifact_id, "artifact ID")
		artifact = record(self.api.get(f"/actions/artifacts/{artifact_id}"), "artifact")
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
			"run": self.api.get(f"/actions/runs/{run_id}"),
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
