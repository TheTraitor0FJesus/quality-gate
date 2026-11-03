"""Accept only complete, fresh, source-free public edit-secrets responses."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from time import monotonic
from typing import Any

from _shared.quality_events import MAX_FILES, AttributionError, safe_path
from _shared.quality_settings import RELEASE

from secret_scan.inputs import DIGEST, SecretInputError, input_identities, snapshot_files

MAX_RESULT_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_FINDINGS = 200
MAX_COUNTER = 2**31 - 1
CONFIG_PATHS = frozenset({"quality-gate.toml", ".gitleaks.toml", ".gitleaksignore"})
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
		"tool",
		"configuration",
		"coverage",
		"tool_output",
		"io",
	}
)
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class SecretResultError(ValueError):
	"""A fixed public recovery code, never provider-controlled diagnostic text."""


def unchecked(reason: str) -> dict[str, Any]:
	return {
		"schema": 1,
		"check": "secrets",
		"check_id": "secrets.candidate",
		"status": "unchecked",
		"reason": reason,
		"files": [],
		"selections": [],
		"inputs": [],
		"writes_may_have_occurred": False,
	}


def require(condition: bool) -> None:
	if not condition:
		raise SecretResultError("invalid_provider_result")


def is_digest(value: object) -> bool:
	return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		require(key not in result)
		result[key] = value
	return result


def configuration_inputs(value: object, root: Path, deadline: float) -> list[dict[str, Any]]:
	if not isinstance(value, list) or len(value) != len(CONFIG_PATHS):
		raise SecretResultError("invalid_provider_result")
	clean = []
	seen: set[str] = set()
	for item in value:
		require(isinstance(item, dict))
		path = item.get("path")
		require(isinstance(path, str) and path in CONFIG_PATHS and path not in seen)
		seen.add(path)
		digest = item["sha256"]
		require(digest is None or is_digest(digest))
		if monotonic() >= deadline:
			raise SecretResultError("deadline")
		full, _ = safe_path(path, root)
		if digest is None:
			if full.exists():
				raise SecretResultError("stale_result")
		else:
			with full.open("rb") as stream:
				content = stream.read(MAX_CONFIG_BYTES + 1)
			require(len(content) <= MAX_CONFIG_BYTES)
			if hashlib.sha256(content).hexdigest() != digest:
				raise SecretResultError("stale_result")
		clean.append({"path": path, "sha256": digest})
	return clean


def policy_selection(value: object, inputs: dict[str, Any]) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise SecretResultError("invalid_provider_result")
	require(
		isinstance(value.get("policy_release"), str)
		and bool(RELEASE.fullmatch(value["policy_release"]))
	)
	for name in ("release_sha256", "policy_sha256", "scanner_sha256", "waiver_sha256"):
		require(is_digest(value.get(name)))
	require(
		isinstance(value.get("scanner_version"), str)
		and re.fullmatch(r"\d+\.\d+\.\d+", value["scanner_version"]) is not None
	)
	project = inputs[".gitleaks.toml"]
	expected_source = "project" if project is not None else "gitleaks-built-in"
	require(value.get("policy_source") == expected_source)
	require(value["policy_sha256"] == (project or value["scanner_sha256"]))
	return {
		name: value[name]
		for name in (
			"policy_release",
			"release_sha256",
			"policy_source",
			"policy_sha256",
			"scanner_version",
			"scanner_sha256",
			"waiver_sha256",
		)
	}


def file_findings(value: object, path: str) -> list[dict[str, Any]]:
	if not isinstance(value, list) or len(value) > MAX_FINDINGS:
		raise SecretResultError("invalid_provider_result")
	clean = []
	for item in value:
		require(isinstance(item, dict) and item.get("check_id") == "secrets.candidate")
		line = item.get("line")
		require(type(line) is int and 0 < line <= MAX_COUNTER)
		require(item.get("path") == path and item.get("target") == f"{path}:{line}")
		require(is_digest(item.get("fingerprint")))
		require(isinstance(item.get("status"), str) and item["status"] in {"failed", "waived"})
		clean.append(
			{
				name: item[name]
				for name in (
					"check_id",
					"path",
					"target",
					"line",
					"fingerprint",
					"status",
				)
			}
		)
	return clean


def result_file(
	value: object,
	expected: dict[str, str],
	selection: dict[str, Any],
	request: dict[str, Any],
	inputs: dict[str, Any],
) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise SecretResultError("invalid_provider_result")
	path = value.get("path")
	if not isinstance(path, str) or path not in expected:
		raise SecretResultError("invalid_provider_result")
	require(value.get("input_sha256") == expected[path] == value.get("sha256"))
	require(value.get("changed") is False)
	require(type(value.get("selection")) is int and value["selection"] == 0)
	coverage = "managed" if inputs["quality-gate.toml"] is not None else "baseline"
	require(value.get("coverage") == coverage)
	if coverage == "baseline":
		require(selection["policy_release"] == request["baseline"]["policy_release"])
	findings = file_findings(value.get("findings"), path)
	states = {item["status"] for item in findings}
	status = "failed" if "failed" in states else "waived" if states else "passed"
	if expected[path] == EMPTY_DIGEST:
		require(not findings)
		status = "not_applicable"
	require(value.get("status") == status)
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


def completed_result(
	data: dict[str, Any], request: dict[str, Any], engine_release: str, deadline: float
) -> dict[str, Any]:
	root = Path(request["root"]).resolve()
	require(data.get("engine_version") == engine_release.removeprefix("v"))
	require(data.get("root") == str(root) and data.get("reason") is None)
	require(data.get("writes_may_have_occurred") is False)
	expected = input_identities(request["files"])
	require(bool(expected))
	inputs = configuration_inputs(data.get("inputs"), root, deadline)
	configuration = {item["path"]: item["sha256"] for item in inputs}
	selections = data.get("selections")
	if not isinstance(selections, list) or len(selections) != 1:
		raise SecretResultError("invalid_provider_result")
	selection = policy_selection(selections[0], configuration)
	raw_files = data.get("files")
	if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
		raise SecretResultError("invalid_provider_result")
	files = [result_file(item, expected, selection, request, configuration) for item in raw_files]
	require(len(files) == len(expected) and {item["path"] for item in files} == set(expected))
	require(sum(len(item["findings"]) for item in files) <= MAX_FINDINGS)
	states = {item["status"] for item in files}
	status = next(
		state for state in ("failed", "passed", "waived", "not_applicable") if state in states
	)
	require(data["status"] == status)
	for name in ("process_count", "elapsed_ms"):
		require(type(data.get(name)) is int and 0 <= data[name] <= MAX_COUNTER)
	snapshot_files(root, request["files"], deadline)
	return {
		"schema": 1,
		"check": "secrets",
		"check_id": "secrets.candidate",
		"status": status,
		"reason": None,
		"root": str(root),
		"engine_version": data["engine_version"],
		"files": files,
		"selections": [selection],
		"inputs": inputs,
		"process_count": data["process_count"],
		"elapsed_ms": data["elapsed_ms"],
		"writes_may_have_occurred": False,
	}


def validate_result(
	output: str, exit_code: int, request: dict[str, Any], engine_release: str, deadline: float
) -> dict[str, Any]:
	"""Validate the provider response atomically; discard all partial coverage."""
	try:
		require(len(output) <= MAX_RESULT_BYTES and len(output.encode("utf-8")) <= MAX_RESULT_BYTES)
		data = json.loads(output, object_pairs_hook=unique_keys)
		require(isinstance(data, dict) and type(data.get("schema")) is int and data["schema"] == 1)
		require(data.get("check_id") == "secrets.candidate")
		status = data.get("status")
		require(isinstance(status, str) and STATUS_EXIT.get(status) == exit_code)
		if monotonic() >= deadline:
			raise SecretResultError("deadline")
		if status == "unchecked":
			reason = data.get("reason")
			require(isinstance(reason, str) and reason in REASONS)
			return unchecked(reason)
		return completed_result(data, request, engine_release, deadline)
	except SecretInputError as error:
		return unchecked("stale_result" if str(error) == "stale_input" else str(error))
	except SecretResultError as error:
		return unchecked(str(error))
	except OSError:
		return unchecked("io")
	except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, AttributionError):
		return unchecked("invalid_provider_result")
