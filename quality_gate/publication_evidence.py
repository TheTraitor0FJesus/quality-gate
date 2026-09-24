"""Actions provenance verification for the shared publisher."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from .publication import PublicationError

MAX_RECORDS = 10000
MAX_TIMESTAMP_LENGTH = 40


def record(value: object, label: str) -> dict[str, object]:
	"""Require an external JSON object before reading its fields."""
	if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
		raise PublicationError(f"{label} must be an object")
	return cast(dict[str, object], value)


def records(value: object, label: str) -> list[dict[str, object]]:
	"""Require a bounded external collection of JSON objects."""
	if not isinstance(value, list) or len(value) > MAX_RECORDS:
		raise PublicationError(f"{label} must be a bounded list")
	return [record(item, label) for item in value]


def positive(value: object, label: str) -> int:
	"""Require an Actions identity or attempt number."""
	if type(value) is not int or value <= 0:
		raise PublicationError(f"{label} must be a positive integer")
	return value


def digest(value: object) -> str:
	"""Read GitHub's SHA-256 digest, preserving its algorithm contract."""
	if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
		raise PublicationError("a GitHub SHA-256 digest is required")
	return value.removeprefix("sha256:")


def _timestamp(value: object) -> datetime:
	if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_LENGTH:
		raise PublicationError("Actions timestamp is missing")
	try:
		result = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError as error:
		raise PublicationError("Actions timestamp is invalid") from error
	if result.tzinfo is None:
		raise PublicationError("Actions timestamp requires a timezone")
	return result


def verify_run(
	run: Mapping[str, object],
	*,
	repository: str,
	run_id: int,
	run_head_sha: str,
	provider_repository: str,
	provider_sha: str,
	workflow: str,
) -> None:
	"""Bind a producer to the actual caller and reusable workflow revision."""
	if (
		run.get("id") != run_id
		or run.get("head_sha") != run_head_sha
		or run.get("path") != workflow
		or run.get("event") not in {"pull_request", "workflow_dispatch"}
		or record(run.get("repository"), "run repository").get("full_name") != repository
		or record(run.get("head_repository"), "head repository").get("full_name") != repository
	):
		raise PublicationError("run does not identify the trusted caller and source")
	references = records(run.get("referenced_workflows"), "referenced workflows")
	expected = f"{provider_repository}/.github/workflows/release-prepare.yml@"
	matching = [item for item in references if str(item.get("path", "")).startswith(expected)]
	if len(matching) != 1 or matching[0].get("sha") != provider_sha:
		raise PublicationError("executed provider revision does not match the accepted helper")
	ref = str(matching[0]["path"]).removeprefix(expected)
	if repository != provider_repository and ref != provider_sha:
		raise PublicationError("external wrapper must use the same full SHA as its helper")


def _verify_job(
	job: Mapping[str, object],
	*,
	run_id: int,
	run_head_sha: str,
	current_attempt: int,
	job_name: str,
	required_steps: Sequence[str],
) -> int:
	attempt = positive(job.get("run_attempt"), "producing attempt")
	if (
		job.get("run_id") != run_id
		or job.get("head_sha") != run_head_sha
		or job.get("name") != job_name
		or job.get("status") != "completed"
		or job.get("conclusion") != "success"
		or attempt > current_attempt
	):
		raise PublicationError("producing job is not a successful trusted attempt of this run")
	steps = records(job.get("steps"), "job steps")
	for name in [*required_steps, "Upload release evidence"]:
		matches = [step for step in steps if step.get("name") == name]
		if len(matches) != 1 or matches[0].get("conclusion") != "success":
			raise PublicationError(f"required producer step is not successful: {name}")
	return attempt


def verify_job(
	job: Mapping[str, object],
	*,
	run_id: int,
	run_head_sha: str,
	current_attempt: int,
	job_name: str,
	required_steps: Sequence[str],
) -> int:
	"""Verify one successful producer record independently of its artifact."""
	return _verify_job(
		job,
		run_id=run_id,
		run_head_sha=run_head_sha,
		current_attempt=current_attempt,
		job_name=job_name,
		required_steps=required_steps,
	)


def verify_producer(
	proof: Mapping[str, object],
	*,
	repository: str,
	run_id: int,
	run_head_sha: str,
	current_attempt: int,
	provider_repository: str,
	provider_sha: str,
	workflow: str,
	job_name: str,
	required_steps: Sequence[str],
	artifact_name: str,
) -> dict[str, object]:
	"""Verify API records and producer logs, independently of uploaded evidence JSON.

	The job record and artifact must identify the same producing attempt. The caller separately
	checks the latest job and only permits an earlier successful artifact when GitHub identifies
	the later record as the same reused execution.
	"""
	verify_run(
		record(proof.get("run"), "run"),
		repository=repository,
		run_id=run_id,
		run_head_sha=run_head_sha,
		provider_repository=provider_repository,
		provider_sha=provider_sha,
		workflow=workflow,
	)
	job = record(proof.get("job"), "job")
	attempt = verify_job(
		job,
		run_id=run_id,
		run_head_sha=run_head_sha,
		current_attempt=current_attempt,
		job_name=job_name,
		required_steps=required_steps,
	)
	artifact = record(proof.get("artifact"), "artifact")
	artifact_run = record(artifact.get("workflow_run"), "artifact run")
	if (
		artifact_run.get("id") != run_id
		or artifact_run.get("head_sha") != run_head_sha
		or artifact.get("expired") is not False
		or artifact.get("name") != artifact_name
		or not _timestamp(job.get("started_at"))
		<= _timestamp(artifact.get("created_at"))
		<= _timestamp(job.get("completed_at"))
	):
		raise PublicationError("artifact is expired or does not identify its producing job")
	artifact_id = positive(artifact.get("id"), "artifact ID")
	sha256 = digest(artifact.get("digest"))
	logs = proof.get("logs")
	if not isinstance(logs, str) or len(logs) > 16 * 1024 * 1024:
		raise PublicationError("producer logs are missing or too large")
	if (
		re.search(rf"Artifact ID(?::| is)? {artifact_id}(?:\s|$)", logs) is None
		or re.search(rf"SHA256 digest of uploaded artifact(?: zip)? is {sha256}(?:\s|$)", logs)
		is None
	):
		raise PublicationError("producer upload receipt does not bind artifact ID and digest")
	return {
		"artifact_id": artifact_id,
		"digest": sha256,
		"job_id": positive(job.get("id"), "job ID"),
		"attempt": attempt,
	}
