"""Bounded typing inventories and fixed filesystem freshness operations."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .distribution import default_cache_root
from .edit_inputs import SourceFile, read_bounded, regular_path
from .edit_policy import file_sha256, sha256
from .edit_protocol import FIRST_PRINTABLE_CHARACTER, MAX_FILES, MAX_PATH_CHARS, EditError
from .path_safety import is_link_or_reparse, is_reparse_point

MAX_SOURCES = 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_ENVIRONMENT_RECORDS = 20_000
MAX_ENVIRONMENT_BYTES = 512 * 1024 * 1024
UNMANAGED_EXCLUSIONS = {"build", "dist", "temp"}
MYPY_EXCLUSIONS = {"__pycache__", "site-packages", "node_modules"}


class CancellationSignal:
	"""A host-owned source-free signal confined to the selected external cache."""

	def __init__(self, request: dict[str, Any]) -> None:
		cache = Path(request.get("cache_dir", default_cache_root()))
		root = Path(request["root"]).resolve(strict=True)
		for parent in (cache, *cache.parents):
			try:
				metadata = parent.lstat()
			except FileNotFoundError:
				continue
			if is_link_or_reparse(metadata):
				raise EditError("unsafe_path")
		self.cache = cache.resolve()
		self.relative = f"edit-types-events/{request['event_id']}.cancel"
		if (
			self.cache.is_relative_to(root)
			or Path(request["cancel_path"]) != self.cache / self.relative
		):
			raise EditError("unsafe_path")

	def __call__(self) -> bool:
		try:
			metadata = self.cache.lstat()
		except FileNotFoundError:
			metadata = None
		if metadata is not None and is_link_or_reparse(metadata):
			raise EditError("unsafe_path")
		return regular_path(self.cache, self.relative, optional=True).exists()


def assert_deadline(deadline: float) -> None:
	if time.monotonic() >= deadline:
		raise EditError("deadline")


def validate_name(name: str) -> None:
	if (
		not name
		or name in {".", ".."}
		or any(ord(character) < FIRST_PRINTABLE_CHARACTER for character in name)
		or any(character in name for character in "/\\:")
	):
		raise EditError("unsafe_path")


def validate_relative(relative: object) -> str:
	if not isinstance(relative, str) or len(relative) > MAX_PATH_CHARS:
		raise EditError("invalid_request")
	for segment in relative.split("/"):
		validate_name(segment)
	return relative


def request_files(root: Path, entries: list[Any]) -> list[dict[str, Any]]:
	if len(entries) > MAX_FILES:
		raise EditError("limits")
	identities: dict[str, str | None] = {}
	for entry in entries:
		if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
			raise EditError("invalid_request")
		relative = validate_relative(entry["path"])
		expected = entry["sha256"]
		if expected is not None and (
			not isinstance(expected, str) or re.fullmatch("[0-9a-f]{64}", expected) is None
		):
			raise EditError("invalid_request")
		if relative in identities and identities[relative] != expected:
			raise EditError("invalid_request")
		path = regular_path(root, relative, optional=expected is None)
		actual = sha256(read_bounded(path, MAX_SOURCE_BYTES)) if path.exists() else None
		if actual != expected:
			raise EditError("stale_input")
		identities[relative] = expected
	return [
		{"path": path, "input_sha256": digest, "final_sha256": digest}
		for path, digest in sorted(identities.items())
	]


def directory_entries(path: Path, *, managed: bool) -> list[dict[str, str]]:
	entries = []
	for child in path.iterdir():
		if child.name.startswith(".") or child.name in MYPY_EXCLUSIONS:
			continue
		if not managed and child.name in UNMANAGED_EXCLUSIONS and child.is_dir():
			continue
		validate_name(child.name)
		metadata = child.lstat()
		if is_link_or_reparse(metadata):
			raise EditError("unsafe_path")
		if stat.S_ISDIR(metadata.st_mode):
			kind = "directory"
		elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
			kind = "file"
		else:
			raise EditError("unsafe_path")
		entries.append({"name": child.name, "kind": kind})
		if len(entries) > MAX_ENVIRONMENT_RECORDS:
			raise EditError("limits")
	return sorted(entries, key=lambda entry: entry["name"])


def inventory(
	root: Path,
	relative: str,
	*,
	managed: bool,
	deadline: float,
	source_roots: tuple[str, ...] = (),
) -> tuple[list[SourceFile], list[dict[str, Any]]]:
	_roots, missing_reasons = _inventory_roots(relative, source_roots)
	queue = [(value, missing_reasons[value]) for value in reversed(_roots)]
	directories: list[dict[str, Any]] = []
	sources: list[SourceFile] = []
	total_bytes = 0
	record_count = 0
	while queue:
		assert_deadline(deadline)
		relative_directory, missing_reason = queue.pop()
		directory = _safe_directory(root, relative_directory, missing_reason=missing_reason)
		before = directory.lstat()
		entries = directory_entries(directory, managed=managed)
		current = _safe_directory(root, relative_directory, missing_reason=missing_reason)
		after = current.lstat()
		if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
			raise EditError("stale_input")
		directories.append({"path": directory.relative_to(root).as_posix(), "entries": entries})
		record_count += 1 + len(entries)
		if record_count > MAX_ENVIRONMENT_RECORDS:
			raise EditError("limits")
		for entry in entries:
			path = directory / entry["name"]
			if entry["kind"] == "directory":
				queue.append((path.relative_to(root).as_posix(), missing_reason))
			elif path.suffix in {".py", ".pyi"}:
				relative_path = path.relative_to(root).as_posix()
				content = read_bounded(regular_path(root, relative_path), MAX_SOURCE_BYTES)
				total_bytes += len(content)
				if len(sources) >= MAX_SOURCES or total_bytes > MAX_SOURCE_BYTES:
					raise EditError("limits")
				sources.append(SourceFile(relative_path, content, sha256(content)))
	return sorted(sources, key=lambda source: source.path), sorted(
		directories, key=lambda item: item["path"]
	)


def _inventory_roots(
	relative: str, source_roots: tuple[str, ...]
) -> tuple[tuple[str, ...], dict[str, str]]:
	if relative == "." and source_roots:
		raise EditError("configuration")
	if any(value == "." for value in source_roots):
		raise EditError("configuration")
	_roots = (relative, *source_roots)
	canonical_roots = tuple(value.casefold() for value in _roots)
	if len(set(canonical_roots)) != len(canonical_roots) or any(
		left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")
		for index, left in enumerate(canonical_roots)
		for right in canonical_roots[index + 1 :]
	):
		raise EditError("configuration")
	missing_reasons = {
		value: "configuration" if index == 0 else "dependencies"
		for index, value in enumerate(_roots)
	}
	return _roots, missing_reasons


def _safe_directory(root: Path, relative: str, *, missing_reason: str) -> Path:
	if relative == ".":
		parts: tuple[str, ...] = ()
	else:
		validate_relative(relative)
		parts = tuple(relative.split("/"))
	current = root
	try:
		metadata = current.lstat()
	except FileNotFoundError:
		raise EditError(missing_reason) from None
	if is_link_or_reparse(metadata):
		raise EditError("unsafe_path")
	if not stat.S_ISDIR(metadata.st_mode):
		raise EditError(missing_reason)
	for part in parts:
		current = current / part
		try:
			metadata = current.lstat()
		except FileNotFoundError:
			raise EditError(missing_reason) from None
		if is_link_or_reparse(metadata):
			raise EditError("unsafe_path")
		if not stat.S_ISDIR(metadata.st_mode):
			raise EditError(missing_reason)
	return current


def environment_tree(
	root: Path,
	base_python: Path,
	deadline: float,
	*,
	file_identities: dict[Path, tuple[str, int]] | None = None,
) -> dict[str, Any]:
	"""Hash the documented tree, allowing only ordinary POSIX venv aliases."""
	records: list[dict[str, Any]] = []
	files: list[Path] = []
	total_bytes = 0
	for path, metadata in _environment_paths(root, deadline):
		if stat.S_ISREG(metadata.st_mode):
			files.append(path)
			total_bytes += metadata.st_size
		else:
			record = _environment_record(root, path, metadata)
			records.append(record)
			total_bytes += record.get("size", 0)
		if total_bytes > MAX_ENVIRONMENT_BYTES:
			raise EditError("limits")
	# Windows DirEntry omits st_nlink; each file still needs a real stat.
	with ThreadPoolExecutor(max_workers=16) as pool:
		for record in pool.map(lambda path: _environment_file(root, path, deadline), files):
			assert_deadline(deadline)
			records.append(record)
			if file_identities is not None:
				file_identities[root / record["path"]] = record["sha256"], record["size"]
	if not base_python.is_relative_to(root):
		records.append(
			{
				"path": "@base-python",
				"kind": "interpreter",
				"target": str(base_python),
				"sha256": file_sha256(base_python, MAX_ENVIRONMENT_BYTES),
				"size": base_python.stat().st_size,
			}
		)
	total_bytes = sum(record.get("size", 0) for record in records)
	if len(records) > MAX_ENVIRONMENT_RECORDS or total_bytes > MAX_ENVIRONMENT_BYTES:
		raise EditError("limits")
	return {
		"path": str(root),
		"sha256": sha256(
			json.dumps(
				sorted(records, key=lambda record: record["path"]),
				sort_keys=True,
				ensure_ascii=True,
				separators=(",", ":"),
			).encode()
		),
		"file_count": sum("size" in record for record in records),
		"bytes": total_bytes,
	}


def _environment_paths(root: Path, deadline: float) -> Iterator[tuple[Path, os.stat_result]]:
	queue = [(root, root.lstat())]
	count = 1
	while queue:
		assert_deadline(deadline)
		path, metadata = queue.pop()
		yield path, metadata
		if stat.S_ISDIR(metadata.st_mode):
			with os.scandir(path) as children:
				for child in children:
					validate_name(child.name)
					if not _ignored_environment_entry(child):
						queue.append((Path(child.path), child.stat(follow_symlinks=False)))
						count += 1
						if count > MAX_ENVIRONMENT_RECORDS:
							raise EditError("limits")


def _ignored_environment_entry(child: os.DirEntry[str]) -> bool:
	return (child.name == "__pycache__" and child.is_dir(follow_symlinks=False)) or (
		Path(child.name).suffix in {".pyc", ".pyo"} and child.is_file(follow_symlinks=False)
	)


def _environment_record(root: Path, path: Path, metadata: os.stat_result) -> dict[str, Any]:
	if stat.S_ISLNK(metadata.st_mode):
		return _environment_link(root, path)
	if is_reparse_point(metadata):
		raise EditError("unsafe_path")
	if stat.S_ISDIR(metadata.st_mode):
		return {"path": path.relative_to(root).as_posix(), "kind": "directory"}
	raise EditError("unsafe_path")


def _environment_file(root: Path, path: Path, deadline: float) -> dict[str, Any]:
	assert_deadline(deadline)
	metadata = path.lstat()
	if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or is_reparse_point(metadata):
		raise EditError("unsafe_path")
	return {
		"path": path.relative_to(root).as_posix(),
		"kind": "file",
		"sha256": file_sha256(path, MAX_ENVIRONMENT_BYTES),
		"size": metadata.st_size,
	}


def _environment_link(root: Path, path: Path) -> dict[str, Any]:
	relative = path.relative_to(root).as_posix()
	if os.name != "nt" and relative == "lib64" and os.readlink(path) == "lib":
		if (root / "lib").is_dir() and not (root / "lib").is_symlink():
			return {"path": relative, "kind": "alias", "target": "lib"}
	if (
		os.name != "nt"
		and path.parent == root / "bin"
		and re.fullmatch(r"python(?:3(?:\.\d+)?)?", path.name)
	):
		target = path.resolve(strict=True)
		if target.is_file() and os.access(target, os.X_OK):
			return {
				"path": relative,
				"kind": "interpreter",
				"target": str(target),
				"sha256": file_sha256(target, MAX_ENVIRONMENT_BYTES),
				"size": target.stat().st_size,
			}
		raise EditError("runtime")
	raise EditError("unsafe_path")
