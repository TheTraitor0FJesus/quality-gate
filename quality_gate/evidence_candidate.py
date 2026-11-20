"""Match an immutable candidate to current Git modes and working-tree bytes."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
from pathlib import Path

from .edit_inputs import regular_path
from .edit_protocol import EditError
from .operation import current_operation, run_probe
from .snapshot import SnapshotError, _index_fingerprint, _parse_staged_entry

_WINDOWS_FILE_MODE = 0o666
_SHA1_HEX_LENGTH = 40


def _git_output(
	repository: Path,
	arguments: tuple[str, ...],
	deadline: float,
	timeout: int,
	*,
	index: Path | None = None,
) -> bytes:
	"""Keep Git probes under the same operation supervisor and remaining work deadline."""
	operation = current_operation()
	if operation is None:
		raise SnapshotError("candidate evidence requires an owned Gate operation")
	remaining = min(operation.remaining(float(timeout)), deadline - time.monotonic())
	if remaining <= 0:
		operation.remaining()
		raise SnapshotError("candidate evidence exceeded its deadline")
	environment = os.environ.copy()
	if index is not None:
		environment["GIT_INDEX_FILE"] = str(index)
	try:
		result = run_probe(
			["git", *arguments], cwd=repository, environment=environment, timeout=remaining
		)
	except (OSError, subprocess.TimeoutExpired) as error:
		operation.remaining()
		raise SnapshotError("Git could not identify the candidate evidence") from error
	if result.returncode != 0:
		raise SnapshotError("Git could not identify the candidate evidence")
	return result.stdout


def _evidence_index_path(repository: Path, deadline: float, timeout: int) -> Path:
	configured = os.environ.get("GIT_INDEX_FILE")
	if configured is None:
		try:
			configured = (
				_git_output(repository, ("rev-parse", "--git-path", "index"), deadline, timeout)
				.decode("utf-8")
				.strip()
			)
		except UnicodeDecodeError as error:
			raise SnapshotError("Git returned a non-UTF-8 index path") from error
	path = Path(configured)
	return (path if path.is_absolute() else repository / path).resolve()


class CandidateEvidence:
	"""Capture index identity and safely identify files eligible for local reuse."""

	def __init__(self, repository: Path, deadline: float, timeout: int) -> None:
		self.repository = repository.resolve(strict=True)
		self.deadline = deadline
		self.index = _evidence_index_path(self.repository, deadline, timeout)
		self.fingerprint = _index_fingerprint(self.index, deadline)
		output = _git_output(
			self.repository, ("ls-files", "--stage", "-z"), deadline, timeout, index=self.index
		)
		entries = [_parse_staged_entry(item) for item in output.split(b"\0") if item]
		if self.fingerprint != _index_fingerprint(self.index, deadline):
			raise ValueError("Git index changed while identifying evidence")
		self.entries = {entry.relative_path: entry for entry in entries if entry is not None}

	def mode(self, relative: str, content: bytes) -> int | None:
		entry = self.entries.get(relative)
		if entry is None or entry.mode not in {b"100644", b"100755"}:
			return None
		try:
			path = regular_path(self.repository, relative)
			metadata = path.lstat()
			if (
				not stat.S_ISREG(metadata.st_mode)
				or metadata.st_size != len(content)
				or path.read_bytes() != content
			):
				return None
			mode = stat.S_IMODE(metadata.st_mode)
			if os.name == "nt":
				if entry.mode != b"100644" or mode != _WINDOWS_FILE_MODE:
					return None
			elif mode != (0o755 if entry.mode == b"100755" else 0o644):
				return None
			algorithm = hashlib.sha1 if len(entry.object_id) == _SHA1_HEX_LENGTH else hashlib.sha256
			blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
			if (
				algorithm(blob, usedforsecurity=False).hexdigest().encode("ascii")
				!= entry.object_id
			):
				return None
			return mode
		except (OSError, ValueError, EditError):
			return None

	def assert_current(self, files: dict[str, tuple[bytes, int]]) -> None:
		try:
			if self.fingerprint != _index_fingerprint(self.index, self.deadline):
				raise ValueError("Git index changed during evidence lookup")
			if any(self.mode(path, content) != mode for path, (content, mode) in files.items()):
				raise ValueError("working-tree evidence inputs changed")
		except SnapshotError as error:
			raise ValueError("Git index could not be revalidated") from error
