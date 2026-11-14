"""Read-only materialization of the exact Git index candidate."""

from __future__ import annotations

import hashlib as hashlib
import logging
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading as threading
import time
import tomllib as tomllib
from collections.abc import Callable, Iterator
from collections.abc import Mapping as Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO as BinaryIO
from typing import cast as cast

from .contracts import (
	DEFAULT_COMMAND_TIMEOUT_SECONDS,
)
from .contracts import (
	DEFAULT_MAX_BLOB_SIZE_MIB as DEFAULT_MAX_BLOB_SIZE_MIB,
)
from .snapshot_batch import (
	_candidate_limits as _candidate_limits,
)
from .snapshot_batch import (
	_close_pipe as _close_pipe,
)
from .snapshot_batch import (
	_GitBatchOutput as _GitBatchOutput,
)
from .snapshot_batch import (
	_object_header as _object_header,
)
from .snapshot_batch import (
	_prepare_index as _prepare_index,
)
from .snapshot_batch import (
	_read_batch_object as _read_batch_object,
)
from .snapshot_batch import (
	_read_cached_blob as _read_cached_blob,
)
from .snapshot_batch import (
	_read_git_blobs as _read_git_blobs,
)
from .snapshot_batch import (
	_reject_filtered_paths as _reject_filtered_paths,
)
from .snapshot_batch import (
	_run_git_batch as _run_git_batch,
)
from .snapshot_batch import (
	_stop_and_reap as _stop_and_reap,
)
from .snapshot_batch import (
	_wait_for_git_batch as _wait_for_git_batch,
)

_SUPPORTED_MODES = {b"100644", b"100755", b"120000"}
_IndexFingerprint = tuple[int, int, int, bytes] | None
_LOGGER = logging.getLogger(__name__)
_MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CANDIDATE_BLOB_SIZE_MIB = 64
_SHA1_OBJECT_ID_BYTES = 40
_SHA256_OBJECT_ID_BYTES = 64
_MISSING_OBJECT_HEADER_FIELDS = 2
_OBJECT_HEADER_FIELDS = 3
_MAX_OBJECT_SIZE_DIGITS = 20
_MAX_ATTRIBUTE_VALUE_BYTES = 1024
_MAX_OBJECT_HEADER_BYTES = 96
_BATCH_READ_CHUNK_BYTES = 64 * 1024


class SnapshotError(RuntimeError):
	"""A candidate could not be constructed or proved unchanged."""

	def __init__(self, message: str = "candidate snapshot is unavailable") -> None:
		self.message = message
		super().__init__(message)


def _positive_limit(value: int, name: str) -> int:
	if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
		raise SnapshotError(f"{name} must be a positive integer")
	return value


def _validate_deadline(deadline: float | None) -> float | None:
	if deadline is None:
		return None
	if (
		isinstance(deadline, bool)
		or not isinstance(deadline, (int, float))
		or not math.isfinite(deadline)
	):
		raise SnapshotError("snapshot deadline must be a finite monotonic timestamp")
	return float(deadline)


def _check_deadline(deadline: float | None) -> None:
	if deadline is not None and time.monotonic() >= deadline:
		raise SnapshotError("candidate snapshot exceeded its operation deadline")


def _remaining_timeout(timeout_seconds: int, deadline: float | None) -> float:
	_check_deadline(deadline)
	if deadline is None:
		return float(timeout_seconds)
	remaining = deadline - time.monotonic()
	if remaining <= 0:
		raise SnapshotError("candidate snapshot exceeded its operation deadline")
	return min(float(timeout_seconds), remaining)


