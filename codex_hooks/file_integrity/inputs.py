"""Bounded snapshots of explicitly attributed working-tree files."""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from time import monotonic

from _shared.quality_events import MAX_FILES, AttributionError, safe_path

MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_PATH_CHARS = 1024
DIGEST = re.compile(r"^[0-9a-f]{64}$")
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class IntegrityInputError(ValueError):
	"""A bounded, source-free integrity input failure."""

	def __init__(self, reason: str) -> None:
		self.reason = reason
		super().__init__(reason)


def _parse_entry(entry: object) -> tuple[str, str]:
	if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
		raise IntegrityInputError("invalid_request")
	relative, expected = entry["path"], entry["sha256"]
	if (
		not isinstance(relative, str)
		or not relative
		or len(relative) > MAX_PATH_CHARS
		or not isinstance(expected, str)
		or DIGEST.fullmatch(expected) is None
	):
		raise IntegrityInputError("invalid_request")
	return relative, expected


def _regular_path(root: Path, relative: str) -> Path:
	try:
		path, candidate_root = safe_path(relative, root)
	except AttributionError as error:
		raise IntegrityInputError("unsafe_path") from error
	if candidate_root.resolve() != root or path.relative_to(root).as_posix() != relative:
		raise IntegrityInputError("unsafe_path")
	try:
		metadata = path.lstat()
	except OSError as error:
		raise IntegrityInputError("io") from error
	if (
		stat.S_ISLNK(metadata.st_mode)
		or not stat.S_ISREG(metadata.st_mode)
		or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT
		or metadata.st_nlink != 1
	):
		raise IntegrityInputError("unsafe_path")
	return path


def _read_file(path: Path) -> bytes:
	try:
		with path.open("rb") as stream:
			content = stream.read(MAX_FILE_BYTES + 1)
	except OSError as error:
		raise IntegrityInputError("io") from error
	if len(content) > MAX_FILE_BYTES:
		raise IntegrityInputError("limits")
	return content


def snapshot_files(root: Path, value: object, deadline: float) -> list[dict[str, str]]:
	"""Return exact current regular-file hashes for an attributed batch."""
	if not isinstance(value, list) or len(value) > MAX_FILES:
		raise IntegrityInputError("limits")
	if not root.is_absolute():
		raise IntegrityInputError("invalid_request")
	try:
		root = root.resolve(strict=True)
	except OSError as error:
		raise IntegrityInputError("unsafe_path") from error
	selected: dict[str, str] = {}
	total = 0
	for entry in value:
		if monotonic() >= deadline:
			raise IntegrityInputError("deadline")
		relative, expected = _parse_entry(entry)
		content = _read_file(_regular_path(root, relative))
		digest = hashlib.sha256(content).hexdigest()
		if digest != expected:
			raise IntegrityInputError("stale_input")
		if relative in selected:
			if selected[relative] != digest:
				raise IntegrityInputError("invalid_request")
			continue
		total += len(content)
		if total > MAX_TOTAL_BYTES:
			raise IntegrityInputError("limits")
		selected[relative] = digest
	return [{"path": path, "sha256": digest} for path, digest in selected.items()]


def input_identities(entries: list[dict[str, str]]) -> dict[str, str]:
	"""Build the expected exact path-to-digest map for one accepted snapshot."""
	return {entry["path"]: entry["sha256"] for entry in entries}
