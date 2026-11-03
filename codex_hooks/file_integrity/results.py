"""Validate complete, fresh and source-free edit-integrity responses."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import tomllib
from pathlib import Path
from time import monotonic
from typing import Any

from _shared.quality_events import MAX_FILES, AttributionError, safe_path
from _shared.quality_settings import RELEASE

from file_integrity.inputs import (
	IntegrityInputError,
	input_identities,
	snapshot_files,
)

CHECK_ID = "repository.git.conflict_markers"
ACTION = "Resolve the merge conflict markers in this touched file."
MAX_RESULT_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_FINDINGS = 200
MAX_COUNTER = 2**31 - 1
PROCESS_COUNT = 2
STATUS_EXIT = {"passed": 0, "failed": 1, "waived": 0, "not_applicable": 0, "unchecked": 2}
REASONS = frozenset(
	{
		"invalid_request",
		"deadline",
		"unsafe_path",
		"limits",
		"stale_input",
		"manifest",
		"unsupported_policy",
		"release",
		"tool_output",
		"io",
	}
)
DIGEST = re.compile(r"^[0-9a-f]{64}$")
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class IntegrityResultError(ValueError):
	"""A fixed error for malformed provider output or stale inputs."""


def require(condition: bool) -> None:
	if not condition:
		raise IntegrityResultError("invalid_provider_result")


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		require(key not in result)
		result[key] = value
	return result


def _manifest_path(root: Path) -> Path | None:
	try:
		path, candidate_root = safe_path("quality-gate.toml", root)
	except AttributionError as error:
		raise IntegrityResultError("stale_input") from error
	if candidate_root.resolve() != root:
		raise IntegrityResultError("stale_input")
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return None
	except OSError as error:
		raise IntegrityResultError("io") from error
	if (
		stat.S_ISLNK(metadata.st_mode)
		or not stat.S_ISREG(metadata.st_mode)
		or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT
		or metadata.st_nlink != 1
	):
		raise IntegrityResultError("stale_input")
	return path


def _read_manifest(path: Path, digest: str) -> bytes:
	try:
		with path.open("rb") as stream:
			content = stream.read(MAX_CONFIG_BYTES + 1)
	except OSError as error:
		raise IntegrityResultError("io") from error
	require(len(content) <= MAX_CONFIG_BYTES)
	if hashlib.sha256(content).hexdigest() != digest:
		raise IntegrityResultError("stale_input")
	return content


def _manifest_bytes(value: object, root: Path, deadline: float) -> bytes | None:
	if not isinstance(value, list) or len(value) != 1:
		raise IntegrityResultError("invalid_provider_result")
	item = value[0]
	if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
		raise IntegrityResultError("invalid_provider_result")
	require(item["path"] == "quality-gate.toml")
	digest = item["sha256"]
	require(digest is None or isinstance(digest, str) and DIGEST.fullmatch(digest) is not None)
	if monotonic() >= deadline:
		raise IntegrityResultError("deadline")
	path = _manifest_path(root)
	if path is None:
		if digest is None:
			return None
		raise IntegrityResultError("stale_input")
	if digest is None:
		raise IntegrityResultError("stale_input")
	return _read_manifest(path, digest)


def _expected_policy(manifest: bytes | None, request: dict[str, Any]) -> tuple[str, str]:
	if manifest is None:
		baseline = request.get("baseline")
		if not isinstance(baseline, dict) or set(baseline) != {"policy_release"}:
			raise IntegrityResultError("invalid_provider_result")
		version = baseline["policy_release"]
		coverage = "baseline"
	else:
		try:
			parsed = tomllib.loads(manifest.decode("utf-8"))
		except (tomllib.TOMLDecodeError, UnicodeError) as error:
			raise IntegrityResultError("stale_input") from error
		quality = parsed.get("quality")
		version = quality.get("policy_release") if isinstance(quality, dict) else None
		coverage = "managed"
	if not isinstance(version, str) or not RELEASE.fullmatch(version):
		raise IntegrityResultError("invalid_provider_result")
	return version, coverage


def _selection(value: object, expected_policy: str) -> dict[str, str]:
	if not isinstance(value, dict) or set(value) != {
		"policy_release",
		"release_sha256",
		"waiver_sha256",
	}:
		raise IntegrityResultError("invalid_provider_result")
	require(value.get("policy_release") == expected_policy)
	for name in ("release_sha256", "waiver_sha256"):
		require(isinstance(value.get(name), str) and DIGEST.fullmatch(value[name]) is not None)
	return {name: value[name] for name in ("policy_release", "release_sha256", "waiver_sha256")}


def _findings(value: object, path: str) -> list[dict[str, Any]]:
	if not isinstance(value, list) or len(value) > MAX_FINDINGS:
		raise IntegrityResultError("invalid_provider_result")
	clean = []
	seen: set[tuple[str, int]] = set()
	for item in value:
		if not isinstance(item, dict) or set(item) != {
			"check_id",
			"path",
			"line",
			"status",
			"action",
		}:
			raise IntegrityResultError("invalid_provider_result")
		line = item.get("line")
		require(item.get("check_id") == CHECK_ID and item.get("path") == path)
		if type(line) is not int or not 0 < line <= MAX_COUNTER:
			raise IntegrityResultError("invalid_provider_result")
		require(item.get("status") == "failed" and item.get("action") == ACTION)
		identity = (path, line)
		require(identity not in seen)
		seen.add(identity)
		clean.append({"path": path, "line": line})
	return clean


def _result_file(
	value: object,
	expected: dict[str, str],
	coverage: str,
) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise IntegrityResultError("invalid_provider_result")
	if set(value) != {
		"path",
		"status",
		"coverage",
		"input_sha256",
		"sha256",
		"changed",
		"selection",
		"findings",
	}:
		raise IntegrityResultError("invalid_provider_result")
	path = value.get("path")
	if not isinstance(path, str) or path not in expected:
		raise IntegrityResultError("invalid_provider_result")
	require(value.get("input_sha256") == expected[path] == value.get("sha256"))
	require(value.get("changed") is False)
	require(type(value.get("selection")) is int and value["selection"] == 0)
	require(value.get("coverage") == coverage)
	findings = _findings(value.get("findings"), path)
	status = value.get("status")
	if findings:
		require(status == "failed")
	else:
		require(status in {"passed", "waived"})
	return {
		"path": path,
		"status": status,
		"coverage": coverage,
		"input_sha256": expected[path],
		"sha256": expected[path],
		"changed": False,
		"selection": 0,
		"findings": findings,
	}


def _overall(files: list[dict[str, Any]]) -> str:
	states = {item["status"] for item in files}
	return next(state for state in ("failed", "waived", "passed") if state in states)


def _completed(
	data: dict[str, Any], request: dict[str, Any], engine_release: str, deadline: float
) -> dict[str, Any]:
	root = Path(request["root"]).resolve(strict=True)
	require(data.get("engine_version") == engine_release.removeprefix("v"))
	require(data.get("root") == str(root) and data.get("reason") is None)
	require(data.get("writes_may_have_occurred") is False)
	expected = input_identities(request["files"])
	require(bool(expected) and len(expected) <= MAX_FILES)
	manifest = _manifest_bytes(data.get("inputs"), root, deadline)
	expected_policy, coverage = _expected_policy(manifest, request)
	selections = data.get("selections")
	if not isinstance(selections, list) or len(selections) != 1:
		raise IntegrityResultError("invalid_provider_result")
	_selection(selections[0], expected_policy)
	raw_files = data.get("files")
	if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
		raise IntegrityResultError("invalid_provider_result")
	files = [_result_file(item, expected, coverage) for item in raw_files]
	require(len(files) == len(expected) and {item["path"] for item in files} == set(expected))
	findings = [finding for item in files for finding in item["findings"]]
	require(
		len(findings) <= MAX_FINDINGS
		and data.get("findings")
		== [
			{"check_id": CHECK_ID, **finding, "status": "failed", "action": ACTION}
			for finding in findings
		]
	)
	status = _overall(files)
	require(data.get("status") == status)
	require(type(data.get("process_count")) is int and data["process_count"] == PROCESS_COUNT)
	if monotonic() >= deadline:
		raise IntegrityResultError("deadline")
	snapshot_files(root, request["files"], deadline)
	if _manifest_bytes(data.get("inputs"), root, deadline) != manifest:
		raise IntegrityResultError("stale_input")
	return {"status": status, "reason": None, "findings": findings}


def validate_result(
	output: str, exit_code: int, request: dict[str, Any], engine_release: str, deadline: float
) -> dict[str, Any]:
	"""Validate the provider response atomically; discard every partial result."""
	try:
		require(isinstance(output, str) and len(output) <= MAX_RESULT_BYTES)
		require(len(output.encode("utf-8")) <= MAX_RESULT_BYTES)
		data = json.loads(output, object_pairs_hook=unique_keys)
		require(isinstance(data, dict) and type(data.get("schema")) is int and data["schema"] == 1)
		require(data.get("check_id") == CHECK_ID)
		require(data.get("engine_version") == engine_release.removeprefix("v"))
		status = data.get("status")
		require(isinstance(status, str) and STATUS_EXIT.get(status) == exit_code)
		if monotonic() >= deadline:
			raise IntegrityResultError("deadline")
		if status == "unchecked":
			reason = data.get("reason")
			require(isinstance(reason, str) and reason in REASONS)
			require(data.get("writes_may_have_occurred") is False)
			require(data.get("files") == [] and data.get("selections") == [])
			return {"status": "unchecked", "reason": reason, "findings": []}
		return _completed(data, request, engine_release, deadline)
	except IntegrityInputError as error:
		return {"status": "unchecked", "reason": error.reason, "findings": []}
	except IntegrityResultError as error:
		return {"status": "unchecked", "reason": str(error), "findings": []}
	except OSError:
		return {"status": "unchecked", "reason": "io", "findings": []}
	except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, AttributionError):
		return {"status": "unchecked", "reason": "invalid_provider_result", "findings": []}
