"""Read-only exact-source product verification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_PRODUCERS = 64
MAX_EVIDENCE_REFERENCE = 512
MAX_EVIDENCE_FILES = 2
MAX_JSON_BYTES = 65536
MAX_JOB_STEPS = 256


class VerificationError(ValueError):
	"""Verification evidence is incomplete or invalid."""


def _object(value: object, keys: set[str], label: str) -> dict[str, Any]:
	if not isinstance(value, dict) or set(value) != keys:
		raise VerificationError(f"invalid {label} fields")
	return value


def _name(value: object) -> bool:
	return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _requirements(request: dict[str, Any]) -> dict[tuple[str, str], str]:
	request = _object(
		request, {"schema", "repository", "base_sha", "head_sha", "obligations"}, "request"
	)
	if request["schema"] != 1 or type(request["schema"]) is not int:
		raise VerificationError("unsupported request schema")
	if not isinstance(request["repository"], str) or _REPO.fullmatch(request["repository"]) is None:
		raise VerificationError("invalid repository")
	for key in ("base_sha", "head_sha"):
		if not isinstance(request[key], str) or _SHA.fullmatch(request[key]) is None:
			raise VerificationError(f"invalid {key}")
	obligations = request["obligations"]
	if not isinstance(obligations, list) or not 1 <= len(obligations) <= MAX_PRODUCERS:
		raise VerificationError("invalid obligations")
	required: dict[tuple[str, str], str] = {}
	artifact_names: set[str] = set()
	for obligation in obligations:
		entry = _object(obligation, {"producer", "platform", "kind"}, "obligation")
		producer, platform, kind = entry["producer"], entry["platform"], entry["kind"]
		if not _name(producer) or not _name(platform) or kind not in ("artifact", "checks-only"):
			raise VerificationError("invalid obligation")
		identity = (producer, platform)
		if identity in required:
			raise VerificationError("duplicate obligation")
		name = _artifact_name(identity, 1)
		if name in artifact_names:
			raise VerificationError("obligations have colliding artifact names")
		artifact_names.add(name)
		required[identity] = kind
	return required


def _artifact_name(identity: tuple[str, str], attempt: int) -> str:
	return f"product-verification-{identity[0]}-{identity[1]}-{attempt}"


def _result_identity(
	entry: dict[str, Any], request: dict[str, Any], required: dict[tuple[str, str], str]
) -> tuple[str, str]:
	identity = (entry["producer"], entry["platform"])
	if not all(_name(value) for value in identity):
		raise VerificationError("invalid producer or platform")
	if identity not in required:
		raise VerificationError("unexpected producer or platform")
	if (
		type(entry["schema"]) is not int
		or entry["schema"] != 1
		or any(entry[key] != request[key] for key in ("repository", "base_sha", "head_sha"))
	):
		raise VerificationError("wrong source or request identity")
	if (
		entry["kind"] != required[identity]
		or entry["status"] != "passed"
		or entry["applicability"] != "applicable"
	):
		raise VerificationError("incomplete or incompatible producer result")
	if (
		not isinstance(entry["evidence"], str)
		or not 1 <= len(entry["evidence"]) <= MAX_EVIDENCE_REFERENCE
	):
		raise VerificationError("invalid evidence reference")
	return identity


def _validate_artifact(entry: dict[str, Any], request: dict[str, Any]) -> None:
	if entry["kind"] == "artifact":
		artifact = _object(entry["artifact"], {"name", "sha256", "source_sha"}, "artifact")
		if (
			not _name(artifact["name"])
			or not isinstance(artifact["sha256"], str)
			or _DIGEST.fullmatch(artifact["sha256"]) is None
			or artifact["source_sha"] != request["head_sha"]
		):
			raise VerificationError("invalid or wrong-source artifact")
	elif entry["artifact"] is not None:
		raise VerificationError("checks-only result cannot provide an artifact")


def aggregate(request: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
	"""Validate every required producer result for one exact PR head."""
	required = _requirements(request)
	if not isinstance(results, list) or len(results) > MAX_PRODUCERS:
		raise VerificationError("invalid results")
	seen: set[tuple[str, str]] = set()
	for result in results:
		entry = _object(
			result,
			{
				"schema",
				"repository",
				"base_sha",
				"head_sha",
				"producer",
				"platform",
				"kind",
				"status",
				"applicability",
				"artifact",
				"evidence",
			},
			"result",
		)
		identity = _result_identity(entry, request, required)
		if identity in seen:
			raise VerificationError("duplicate producer evidence")
		seen.add(identity)
		_validate_artifact(entry, request)
	if seen != set(required):
		raise VerificationError("missing required producer or platform")
	return {
		"schema": 1,
		"repository": request["repository"],
		"base_sha": request["base_sha"],
		"head_sha": request["head_sha"],
		"status": "content-verified",
		"results": results,
	}


def _check_artifact_bytes(path: Path, expected: object) -> None:
	if path.stat().st_size > 512 * 1024 * 1024:
		raise VerificationError("oversized artifact")
	digest = hashlib.sha256()
	with path.open("rb") as stream:
		for chunk in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(chunk)
	if digest.hexdigest() != expected:
		raise VerificationError("artifact digest mismatch")


def _read_evidence(child: Path) -> dict[str, Any]:
	if not child.is_dir() or child.is_symlink():
		raise VerificationError("invalid evidence directory")
	files = list(child.iterdir())
	if len(files) > MAX_EVIDENCE_FILES or any(
		not item.is_file() or item.is_symlink() for item in files
	):
		raise VerificationError("invalid evidence files")
	metadata = child / "verification.json"
	try:
		if metadata.stat().st_size > MAX_JSON_BYTES:
			raise VerificationError("evidence exceeds size limit")
		result = json.loads(metadata.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError) as error:
		raise VerificationError("evidence is unreadable") from error
	if not isinstance(result, dict):
		raise VerificationError("invalid evidence")
	artifact = result.get("artifact")
	if isinstance(artifact, dict) and _name(artifact.get("name")):
		path = child / artifact["name"]
		if set(files) != {metadata, path}:
			raise VerificationError("missing artifact")
		_check_artifact_bytes(path, artifact.get("sha256"))
	elif set(files) != {metadata}:
		raise VerificationError("unexpected evidence file")
	return result


def aggregate_directory(request: dict[str, Any], directory: Path) -> dict[str, Any]:
	"""Read bounded evidence and validate the downloaded artifact bytes."""
	if not directory.is_dir():
		raise VerificationError("missing evidence directory")
	children = list(directory.iterdir())
	if len(children) > MAX_PRODUCERS:
		raise VerificationError("too many evidence directories")
	return aggregate(request, [_read_evidence(child) for child in children])


def _verify_artifact_transport(
	artifact: dict[str, Any],
	job: dict[str, Any],
	logs: object,
	identity: tuple[str, str],
	run_id: int,
	attempt: int,
	head_sha: str,
) -> None:
	artifact_run = artifact.get("workflow_run")
	artifact_id, digest = artifact.get("id"), artifact.get("digest")
	if (
		type(artifact_id) is not int
		or artifact_id <= 0
		or not isinstance(digest, str)
		or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
		or artifact.get("name") != _artifact_name(identity, attempt)
		or artifact.get("expired") is not False
		or not isinstance(artifact_run, dict)
		or artifact_run.get("id") != run_id
		or artifact_run.get("head_sha") != head_sha
	):
		raise VerificationError("artifact transport identity mismatch")
	try:
		start, created, end = [
			datetime.fromisoformat(str(value).replace("Z", "+00:00"))
			for value in (
				job.get("started_at"),
				artifact.get("created_at"),
				job.get("completed_at"),
			)
		]
		if (
			any(value.tzinfo is None for value in (start, created, end))
			or not start <= created <= end
		):
			raise ValueError
	except ValueError as error:
		raise VerificationError("artifact creation time mismatch") from error
	if (
		not isinstance(logs, str)
		or len(logs) > 16 * 1024 * 1024
		or re.search(rf"Artifact ID(?::| is)? {artifact_id}(?:\s|$)", logs) is None
		or re.search(rf"SHA256 digest of uploaded artifact(?: zip)? is {digest[7:]}(?:\s|$)", logs)
		is None
	):
		raise VerificationError("missing upload receipt")


def _upload_step_number(job: dict[str, Any]) -> int:
	steps = job.get("steps")
	if not isinstance(steps, list) or len(steps) > MAX_JOB_STEPS:
		raise VerificationError("invalid producer steps")
	step_numbers: list[int] = []
	for name in ("Verify product", "Upload verification evidence"):
		matching = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
		if len(matching) != 1 or matching[0].get("conclusion") != "success":
			raise VerificationError("missing successful producer step")
		number = matching[0].get("number")
		if type(number) is not int or number <= 0:
			raise VerificationError("invalid producer step number")
		step_numbers.append(number)
	verify_number, upload_number = step_numbers
	upload_matches = sum(
		isinstance(step, dict) and step.get("number") == upload_number for step in steps
	)
	if verify_number >= upload_number or upload_matches != 1:
		raise VerificationError("ambiguous producer step number")
	return upload_number


def verify_transport(
	request: dict[str, Any],
	results: list[dict[str, Any]],
	proofs: list[dict[str, Any]],
	*,
	run_id: int,
	attempt: int,
	workflow: str,
) -> dict[str, Any]:
	"""Bind results to GitHub records and the log retrieved for the upload step."""
	aggregated = aggregate(request, results)
	if type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
		raise VerificationError("invalid run identity")
	if not isinstance(proofs, list) or len(proofs) != len(results):
		raise VerificationError("missing transport proof")
	by_identity = {(item["producer"], item["platform"]): item for item in results}
	seen: set[tuple[str, str]] = set()
	for proof in proofs:
		proof = _object(
			proof, {"producer", "platform", "run", "job", "artifact", "upload_log"}, "proof"
		)
		identity = (proof["producer"], proof["platform"])
		if (
			not all(_name(item) for item in identity)
			or identity not in by_identity
			or identity in seen
		):
			raise VerificationError("unexpected or duplicate transport proof")
		seen.add(identity)
		run, job, artifact = proof["run"], proof["job"], proof["artifact"]
		if not all(isinstance(item, dict) for item in (run, job, artifact)):
			raise VerificationError("invalid Actions records")
		repository = run.get("repository")
		if (
			run.get("id") != run_id
			or run.get("run_attempt") != attempt
			or run.get("event") != "pull_request"
			or run.get("path") != workflow
			or run.get("head_sha") != request["head_sha"]
			or not isinstance(repository, dict)
			or repository.get("full_name") != request["repository"]
		):
			raise VerificationError("run identity mismatch")
		job_id = job.get("id")
		if (
			type(job_id) is not int
			or job_id <= 0
			or job.get("run_id") != run_id
			or job.get("run_attempt") != attempt
			or job.get("head_sha") != request["head_sha"]
			or job.get("name") != f"Verify product {identity[0]} {identity[1]}"
			or job.get("status") != "completed"
			or job.get("conclusion") != "success"
		):
			raise VerificationError("producer job is incomplete or mismatched")
		upload_number = _upload_step_number(job)
		upload_log = _object(proof["upload_log"], {"job_id", "step_number", "text"}, "upload log")
		if (
			type(upload_log["job_id"]) is not int
			or upload_log["job_id"] != job_id
			or type(upload_log["step_number"]) is not int
			or upload_log["step_number"] != upload_number
		):
			raise VerificationError("upload log step identity mismatch")
		_verify_artifact_transport(
			artifact, job, upload_log["text"], identity, run_id, attempt, request["head_sha"]
		)
		if by_identity[identity]["evidence"] != f"run/{run_id}/attempt/{attempt}":
			raise VerificationError("result run identity mismatch")
	return {**aggregated, "status": "transport-verified"}


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--request", type=Path, required=True)
	parser.add_argument("--evidence-dir", type=Path, required=True)
	options = parser.parse_args()
	try:
		if options.request.stat().st_size > MAX_JSON_BYTES:
			raise VerificationError("request exceeds size limit")
		request = json.loads(options.request.read_text(encoding="utf-8"))
		result = aggregate_directory(request, options.evidence_dir)
	except (OSError, UnicodeError, json.JSONDecodeError, VerificationError) as error:
		sys.stderr.write(f"product verification: unchecked - {error}\n")
		return 2
	sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
