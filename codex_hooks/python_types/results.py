"""Validate source-free public edit-types responses before model delivery."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from _shared.quality_feedback import MAX_TYPE_FINDINGS

from python_types import BACKGROUND_SCHEMA
from python_types.coverage import (
	MAX_SOURCES,
	configuration_paths,
	declared_roots,
	is_digest,
	verify_component,
	verify_file_records,
)
from python_types.freshness import FreshnessError, check_deadline, digest, plain_path
from python_types.inputs import TypingInputError, input_identities, snapshot_inputs
from python_types.selections import verify_selection

MAX_RESULT_BYTES = 256 * 1024
MAX_COUNTER = 2**31 - 1
STATUS_EXIT = {"passed": 0, "failed": 1, "not_applicable": 0, "unchecked": 2}
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
		"runtime",
		"tool",
		"configuration",
		"dependencies",
		"tool_output",
		"io",
		"cancelled",
	}
)
RESULT_KEYS = frozenset(
	{
		"schema",
		"check_id",
		"engine_version",
		"status",
		"reason",
		"action",
		"writes_may_have_occurred",
		"root",
		"root_identity",
		"files",
		"configuration",
		"selections",
		"components",
		"freshness_sha256",
		"process_count",
		"elapsed_ms",
	}
)


class TypingResultError(ValueError):
	"""Only fixed public recovery identifiers cross the result trust seam."""


def unchecked(reason: str) -> dict[str, Any]:
	return {
		"schema": 1,
		"check": "types",
		"status": "unchecked",
		"reason": reason,
		"files": [],
		"findings": [],
		"selections": [],
		"writes_may_have_occurred": False,
	}


def require(condition: bool) -> None:
	if not condition:
		raise TypingResultError("invalid_provider_result")


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		require(key not in result)
		result[key] = value
	return result


def envelope(output: str, exit_code: int, engine_release: str) -> dict[str, Any]:
	require(len(output.encode("utf-8")) <= MAX_RESULT_BYTES)
	data = json.loads(output, object_pairs_hook=unique_keys)
	require(isinstance(data, dict))
	background = data.get("schema") == BACKGROUND_SCHEMA
	require(set(data) == RESULT_KEYS | ({"mode", "event_id"} if background else set()))
	require(
		type(data["schema"]) is int
		and data["schema"] in {1, BACKGROUND_SCHEMA}
		and data["check_id"] == "mypy"
	)
	if background:
		require(data["mode"] == "background" and isinstance(data["event_id"], str))
		require(str(uuid.UUID(data["event_id"])) == data["event_id"])
	require(data["engine_version"] == engine_release.removeprefix("v"))
	status = data["status"]
	require(isinstance(status, str) and STATUS_EXIT.get(status) == exit_code)
	require(data["writes_may_have_occurred"] is False)
	for key in ("process_count", "elapsed_ms"):
		require(type(data[key]) is int and 0 <= data[key] <= MAX_COUNTER)
	for key in ("files", "configuration", "selections", "components"):
		require(isinstance(data[key], list))
	if status == "unchecked":
		require(isinstance(data["reason"], str) and data["reason"] in REASONS)
	else:
		require(data["reason"] is None and data["action"] is None)
	return data


def verify_root(data: dict[str, Any], request: dict[str, Any]) -> Path:
	root = Path(request["root"])
	require(root.is_absolute() and str(root.resolve()) == str(root))
	plain_path(root)
	require(data["root"] == str(root))
	identity = data["root_identity"]
	require(isinstance(identity, dict) and set(identity) == {"device", "inode"})
	for key in ("device", "inode"):
		require(type(identity[key]) is int and identity[key] >= 0)
	metadata = root.stat()
	if identity != {"device": metadata.st_dev, "inode": metadata.st_ino}:
		raise FreshnessError("stale_result")
	return root


def verify_request_files(data: dict[str, Any], request: dict[str, Any], deadline: float) -> None:
	expected = input_identities(request["files"])
	seen: dict[str, str | None] = {}
	for item in data["files"]:
		require(isinstance(item, dict) and set(item) == {"path", "input_sha256", "final_sha256"})
		path = item["path"]
		require(isinstance(path, str) and path in expected and path not in seen)
		require(item["input_sha256"] == expected[path] == item["final_sha256"])
		seen[path] = expected[path]
	require(set(seen) == set(expected) and list(seen) == sorted(seen))
	try:
		snapshot_inputs(Path(request["root"]), request["files"], deadline)
	except TypingInputError as error:
		raise FreshnessError("deadline" if str(error) == "deadline" else "stale_result") from error


def freshness_digest(data: dict[str, Any]) -> str:
	components = [
		{key: value for key, value in component.items() if key != "analysis_ms"}
		for component in data["components"]
	]
	return digest(
		{
			**(
				{key: data[key] for key in ("schema", "mode", "event_id")}
				if data["schema"] == BACKGROUND_SCHEMA
				else {}
			),
			**{
				key: data[key]
				for key in ("root", "root_identity", "files", "configuration", "selections")
			},
			"components": components,
		}
	)


def completed_result(
	data: dict[str, Any], request: dict[str, Any], deadline: float
) -> dict[str, Any]:
	check_deadline(deadline)
	root = verify_root(data, request)
	verify_request_files(data, request, deadline)
	configuration = verify_file_records(data["configuration"], root, deadline, nullable=True)
	components = data["components"]
	require(len(components) <= MAX_SOURCES and len(data["selections"]) <= len(components))
	findings = []
	names: set[str] = set()
	paths: set[str] = set()
	roots: dict[str, tuple[str, ...]] = {}
	for component in components:
		scopes = declared_roots(component, root, configuration, deadline)
		findings.extend(
			verify_component(component, root, len(data["selections"]), deadline, scopes)
		)
		roots[component["path"]] = scopes
		require(component["name"] not in names and component["path"] not in paths)
		names.add(component["name"])
		paths.add(component["path"])
	require(sum(component["source_count"] for component in components) <= MAX_SOURCES)
	require(len(findings) <= MAX_TYPE_FINDINGS)
	typed_markers = tuple(int(part) for part in data["engine_version"].split(".")) >= (4, 6, 0)
	require(
		set(configuration)
		== configuration_paths(components, request, roots, typed_markers=typed_markers)
	)
	verify_selections(data, request, deadline, configuration)
	statuses = {component["status"] for component in components}
	status = (
		"failed" if "failed" in statuses else "passed" if "passed" in statuses else "not_applicable"
	)
	require(data["status"] == status)
	require(
		is_digest(data["freshness_sha256"]) and data["freshness_sha256"] == freshness_digest(data)
	)
	check_deadline(deadline)
	return {
		"schema": 1,
		"check": "types",
		"check_id": "mypy",
		"status": status,
		"root": str(root),
		"engine_version": data["engine_version"],
		"files": data["files"],
		"selections": data["selections"],
		"components": components,
		"findings": findings,
		"freshness_sha256": data["freshness_sha256"],
		"process_count": data["process_count"],
		"elapsed_ms": data["elapsed_ms"],
		"writes_may_have_occurred": False,
	}


def verify_selections(
	data: dict[str, Any],
	request: dict[str, Any],
	deadline: float,
	configuration: dict[str, str | None],
) -> None:
	managed = configuration["quality-gate.toml"] is not None
	for component in data["components"]:
		require(component["coverage"] == ("managed" if managed else "baseline"))
		if not managed:
			require(component["path"] == ".")
	used = {
		component["selection"]
		for component in data["components"]
		if component["selection"] is not None
	}
	require(used == set(range(len(data["selections"]))))
	for index, selection in enumerate(data["selections"]):
		owners = [component for component in data["components"] if component["selection"] == index]
		verify_selection(selection, owners, request, deadline)


def validate_result(
	output: str, exit_code: int, request: dict[str, Any], engine_release: str, deadline: float
) -> dict[str, Any]:
	"""Only completed fresh coverage may become a passed or failed host report."""
	try:
		data = envelope(output, exit_code, engine_release)
		if data["schema"] == BACKGROUND_SCHEMA:
			require(all(data[key] == request.get(key) for key in ("schema", "mode", "event_id")))
		elif request.get("schema") == BACKGROUND_SCHEMA:
			require(data["status"] == "unchecked" and data["reason"] == "invalid_request")
		if data["status"] == "unchecked":
			return unchecked(data["reason"])
		return completed_result(data, request, deadline)
	except FreshnessError as error:
		return unchecked(str(error))
	except OSError:
		return unchecked("stale_result")
	except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
		return unchecked("invalid_provider_result")
