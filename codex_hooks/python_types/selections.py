"""Validate policy/runtime provenance and rehash the owned environment tree."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from _shared.quality_feedback import FIRST_CONTROL_CODE
from _shared.quality_settings import RELEASE

from python_types.coverage import is_digest, require
from python_types.freshness import (
	MAX_ENV_BYTES,
	MAX_ENV_RECORDS,
	FreshnessError,
	environment_identity,
	file_identity,
	plain_path,
	relative_path,
)

SELECTION_KEYS = frozenset(
	{
		"policy_release",
		"release_sha256",
		"policy_sha256",
		"mypy_version",
		"mypy_sha256",
		"python",
		"python_version",
		"python_sha256",
		"base_python",
		"base_python_sha256",
		"runtime_fingerprint",
		"environment_sha256",
		"environment",
	}
)
ENVIRONMENT_KEYS = frozenset({"path", "sha256", "file_count", "bytes"})
MAX_RELEASE_FILES = 1024
MAX_RELEASE_MANIFEST = 256 * 1024


def declared_inventory(document: dict[str, Any]) -> dict[str, str]:
	files, tools = document["files"], document.get("tools", [])
	require(isinstance(files, list) and isinstance(tools, list))
	require(0 < len(files) and len(files) + len(tools) <= MAX_RELEASE_FILES)
	declared: dict[str, str] = {}
	for group in (files, tools):
		seen: set[str] = set()
		for item in group:
			require(isinstance(item, dict) and is_digest(item.get("sha256")))
			path = item["path"]
			require(isinstance(path, str) and path not in seen)
			require(path not in declared or declared[path] == item["sha256"])
			seen.add(path)
			declared[path] = item["sha256"]
	return declared


def release_inventory(
	version: str, request: dict[str, Any], deadline: float, *, expected_manifest: str | None = None
) -> dict[str, Any]:
	"""Rehash declared immutable files without interpreting component or policy routing."""
	cache = gate_cache_root(request)
	require(not cache.is_relative_to(Path(request["root"])))
	root = cache / "releases" / version
	plain_path(root)
	manifest = root / "release.toml"
	manifest_digest, _ = file_identity(manifest, deadline, MAX_RELEASE_MANIFEST)
	if expected_manifest is not None and manifest_digest != expected_manifest:
		raise FreshnessError("stale_result")
	with manifest.open("rb") as source:
		content = source.read(MAX_RELEASE_MANIFEST + 1)
	if hashlib.sha256(content).hexdigest() != manifest_digest:
		raise FreshnessError("stale_result")
	document = tomllib.loads(content.decode("utf-8"))["release"]
	require(document["version"] == version)
	identities: dict[str, str] = {}
	total = 0
	for path, expected in declared_inventory(document).items():
		full = relative_path(path, root)
		actual, size = file_identity(full, deadline, MAX_ENV_BYTES - total)
		total += size
		if actual != expected:
			raise FreshnessError("stale_result")
		identities[path] = actual
	return {"sha256": manifest_digest, "files": identities, "tools": document.get("tools", [])}


def verify_policy(selection: dict[str, Any], request: dict[str, Any], deadline: float) -> None:
	inventory = release_inventory(
		selection["policy_release"],
		request,
		deadline,
		expected_manifest=selection["release_sha256"],
	)
	if inventory["sha256"] != selection["release_sha256"]:
		raise FreshnessError("stale_result")
	require(inventory["files"].get("quality_gate/policy/mypy.ini") == selection["policy_sha256"])
	tools = inventory["tools"]
	require(isinstance(tools, list) and all(isinstance(tool, dict) for tool in tools))
	mypy = [tool for tool in tools if tool.get("name") == "mypy"]
	require(len(mypy) == 1)
	require(mypy[0]["version"] == selection["mypy_version"])
	require(
		mypy[0]["sha256"] == selection["mypy_sha256"] == inventory["files"].get(mypy[0]["path"])
	)


def absolute_path(value: object) -> Path:
	if not isinstance(value, str) or any(
		ord(character) < FIRST_CONTROL_CODE for character in value
	):
		raise FreshnessError("invalid_provider_result")
	path = Path(value)
	require(path.is_absolute() and str(path.absolute()) == value and ".." not in path.parts)
	return path


def gate_cache_root(request: dict[str, Any]) -> Path:
	if "cache_dir" in request:
		return absolute_path(request["cache_dir"])
	if os.name == "nt":
		base = os.environ.get("LOCALAPPDATA")
		if not base:
			raise FreshnessError("runtime")
		return Path(base) / "quality-gate"
	base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
	return Path(base) / "quality-gate"


def selection_shape(value: object) -> dict[str, Any]:
	if not isinstance(value, dict) or set(value) != SELECTION_KEYS:
		raise FreshnessError("invalid_provider_result")
	require(
		isinstance(value["policy_release"], str)
		and RELEASE.fullmatch(value["policy_release"]) is not None
	)
	for key in (
		"release_sha256",
		"policy_sha256",
		"mypy_sha256",
		"python_sha256",
		"base_python_sha256",
		"runtime_fingerprint",
		"environment_sha256",
	):
		require(is_digest(value[key]))
	for key, pattern in (("mypy_version", r"\d+\.\d+\.\d+"), ("python_version", r"\d+\.\d+")):
		require(isinstance(value[key], str) and re.fullmatch(pattern, value[key]) is not None)
	environment = value["environment"]
	require(isinstance(environment, dict) and set(environment) == ENVIRONMENT_KEYS)
	require(environment["sha256"] == value["environment_sha256"])
	for key, maximum in (("file_count", MAX_ENV_RECORDS), ("bytes", MAX_ENV_BYTES)):
		require(type(environment[key]) is int and 0 <= environment[key] <= maximum)
	return value


def verify_selection(
	value: object, components: list[dict[str, Any]], request: dict[str, Any], deadline: float
) -> dict[str, Any]:
	selection = selection_shape(value)
	verify_policy(selection, request, deadline)
	root = Path(request["root"])
	environment = absolute_path(selection["environment"]["path"])
	python = absolute_path(selection["python"])
	base_python = absolute_path(selection["base_python"])
	for component in components:
		if component["coverage"] == "baseline":
			require(environment == root / ".venv")
			require(selection["policy_release"] == request["baseline"]["policy_release"])
		else:
			key = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:32]
			require(
				environment
				== gate_cache_root(request) / "runtimes" / key / selection["runtime_fingerprint"]
			)
	require(python == environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
	plain_path(environment)
	actual = environment_identity(environment, base_python, deadline)
	if actual != selection["environment"]:
		raise FreshnessError("stale_result")
	# The standard POSIX venv executable may be an allowed interpreter alias.
	python_digest, _ = file_identity(python.resolve(strict=True), deadline, MAX_ENV_BYTES)
	base_digest, _ = file_identity(base_python, deadline, MAX_ENV_BYTES)
	if (
		python_digest != selection["python_sha256"]
		or base_digest != selection["base_python_sha256"]
	):
		raise FreshnessError("stale_result")
	return selection