def _git(
	repository: Path,
	arguments: tuple[str, ...],
	timeout_seconds: int,
	*,
	index_file: Path | None = None,
	deadline: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
	environment = os.environ.copy()
	if index_file is not None:
		environment["GIT_INDEX_FILE"] = str(index_file)
	_LOGGER.debug("running Git candidate operation")
	try:
		result = subprocess.run(
			["git", *arguments],
			cwd=repository,
			env=environment,
			capture_output=True,
			check=False,
			timeout=_remaining_timeout(timeout_seconds, deadline),
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		if deadline is not None and time.monotonic() >= deadline:
			raise SnapshotError("candidate snapshot exceeded its operation deadline") from exc
		raise SnapshotError("Git could not provide the candidate snapshot") from exc
	_check_deadline(deadline)
	if result.returncode != 0:
		raise SnapshotError(
			f"Git could not provide the candidate snapshot (exit code {result.returncode})"
		)
	return result


def _index_path(repository: Path, timeout_seconds: int, deadline: float | None = None) -> Path:
	configured_path = os.environ.get("GIT_INDEX_FILE")
	if configured_path:
		path = Path(configured_path)
		return (path if path.is_absolute() else repository / path).resolve()
	result = _git(
		repository,
		("rev-parse", "--git-path", "index"),
		timeout_seconds,
		deadline=deadline,
	)
	try:
		raw_path = result.stdout.decode("utf-8").strip()
	except UnicodeDecodeError as exc:
		raise SnapshotError("Git returned a non-UTF-8 index path") from exc
	path = Path(raw_path)
	return (path if path.is_absolute() else repository / path).resolve()


def _index_fingerprint(path: Path, deadline: float | None = None) -> _IndexFingerprint:
	_check_deadline(deadline)
	try:
		metadata = path.stat()
		content = path.read_bytes()
	except FileNotFoundError:
		_check_deadline(deadline)
		return None
	except OSError as exc:
		raise SnapshotError("the Git index could not be read") from exc
	_check_deadline(deadline)
	return (
		metadata.st_size,
		metadata.st_mtime_ns,
		metadata.st_ctime_ns,
		hashlib.sha256(content).digest(),
	)


def _validated_relative_path(raw_path: bytes) -> str:
	try:
		path = raw_path.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise SnapshotError("the Git index contains a non-UTF-8 path") from exc
	relative = Path(path)
	if (
		not path
		or "\\" in path
		or relative.is_absolute()
		or relative.drive
		or any(part in {".", ".."} for part in relative.parts)
	):
		raise SnapshotError("the Git index contains an unsafe relative path")
	return path


@dataclass(frozen=True, slots=True)
class _StagedEntry:
	mode: bytes
	object_id: bytes
	raw_path: bytes
	relative_path: str


def _valid_object_id(object_id: bytes) -> bool:
	return len(object_id) in {_SHA1_OBJECT_ID_BYTES, _SHA256_OBJECT_ID_BYTES} and all(
		byte in b"0123456789abcdef" for byte in object_id
	)


def _parse_staged_entry(raw_entry: bytes) -> _StagedEntry | None:
	if not raw_entry:
		return None
	try:
		metadata, raw_path = raw_entry.split(b"\t", 1)
		mode, object_id, stage = metadata.split()
	except ValueError as exc:
		raise SnapshotError("the Git index contains an unsupported entry") from exc
	if stage != b"0":
		raise SnapshotError("the Git index contains unresolved merge entries")
	if mode == b"160000":
		raise SnapshotError("Git submodules are not supported by this snapshot")
	if mode not in _SUPPORTED_MODES:
		raise SnapshotError("the Git index contains an unsupported entry mode")
	if object_id in {
		b"0" * _SHA1_OBJECT_ID_BYTES,
		b"0" * _SHA256_OBJECT_ID_BYTES,
	}:
		raise SnapshotError("Git intent-to-add entries are not supported by this snapshot")
	if not _valid_object_id(object_id):
		raise SnapshotError("the Git index contains an invalid object ID")
	return _StagedEntry(
		mode=mode,
		object_id=object_id,
		raw_path=raw_path,
		relative_path=_validated_relative_path(raw_path),
	)


def _candidate_path(root: Path, relative_path: str) -> Path:
	path = root / relative_path
	resolved_root = root.resolve()
	resolved_path = path.resolve()
	if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
		raise SnapshotError("the candidate path escapes its snapshot root")
	return path


def _index_state(
	repository: Path, timeout_seconds: int, index_file: Path, deadline: float | None = None
) -> tuple[bytes, tuple[_StagedEntry, ...]]:
	unmerged = _git(
		repository,
		("ls-files", "--unmerged", "-z"),
		timeout_seconds,
		index_file=index_file,
		deadline=deadline,
	).stdout
	staged = _git(
		repository,
		("ls-files", "--stage", "-z"),
		timeout_seconds,
		index_file=index_file,
		deadline=deadline,
	).stdout
	status = _git(
		repository,
		("status", "--porcelain=v2", "--untracked-files=no"),
		timeout_seconds,
		index_file=index_file,
		deadline=deadline,
	).stdout
	if unmerged:
		raise SnapshotError("the Git index contains unresolved merge entries")
	if any(line.startswith(b"1 .A ") and b" 000000 " in line for line in status.splitlines()):
		raise SnapshotError("Git intent-to-add entries are not supported by this snapshot")
	entries: list[_StagedEntry] = []
	for raw_entry in staged.split(b"\0"):
		_check_deadline(deadline)
		entry = _parse_staged_entry(raw_entry)
		if entry is not None:
			entries.append(entry)
	fingerprint = hashlib.sha256(unmerged + b"\0" + staged).digest()
	return fingerprint, tuple(entries)


def _make_read_only(path: Path, deadline: float | None = None) -> None:
	try:
		for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
			_check_deadline(deadline)
			if child.is_symlink():
				continue
			mode = child.stat().st_mode
			read_only_mode = stat.S_IREAD
			if child.is_dir() or mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
				read_only_mode |= stat.S_IEXEC
			os.chmod(child, read_only_mode)
		os.chmod(path, stat.S_IREAD | stat.S_IEXEC)
		_check_deadline(deadline)
	except SnapshotError:
		raise
	except OSError as exc:
		raise SnapshotError("candidate snapshot could not be made immutable") from exc


def _make_writable(path: Path) -> None:
	for child in sorted(path.rglob("*"), key=lambda item: len(item.parts)):
		if child.is_symlink():
			continue
		os.chmod(child, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
	os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)


def remove_snapshot(path: Path) -> None:
	"""Remove an owned snapshot root, retrying its read-only payload as writable."""
	if not path.exists():
		return
	try:
		shutil.rmtree(path)
	except OSError:
		_LOGGER.debug("candidate snapshot cleanup requires a writable retry", exc_info=True)
		try:
			_make_writable(path)
			shutil.rmtree(path)
		except OSError as exc:
			raise SnapshotError("candidate snapshot cleanup failed") from exc


def _register_snapshot_cleanup(path: Path, register_cleanup: Callable[[Path], None]) -> None:
	try:
		register_cleanup(path)
	except Exception as exc:
		raise SnapshotError("candidate snapshot cleanup registration failed") from exc


def _cleanup_after_failure(
	path: Path | None,
	original: BaseException,
	register_cleanup: Callable[[Path], None] | None = None,
) -> None:
	if path is None:
		return
	if register_cleanup is not None:
		try:
			_register_snapshot_cleanup(path, register_cleanup)
		except SnapshotError as registration_error:
			registration_error.__context__ = original
			raise
		return
	try:
		remove_snapshot(path)
	except SnapshotError as cleanup_error:
		raise cleanup_error from original


@dataclass(frozen=True, slots=True)
class _MaterializationContext:
	repository: Path
	candidate_root: Path
	index_path: Path
	source_fingerprint: _IndexFingerprint
	index_copy: Path
	timeout_seconds: int
	max_blob_bytes: int
	deadline: float | None = None


def _materialize_entry(context: _MaterializationContext, entry: _StagedEntry, blob: bytes) -> None:
	_check_deadline(context.deadline)
	path = _candidate_path(context.candidate_root, entry.relative_path)
	path.parent.mkdir(parents=True, exist_ok=True)
	_check_deadline(context.deadline)
	if entry.mode == b"120000":
		try:
			target = blob.decode("utf-8")
		except UnicodeDecodeError as exc:
			raise SnapshotError("a staged symbolic link has an invalid target") from exc
		target_path = Path(target)
		resolved_root = context.candidate_root.resolve()
		resolved_target = (path.parent / target_path).resolve()
		if (
			target_path.is_absolute()
			or target_path.drive
			or "\\" in target
			or (resolved_target != resolved_root and resolved_root not in resolved_target.parents)
		):
			raise SnapshotError("a staged symbolic link escapes its snapshot root")
		try:
			os.symlink(target, path)
		except OSError as exc:
			raise SnapshotError("a staged symbolic link could not be materialized") from exc
		_check_deadline(context.deadline)
		return
	path.write_bytes(blob)
	_check_deadline(context.deadline)
	if entry.mode == b"100755":
		os.chmod(
			path,
			stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
		)
	_check_deadline(context.deadline)


def _materialize_candidate(context: _MaterializationContext) -> None:
	_check_deadline(context.deadline)
	_, entries = _index_state(
		context.repository, context.timeout_seconds, context.index_copy, context.deadline
	)
	_reject_filtered_paths(
		context.repository,
		entries,
		context.index_copy,
		context.timeout_seconds,
		context.deadline,
	)
	if _index_fingerprint(context.index_path, context.deadline) != context.source_fingerprint:
		raise SnapshotError("the Git index changed during candidate construction")
	_check_deadline(context.deadline)
	object_directory = context.candidate_root.parent / "objects"
	object_sizes = _read_git_blobs(context, entries, object_directory)
	for entry in entries:
		_check_deadline(context.deadline)
		blob = _read_cached_blob(
			object_directory / entry.object_id.decode("ascii"),
			object_sizes[entry.object_id],
			context.max_blob_bytes,
			context.deadline,
		)
		_materialize_entry(context, entry, blob)
	if object_directory.exists():
		shutil.rmtree(object_directory)
	_make_read_only(context.candidate_root, context.deadline)
	_check_deadline(context.deadline)


def _create_snapshot(
	repository: Path,
	timeout_seconds: int | None,
	max_blob_size_mib: int | None,
	deadline: float | None = None,
	register_cleanup: Callable[[Path], None] | None = None,
) -> CandidateSnapshot:
	bootstrap_timeout = (
		DEFAULT_COMMAND_TIMEOUT_SECONDS
		if timeout_seconds is None
		else _positive_limit(timeout_seconds, "snapshot timeout")
	)
	deadline = _validate_deadline(deadline)
	_check_deadline(deadline)
	staging_root: Path | None = None
	construction_succeeded = False
	construction_error: BaseException | None = None
	try:
		staging_root = Path(tempfile.mkdtemp(prefix="quality-gate-candidate-"))
		_check_deadline(deadline)
		candidate_root = staging_root / "candidate"
		candidate_root.mkdir()
		_check_deadline(deadline)
		index_path, source_fingerprint, index_copy = _prepare_index(
			repository, staging_root, bootstrap_timeout, deadline
		)
		_check_deadline(deadline)
		manifest_timeout, manifest_blob_limit = _candidate_limits(
			repository, index_copy, bootstrap_timeout, deadline
		)
		effective_timeout = bootstrap_timeout if timeout_seconds is not None else manifest_timeout
		effective_blob_limit = (
			_positive_limit(max_blob_size_mib, "snapshot blob limit")
			if max_blob_size_mib is not None
			else max(manifest_blob_limit, MAX_CANDIDATE_BLOB_SIZE_MIB)
		)
		_materialize_candidate(
			_MaterializationContext(
				repository=repository,
				candidate_root=candidate_root,
				index_path=index_path,
				source_fingerprint=source_fingerprint,
				index_copy=index_copy,
				timeout_seconds=effective_timeout,
				max_blob_bytes=effective_blob_limit * 1024 * 1024,
				deadline=deadline,
			)
		)
		_check_deadline(deadline)
		snapshot = CandidateSnapshot(
			root=candidate_root,
			repository_index=index_path,
			_source_fingerprint=source_fingerprint,
			_cleanup_root=staging_root,
		)
		construction_succeeded = True
		return snapshot
	except KeyboardInterrupt as exc:
		construction_error = SnapshotError("candidate snapshot construction was interrupted")
		raise construction_error from exc
	except SnapshotError as exc:
		construction_error = exc
		raise
	except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
		construction_error = SnapshotError("candidate snapshot construction failed")
		raise construction_error from exc
	finally:
		if staging_root is not None and not construction_succeeded:
			original_error = construction_error or sys.exception()
			if original_error is not None:
				_cleanup_after_failure(staging_root, original_error, register_cleanup)


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
	"""One read-only candidate root and the source index state that created it."""

	root: Path
	repository_index: Path
	_source_fingerprint: _IndexFingerprint = field(repr=False)
	_cleanup_root: Path = field(repr=False)

	@property
	def index_identity(self) -> str:
		"""Identify the captured source index without exposing its private fingerprint."""
		if self._source_fingerprint is None:
			return "staged Git index identity unavailable"
		return f"git-index-sha256:{self._source_fingerprint[3].hex()}"

	def assert_unchanged(self) -> None:
		"""Reject verification when the source Git index changed after snapshot creation."""

		if _index_fingerprint(self.repository_index) != self._source_fingerprint:
			raise SnapshotError("the Git index changed during candidate verification")


@contextmanager
def candidate_snapshot(
	repository: Path | str = ".",
	*,
	timeout_seconds: int | None = None,
	max_blob_size_mib: int | None = None,
	deadline: float | None = None,
	register_cleanup: Callable[[Path], None] | None = None,
) -> Iterator[CandidateSnapshot]:
	"""Yield an immutable index candidate with an absolute work deadline and cleanup handoff.

	The deadline is one absolute ``time.monotonic()`` timestamp for preparation and
	materialization; it does not bound teardown. When provided, ``register_cleanup`` receives the
	complete owned staging root once and takes responsibility for cleanup. This handoff still
	runs after the deadline.
	"""

	snapshot = _create_snapshot(
		Path(repository).resolve(),
		timeout_seconds,
		max_blob_size_mib,
		deadline,
		register_cleanup,
	)
	primary_error: BaseException | None = None
	try:
		yield snapshot
		snapshot.assert_unchanged()
	except KeyboardInterrupt as exc:
		primary_error = SnapshotError("candidate verification was interrupted")
		raise primary_error from exc
	finally:
		if register_cleanup is None:
			remove_snapshot(snapshot._cleanup_root)
		else:
			original_error = primary_error or sys.exception()
			try:
				_register_snapshot_cleanup(snapshot._cleanup_root, register_cleanup)
			except SnapshotError as registration_error:
				if original_error is not None:
					registration_error.__context__ = original_error
				raise
