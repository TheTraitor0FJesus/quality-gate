"""Verify complete source-directory evidence without selecting typing policy."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from _shared.quality_feedback import MAX_TYPE_FINDINGS, MYPY_RULE

from python_types.freshness import (
	MAX_DIRECTORIES,
	MAX_SOURCE_BYTES,
	FreshnessError,
	check_deadline,
	digest,
	directory_entries,
	file_identity,
	relative_path,
)
from python_types.inputs import DIGEST

MAX_SOURCES = 1024
MAX_COUNTER = 2**31 - 1
MAX_NAME_CHARS = 128
ROOT_CONFIGURATION = frozenset(
	{
		"quality-gate.toml",
		"pyproject.toml",
		"mypy.ini",
		".mypy.ini",
		"setup.cfg",
		"requirements.txt",
		"requirements-dev.txt",
		"uv.lock",
		"poetry.lock",
		"Pipfile",
		"Pipfile.lock",
	}
)
DIRECTORY_CONFIGURATION = frozenset(
	{"quality-gate.toml", "pyproject.toml", "mypy.ini", ".mypy.ini", "setup.cfg"}
)
COMPONENT_KEYS = frozenset(
	{
		"name",
		"path",
		"coverage",
		"status",
		"reason",
		"selection",
		"inventory",
		"directories",
		"scope_sha256",
		"dependency_inputs",
		"diagnostics",
		"source_count",
		"import_count",
		"analysis_ms",
	}
)


def require(condition: bool) -> None:
	if not condition:
		raise FreshnessError("invalid_provider_result")


def is_digest(value: object) -> bool:
	return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def file_records(value: object, *, nullable: bool = False) -> dict[str, str | None]:
	if not isinstance(value, list) or len(value) > MAX_DIRECTORIES:
		raise FreshnessError("invalid_provider_result")
	result: dict[str, str | None] = {}
	for item in value:
		require(isinstance(item, dict) and set(item) == {"path", "sha256"})
		path, sha256 = item["path"], item["sha256"]
		require(isinstance(path, str) and path not in result)
		require(is_digest(sha256) or (nullable and sha256 is None))
		result[path] = sha256
	require(list(result) == sorted(result))
	return result


def verify_file_records(
	value: object, root: Path, deadline: float, *, nullable: bool = False
) -> dict[str, str | None]:
	identities = file_records(value, nullable=nullable)
	total = 0
	for path, sha256 in identities.items():
		check_deadline(deadline)
		full = relative_path(path, root)
		if sha256 is None:
			if full.exists():
				raise FreshnessError("stale_result")
			continue
		actual, size = file_identity(full, deadline, MAX_SOURCE_BYTES - total)
		total += size
		if actual != sha256:
			raise FreshnessError("stale_result")
	return identities


def verify_directories(
	value: object,
	root: Path,
	component: dict[str, Any],
	deadline: float,
	roots: tuple[str, ...],
) -> dict[str, list[dict[str, str]]]:
	if not isinstance(value, list) or not 0 < len(value) <= MAX_DIRECTORIES:
		raise FreshnessError("invalid_provider_result")
	directories: dict[str, list[dict[str, str]]] = {}
	for record in value:
		require(isinstance(record, dict) and set(record) == {"path", "entries"})
		path = record["path"]
		full = relative_path(path, root, directory=True)
		require(path not in directories and any(full.is_relative_to(root / base) for base in roots))
		entries = record["entries"]
		require(isinstance(entries, list))
		actual = directory_entries(full, component["coverage"], deadline)
		if entries != actual:
			raise FreshnessError("stale_result")
		directories[path] = actual
	require(list(directories) == sorted(directories))
	children = set(roots)
	for path, entries in directories.items():
		for entry in entries:
			if entry["kind"] == "directory":
				children.add((PurePosixPath(path) / entry["name"]).as_posix())
	require(set(directories) == children)
	return directories


def verify_inventory(
	component: dict[str, Any], root: Path, deadline: float, roots: tuple[str, ...]
) -> dict[str, str | None]:
	directories = verify_directories(component["directories"], root, component, deadline, roots)
	inventory = verify_file_records(component["inventory"], root, deadline)
	require(len(inventory) <= MAX_SOURCES)
	expected = {
		(PurePosixPath(path) / entry["name"]).as_posix()
		for path, entries in directories.items()
		for entry in entries
		if entry["kind"] == "file" and Path(entry["name"]).suffix in {".py", ".pyi"}
	}
	require(set(inventory) == expected)
	require(component["source_count"] == len(inventory))
	require(
		component["scope_sha256"]
		== digest({"inventory": component["inventory"], "directories": component["directories"]})
	)
	return inventory


def diagnostics(value: object, inventory: dict[str, str | None]) -> list[dict[str, Any]]:
	if not isinstance(value, list) or len(value) > MAX_TYPE_FINDINGS:
		raise FreshnessError("invalid_provider_result")
	clean = []
	for item in value:
		require(isinstance(item, dict) and set(item) == {"path", "line", "column", "code"})
		require(isinstance(item["path"], str) and item["path"] in inventory)
		for key in ("line", "column"):
			require(type(item[key]) is int and 0 < item[key] <= MAX_COUNTER)
		require(isinstance(item["code"], str) and MYPY_RULE.fullmatch(item["code"]) is not None)
		clean.append(
			{
				"path": item["path"],
				"line": item["line"],
				"column": item["column"],
				"rule": item["code"],
			}
		)
	return clean


def declared_roots(
	component: dict[str, Any], root: Path, configuration: dict[str, str | None], deadline: float
) -> tuple[str, ...]:
	"""Resolve coverage only from the independently verified manifest bytes."""
	if component["coverage"] == "baseline":
		return (component["path"],)
	check_deadline(deadline)
	with (root / "quality-gate.toml").open("rb") as stream:
		content = stream.read(256 * 1024 + 1)
	require(len(content) <= 256 * 1024)
	if hashlib.sha256(content).hexdigest() != configuration.get("quality-gate.toml"):
		raise FreshnessError("stale_result")
	document = tomllib.loads(content.decode("utf-8"))
	declarations = document.get("python", [])
	require(isinstance(declarations, list))
	matches = [
		item
		for item in declarations
		if isinstance(item, dict)
		and (item.get("name"), item.get("path")) == (component["name"], component["path"])
	]
	require(len(matches) == 1)
	sources = matches[0].get("source_roots", [])
	require(isinstance(sources, list) and len(sources) < MAX_DIRECTORIES)
	for source in sources:
		relative_path(source, root, directory=True)
		require(source != ".")
	roots = (component["path"], *sources)
	canonical = [value.casefold() for value in roots]
	require(len(set(canonical)) == len(canonical))
	require(
		all(
			not (left.startswith(f"{right}/") or right.startswith(f"{left}/"))
			for index, left in enumerate(canonical)
			for right in canonical[index + 1 :]
		)
	)
	return roots


def verify_component(
	value: object,
	root: Path,
	selection_count: int,
	deadline: float,
	roots: tuple[str, ...],
) -> list[dict[str, Any]]:
	if not isinstance(value, dict) or set(value) != COMPONENT_KEYS:
		raise FreshnessError("invalid_provider_result")
	require(isinstance(value["name"], str) and 0 < len(value["name"]) <= MAX_NAME_CHARS)
	relative_path(value["path"], root, directory=True)
	require(value["coverage"] in {"managed", "baseline"})
	for key in ("source_count", "import_count", "analysis_ms"):
		require(type(value[key]) is int and 0 <= value[key] <= MAX_COUNTER)
	if value["status"] == "not_applicable":
		require(value["reason"] == "typecheck_disabled" and value["selection"] is None)
		require(
			all(
				value[key] == []
				for key in ("inventory", "directories", "dependency_inputs", "diagnostics")
			)
		)
		require(all(value[key] == 0 for key in ("source_count", "import_count", "analysis_ms")))
		return []
	require(value["status"] in {"passed", "failed"} and value["reason"] is None)
	require(type(value["selection"]) is int and 0 <= value["selection"] < selection_count)
	inventory = verify_inventory(value, root, deadline, roots)
	verify_file_records(value["dependency_inputs"], root, deadline)
	findings = diagnostics(value["diagnostics"], inventory)
	require((value["status"] == "failed") == bool(findings))
	return findings


def configuration_paths(
	components: list[dict[str, Any]],
	request: dict[str, Any],
	roots: dict[str, tuple[str, ...]],
	*,
	typed_markers: bool,
) -> set[str]:
	paths = set(ROOT_CONFIGURATION)
	for component in components:
		for record in component["directories"]:
			directory = PurePosixPath(record["path"])
			for ancestor in (directory, *directory.parents):
				paths.update((ancestor / name).as_posix() for name in DIRECTORY_CONFIGURATION)
			if typed_markers and {"name": "py.typed", "kind": "file"} in record["entries"]:
				paths.add((directory / "py.typed").as_posix())
		if component["status"] != "not_applicable":
			markers = (
				("__init__.py", "__init__.pyi", "py.typed")
				if typed_markers
				else ("__init__.py", "__init__.pyi")
			)
			for scope in roots[component["path"]]:
				for ancestor in PurePosixPath(scope).parents:
					paths.update((ancestor / name).as_posix() for name in markers)
		paths.update(record["path"] for record in component["dependency_inputs"])
	paths.update(
		item["path"] for item in request["files"] if Path(item["path"]).name in ROOT_CONFIGURATION
	)
	return paths
