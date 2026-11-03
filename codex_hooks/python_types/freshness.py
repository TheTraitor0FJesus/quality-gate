"""Fixed bounded filesystem identities from the public edit-types contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from _shared.quality_events import is_junction
from _shared.quality_feedback import safe_relative

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_ENV_BYTES = 512 * 1024 * 1024
MAX_ENV_RECORDS = 20000
MAX_DIRECTORIES = 4096
MANAGED_SKIPS = frozenset({"__pycache__", "site-packages", "node_modules"})
BASELINE_SKIPS = MANAGED_SKIPS | {"build", "dist", "temp"}
INTERPRETER_ALIAS = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_cancellation: ContextVar[threading.Event | None] = ContextVar("typing_cancellation", default=None)


@contextmanager
def cancellation_scope(event: threading.Event) -> Iterator[None]:
	token = _cancellation.set(event)
	try:
		yield
	finally:
		_cancellation.reset(token)


class FreshnessError(ValueError):
	"""A fixed source-free failure to establish complete current evidence."""


def check_deadline(deadline: float) -> None:
	if (cancel := _cancellation.get()) is not None and cancel.is_set():
		raise FreshnessError("cancelled")
	if monotonic() >= deadline:
		raise FreshnessError("deadline")


def digest(value: object) -> str:
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
	).hexdigest()


def plain_path(path: Path) -> None:
	for candidate in (path, *path.parents):
		if candidate.is_symlink() or is_junction(candidate):
			raise FreshnessError("unsafe_path")


def relative_path(value: object, root: Path, *, directory: bool = False) -> Path:
	if not isinstance(value, str) or (value == "." and not directory):
		raise FreshnessError("invalid_provider_result")
	if safe_relative(value) != value or not value:
		raise FreshnessError("invalid_provider_result")
	path = root / value
	plain_path(path)
	return path


def file_identity(path: Path, deadline: float, limit: int) -> tuple[str, int]:
	check_deadline(deadline)
	metadata = path.stat()
	if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
		raise FreshnessError("unsafe_path")
	if metadata.st_size > limit:
		raise FreshnessError("limits")
	hasher = hashlib.sha256()
	size = 0
	with path.open("rb") as stream:
		while block := stream.read(1024 * 1024):
			check_deadline(deadline)
			size += len(block)
			if size > limit:
				raise FreshnessError("limits")
			hasher.update(block)
	check_deadline(deadline)
	return hasher.hexdigest(), size


def included_name(name: str, coverage: str) -> bool:
	skips = BASELINE_SKIPS if coverage == "baseline" else MANAGED_SKIPS
	return not name.startswith(".") and name not in skips


def directory_entries(path: Path, coverage: str, deadline: float) -> list[dict[str, str]]:
	check_deadline(deadline)
	plain_path(path)
	entries = []
	with os.scandir(path) as stream:
		for item in stream:
			check_deadline(deadline)
			if not included_name(item.name, coverage):
				continue
			plain_path(Path(item.path))
			if item.is_dir(follow_symlinks=False):
				kind = "directory"
			elif item.is_file(follow_symlinks=False):
				kind = "file"
			else:
				raise FreshnessError("unsafe_path")
			entries.append({"name": item.name, "kind": kind})
			if len(entries) > MAX_ENV_RECORDS:
				raise FreshnessError("limits")
	return sorted(entries, key=lambda item: item["name"])


@dataclass
class EnvironmentTree:
	"""Accumulate one bounded tree; all aliases and base Python enter its digest."""

	root: Path
	deadline: float
	records: list[dict[str, Any]] = field(default_factory=list)
	bytes: int = 0
	file_count: int = 0

	def add(self, record: dict[str, Any]) -> None:
		check_deadline(self.deadline)
		self.records.append(record)
		if record["kind"] in {"file", "interpreter"}:
			self.file_count += 1
			self.bytes += record["size"]
		if len(self.records) > MAX_ENV_RECORDS or self.bytes > MAX_ENV_BYTES:
			raise FreshnessError("limits")

	def add_file(self, path: Path, relative: str, *, target: Path | None = None) -> None:
		actual = target or path
		plain_path(actual)
		sha256, size = file_identity(actual, self.deadline, MAX_ENV_BYTES - self.bytes)
		record = {"path": relative, "kind": "file", "sha256": sha256, "size": size}
		if target is not None:
			record.update(kind="interpreter", target=str(target))
		self.add(record)

	def add_link(self, path: Path, relative: str) -> None:
		if os.name == "nt":
			raise FreshnessError("unsafe_path")
		target = path.resolve(strict=True)
		if relative == "lib64" and target == self.root / "lib":
			self.add({"path": relative, "kind": "alias", "target": "lib"})
		elif path.parent == self.root / "bin" and INTERPRETER_ALIAS.fullmatch(path.name):
			if not os.access(target, os.X_OK):
				raise FreshnessError("unsafe_path")
			self.add_file(path, relative, target=target)
		else:
			raise FreshnessError("unsafe_path")

	def walk(self) -> None:
		pending = [self.root]
		while pending:
			directory = pending.pop()
			self.add({"path": directory.relative_to(self.root).as_posix(), "kind": "directory"})
			with os.scandir(directory) as stream:
				for item in stream:
					check_deadline(self.deadline)
					path = Path(item.path)
					relative = path.relative_to(self.root).as_posix()
					if item.is_symlink() or is_junction(path):
						self.add_link(path, relative)
					elif item.is_dir(follow_symlinks=False):
						if item.name != "__pycache__":
							pending.append(path)
					elif not item.name.endswith((".pyc", ".pyo")):
						self.add_file(path, relative)
					if len(pending) + len(self.records) > MAX_ENV_RECORDS:
						raise FreshnessError("limits")


def environment_identity(root: Path, base_python: Path, deadline: float) -> dict[str, Any]:
	plain_path(root)
	plain_path(base_python)
	tree = EnvironmentTree(root, deadline)
	tree.walk()
	if not base_python.is_relative_to(root):
		tree.add_file(base_python, "@base-python", target=base_python)
	tree.records.sort(key=lambda item: item["path"])
	return {
		"path": str(root),
		"sha256": digest(tree.records),
		"file_count": tree.file_count,
		"bytes": tree.bytes,
	}
