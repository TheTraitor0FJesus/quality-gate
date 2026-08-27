"""Read-only materialization of the exact Git index candidate."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from quality_gate.contracts import (
	DEFAULT_COMMAND_TIMEOUT_SECONDS,
	DEFAULT_MAX_BLOB_SIZE_MIB,
)

_SUPPORTED_MODES = {b"100644", b"100755", b"120000"}
_IndexFingerprint = tuple[int, int, int, bytes] | None
_LOGGER = logging.getLogger(__name__)
_MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CANDIDATE_BLOB_SIZE_MIB = 64


class SnapshotError(RuntimeError):
	"""A candidate could not be constructed or proved unchanged."""

	def __init__(self, message: str = "candidate snapshot is unavailable") -> None:
		self.message = message
		super().__init__(message)


def _positive_limit(value: int, name: str) -> int:
	if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
		raise SnapshotError(f"{name} must be a positive integer")
	return value


def _git(
	repository: Path,
	arguments: tuple[str, ...],
	timeout_seconds: int,
	*,
	index_file: Path | None = None,
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
			timeout=timeout_seconds,
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise SnapshotError("Git could not provide the candidate snapshot") from exc
	if result.returncode != 0:
		raise SnapshotError(
			f"Git could not provide the candidate snapshot (exit code {result.returncode})"
		)
	return result


def _index_path(repository: Path, timeout_seconds: int) -> Path:
	configured_path = os.environ.get("GIT_INDEX_FILE")
	if configured_path:
		path = Path(configured_path)
		return (path if path.is_absolute() else repository / path).resolve()
	result = _git(repository, ("rev-parse", "--git-path", "index"), timeout_seconds)
	try:
		raw_path = result.stdout.decode("utf-8").strip()
	except UnicodeDecodeError as exc:
		raise SnapshotError("Git returned a non-UTF-8 index path") from exc
	path = Path(raw_path)
	return (path if path.is_absolute() else repository / path).resolve()


def _index_fingerprint(path: Path) -> _IndexFingerprint:
	try:
		metadata = path.stat()
		content = path.read_bytes()
	except FileNotFoundError:
		return None
	except OSError as exc:
		raise SnapshotError("the Git index could not be read") from exc
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


def _candidate_path(root: Path, relative_path: str) -> Path:
	path = root / relative_path
	resolved_root = root.resolve()
	resolved_path = path.resolve()
	if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
		raise SnapshotError("the candidate path escapes its snapshot root")
	return path


def _index_state(repository: Path, timeout_seconds: int, index_file: Path) -> tuple[bytes, bytes]:
	unmerged = _git(
		repository, ("ls-files", "--unmerged", "-z"), timeout_seconds, index_file=index_file
	).stdout
	staged = _git(
		repository, ("ls-files", "--stage", "-z"), timeout_seconds, index_file=index_file
	).stdout
	status = _git(
		repository,
		("status", "--porcelain=v2", "--untracked-files=no"),
		timeout_seconds,
		index_file=index_file,
	).stdout
	if unmerged:
		raise SnapshotError("the Git index contains unresolved merge entries")
	if any(line.startswith(b"1 .A ") and b" 000000 " in line for line in status.splitlines()):
		raise SnapshotError("Git intent-to-add entries are not supported by this snapshot")
	for entry in staged.split(b"\0"):
		if not entry:
			continue
		try:
			metadata, raw_path = entry.split(b"\t", 1)
			mode, object_id, stage = metadata.split()
		except ValueError as exc:
			raise SnapshotError("the Git index contains an unsupported entry") from exc
		if stage != b"0":
			raise SnapshotError("the Git index contains unresolved merge entries")
		if mode == b"160000":
			raise SnapshotError("Git submodules are not supported by this snapshot")
		if mode not in _SUPPORTED_MODES:
			raise SnapshotError("the Git index contains an unsupported entry mode")
		if object_id == b"0" * 40:
			raise SnapshotError("Git intent-to-add entries are not supported by this snapshot")
		_validated_relative_path(raw_path)
	fingerprint = hashlib.sha256(unmerged + b"\0" + staged).digest()
	return fingerprint, staged


def _make_read_only(path: Path) -> None:
	try:
		for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
			if child.is_symlink():
				continue
			mode = child.stat().st_mode
			read_only_mode = stat.S_IREAD
			if child.is_dir() or mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
				read_only_mode |= stat.S_IEXEC
			os.chmod(child, read_only_mode)
		os.chmod(path, stat.S_IREAD | stat.S_IEXEC)
	except OSError as exc:
		raise SnapshotError("candidate snapshot could not be made immutable") from exc


def _make_writable(path: Path) -> None:
	for child in sorted(path.rglob("*"), key=lambda item: len(item.parts)):
		if child.is_symlink():
			continue
		os.chmod(child, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
	os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)


def _remove_snapshot(path: Path) -> None:
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


def _cleanup_after_failure(path: Path | None, original: BaseException) -> None:
	if path is None:
		return
	try:
		_remove_snapshot(path)
	except SnapshotError as cleanup_error:
		raise cleanup_error from original


def _prepare_index(
	repository: Path, staging_root: Path, timeout_seconds: int
) -> tuple[Path, _IndexFingerprint, Path]:
	index_path = _index_path(repository, timeout_seconds)
	source_fingerprint = _index_fingerprint(index_path)
	index_copy = staging_root / "index"
	if source_fingerprint is not None:
		shutil.copyfile(index_path, index_copy)
	if _index_fingerprint(index_path) != source_fingerprint:
		raise SnapshotError("the Git index changed during candidate construction")
	return index_path, source_fingerprint, index_copy


def _candidate_limits(repository: Path, index_file: Path, timeout_seconds: int) -> tuple[int, int]:
	try:
		manifest = _git(
			repository, ("show", ":quality-gate.toml"), timeout_seconds, index_file=index_file
		).stdout
	except SnapshotError:
		return DEFAULT_COMMAND_TIMEOUT_SECONDS, DEFAULT_MAX_BLOB_SIZE_MIB
	if len(manifest) > _MAX_MANIFEST_BYTES:
		raise SnapshotError("the staged quality manifest exceeds the snapshot limit")
	try:
		table = tomllib.loads(manifest.decode("utf-8"))
	except (tomllib.TOMLDecodeError, UnicodeDecodeError):
		return DEFAULT_COMMAND_TIMEOUT_SECONDS, DEFAULT_MAX_BLOB_SIZE_MIB
	repository_table = table.get("repository")
	if not isinstance(repository_table, Mapping):
		return DEFAULT_COMMAND_TIMEOUT_SECONDS, DEFAULT_MAX_BLOB_SIZE_MIB
	defaults = repository_table.get("defaults")
	limits = repository_table.get("limits")
	command_timeout = (
		defaults.get("command_timeout_seconds")
		if isinstance(defaults, Mapping)
		else DEFAULT_COMMAND_TIMEOUT_SECONDS
	)
	max_blob_size = (
		limits.get("max_blob_size_mib")
		if isinstance(limits, Mapping)
		else DEFAULT_MAX_BLOB_SIZE_MIB
	)
	if (
		isinstance(command_timeout, bool)
		or not isinstance(command_timeout, int)
		or command_timeout <= 0
	):
		command_timeout = DEFAULT_COMMAND_TIMEOUT_SECONDS
	if isinstance(max_blob_size, bool) or not isinstance(max_blob_size, int) or max_blob_size <= 0:
		max_blob_size = DEFAULT_MAX_BLOB_SIZE_MIB
	return command_timeout, max_blob_size


def _reject_filtered_paths(
	repository: Path, staged: bytes, index_file: Path, timeout_seconds: int
) -> None:
	for entry in staged.split(b"\0"):
		if not entry:
			continue
		try:
			_path_metadata, raw_path = entry.split(b"\t", 1)
			path = _validated_relative_path(raw_path)
		except ValueError as exc:
			raise SnapshotError("the Git index contains an invalid path") from exc
		attribute = _git(
			repository,
			("check-attr", "--cached", "filter", "--", path),
			timeout_seconds,
			index_file=index_file,
		).stdout
		if attribute.rsplit(b": ", 1)[-1].strip().lower() != b"unspecified":
			raise SnapshotError("Git clean/smudge filters are not supported by this snapshot")


@dataclass(frozen=True, slots=True)
class _MaterializationContext:
	repository: Path
	candidate_root: Path
	index_path: Path
	source_fingerprint: _IndexFingerprint
	index_copy: Path
	timeout_seconds: int
	max_blob_bytes: int


def _git_blob(context: _MaterializationContext, object_id: bytes) -> bytes:
	try:
		object_name = object_id.decode("ascii")
	except UnicodeDecodeError as exc:
		raise SnapshotError("the Git index contains an invalid object ID") from exc
	try:
		process = subprocess.Popen(
			["git", "cat-file", "blob", object_name],
			cwd=context.repository,
			env={**os.environ, "GIT_INDEX_FILE": str(context.index_copy)},
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
		)
	except OSError as exc:
		raise SnapshotError("Git could not provide the candidate snapshot") from exc
	assert process.stdout is not None
	stdout = process.stdout
	_LOGGER.debug("running Git candidate blob operation")
	output: list[bytes] = []
	read_error: list[OSError] = []

	def read_blob() -> None:
		try:
			output.append(stdout.read(context.max_blob_bytes + 1))
		except OSError as exc:
			read_error.append(exc)

	reader = threading.Thread(target=read_blob, daemon=True)
	reader.start()
	deadline = time.monotonic() + context.timeout_seconds
	reader.join(context.timeout_seconds)
	if reader.is_alive():
		process.kill()
		reader.join(max(0.0, deadline - time.monotonic()))
		process.wait()
		raise SnapshotError("Git could not provide the candidate snapshot")
	try:
		process.wait(timeout=max(0.0, deadline - time.monotonic()))
	except subprocess.TimeoutExpired as exc:
		process.kill()
		process.wait()
		raise SnapshotError("Git could not provide the candidate snapshot") from exc
	if read_error:
		raise SnapshotError("Git could not provide the candidate snapshot") from read_error[0]
	blob = output[0]
	if len(blob) > context.max_blob_bytes:
		raise SnapshotError("a staged blob exceeds the configured snapshot limit")
	if process.returncode != 0:
		raise SnapshotError(
			f"Git could not provide the candidate snapshot (exit code {process.returncode})"
		)
	return blob


def _materialize_candidate(context: _MaterializationContext) -> None:
	_, staged = _index_state(context.repository, context.timeout_seconds, context.index_copy)
	_reject_filtered_paths(context.repository, staged, context.index_copy, context.timeout_seconds)
	if _index_fingerprint(context.index_path) != context.source_fingerprint:
		raise SnapshotError("the Git index changed during candidate construction")
	for entry in staged.split(b"\0"):
		if not entry:
			continue
		metadata, raw_path = entry.split(b"\t", 1)
		mode, object_id, _stage = metadata.split()
		relative_path = _validated_relative_path(raw_path)
		path = _candidate_path(context.candidate_root, relative_path)
		path.parent.mkdir(parents=True, exist_ok=True)
		blob = _git_blob(context, object_id)
		if mode == b"120000":
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
				or (
					resolved_target != resolved_root
					and resolved_root not in resolved_target.parents
				)
			):
				raise SnapshotError("a staged symbolic link escapes its snapshot root")
			try:
				os.symlink(target, path)
			except OSError as exc:
				raise SnapshotError("a staged symbolic link could not be materialized") from exc
		else:
			path.write_bytes(blob)
			if mode == b"100755":
				os.chmod(
					path,
					stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
				)
	_make_read_only(context.candidate_root)


def _create_snapshot(
	repository: Path, timeout_seconds: int | None, max_blob_size_mib: int | None
) -> CandidateSnapshot:
	bootstrap_timeout = (
		DEFAULT_COMMAND_TIMEOUT_SECONDS
		if timeout_seconds is None
		else _positive_limit(timeout_seconds, "snapshot timeout")
	)
	staging_root: Path | None = None
	try:
		staging_root = Path(tempfile.mkdtemp(prefix="quality-gate-candidate-"))
		candidate_root = staging_root / "candidate"
		candidate_root.mkdir()
		index_path, source_fingerprint, index_copy = _prepare_index(
			repository, staging_root, bootstrap_timeout
		)
		manifest_timeout, manifest_blob_limit = _candidate_limits(
			repository, index_copy, bootstrap_timeout
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
			)
		)
		return CandidateSnapshot(
			root=candidate_root,
			repository_index=index_path,
			_source_fingerprint=source_fingerprint,
			_cleanup_root=staging_root,
		)
	except KeyboardInterrupt as exc:
		_cleanup_after_failure(staging_root, exc)
		raise SnapshotError("candidate snapshot construction was interrupted") from exc
	except SnapshotError as exc:
		_cleanup_after_failure(staging_root, exc)
		raise
	except (OSError, UnicodeError, ValueError) as exc:
		_cleanup_after_failure(staging_root, exc)
		raise SnapshotError("candidate snapshot construction failed") from exc


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
	"""One read-only candidate root and the source index state that created it."""

	root: Path
	repository_index: Path
	_source_fingerprint: _IndexFingerprint = field(repr=False)
	_cleanup_root: Path = field(repr=False)

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
) -> Iterator[CandidateSnapshot]:
	"""Yield one immutable index candidate and remove it before returning."""

	snapshot = _create_snapshot(Path(repository).resolve(), timeout_seconds, max_blob_size_mib)
	try:
		yield snapshot
		snapshot.assert_unchanged()
	except KeyboardInterrupt as exc:
		raise SnapshotError("candidate verification was interrupted") from exc
	finally:
		_remove_snapshot(snapshot._cleanup_root)
