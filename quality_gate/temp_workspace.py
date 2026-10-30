"""Repository-local scratch space for Quality Gate executions."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from .path_safety import is_link_or_reparse, is_reparse_point

TEMP_DIRECTORY_NAME = "temp"
_SYSTEM_TEMP_ENVIRONMENT = "QUALITY_GATE_SYSTEM_TEMP"
SYSTEM_TEMP_DIRECTORY = Path(os.environ.get(_SYSTEM_TEMP_ENVIRONMENT) or tempfile.gettempdir())
_ROOT_LOCK_NAME = ".quality-gate.lock"
_ACTIVE_LOCK_NAME = ".active.lock"
_OWNERSHIP_MARKER_NAME = ".quality-gate-workspace"
_OWNERSHIP_MARKER_MAX_BYTES = 4096
_OWNERSHIP_RECORD_PREFIX = ".quality-gate-owner-"
_OWNERSHIP_RECORD_MAX_BYTES = 4096
_TEMP_ENVIRONMENT = ("TMP", "TEMP", "TMPDIR")
_active_workspace: ContextVar[tuple[Path, Path] | None] = ContextVar(
	"quality_gate_active_workspace", default=None
)
# ponytail: process-global tempfile and environment overrides serialize workspace scopes; use
# per-call temp paths if all scratch-producing APIs later accept explicit directory injection.
_process_lock = threading.RLock()


class TemporaryWorkspaceError(OSError):
	"""Repository-local scratch space could not be prepared or cleaned."""


class _PosixLockModule(Protocol):
	LOCK_EX: int
	LOCK_NB: int
	LOCK_UN: int

	def flock(self, descriptor: int, operation: int) -> None: ...


class _WindowsLockModule(Protocol):
	LK_LOCK: int
	LK_NBLCK: int
	LK_UNLCK: int

	def locking(self, descriptor: int, mode: int, nbytes: int) -> None: ...


def _lock(handle: BinaryIO, *, blocking: bool) -> None:
	handle.seek(0)
	if os.name == "nt":
		msvcrt = cast(_WindowsLockModule, import_module("msvcrt"))
		mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
		msvcrt.locking(handle.fileno(), mode, 1)
		return

	fcntl = cast(_PosixLockModule, import_module("fcntl"))
	mode = fcntl.LOCK_EX
	if not blocking:
		mode |= fcntl.LOCK_NB
	fcntl.flock(handle.fileno(), mode)


def _unlock(handle: BinaryIO) -> None:
	try:
		handle.seek(0)
		if os.name == "nt":
			msvcrt = cast(_WindowsLockModule, import_module("msvcrt"))
			msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
		else:
			fcntl = cast(_PosixLockModule, import_module("fcntl"))
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
	finally:
		handle.close()


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
	return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _plain_lock_file(path: Path, metadata: os.stat_result) -> bool:
	return (
		stat.S_ISREG(metadata.st_mode)
		and not path.is_symlink()
		and not is_reparse_point(metadata)
		and metadata.st_nlink == 1
	)


def _open_locked_file(path: Path, *, blocking: bool, create: bool = True) -> BinaryIO | None:
	try:
		before = path.lstat()
	except FileNotFoundError:
		before = None
	if before is not None and not _plain_lock_file(path, before):
		raise OSError(f"refusing an unsafe temporary lock file: {path}")
	if before is None and not create:
		return None
	handle = path.open("a+b" if create else "r+b")
	try:
		opened = os.fstat(handle.fileno())
		try:
			current = path.lstat()
		except OSError as error:
			raise OSError(f"temporary lock path changed while opening: {path}") from error
		if (
			not _plain_lock_file(path, opened)
			or not _plain_lock_file(path, current)
			or not _same_file_identity(opened, current)
			or (before is not None and not _same_file_identity(before, opened))
		):
			raise OSError(f"temporary lock path changed while opening: {path}")
		handle.seek(0, os.SEEK_END)
		if handle.tell() == 0:
			handle.write(b"\0")
			handle.flush()
		try:
			_lock(handle, blocking=blocking)
		except OSError:
			if blocking:
				raise
			handle.close()
			return None
		return handle
	except OSError:
		handle.close()
		raise


def _remove_entry(path: Path, *, expected_identity: os.stat_result | None = None) -> None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return
	if expected_identity is not None and not _same_file_identity(expected_identity, metadata):
		raise OSError(f"temporary workspace identity changed before removal: {path}")
	if is_link_or_reparse(metadata):
		_remove_link(path, metadata)
	elif stat.S_ISDIR(metadata.st_mode):
		_remove_tree(path, metadata)
	else:
		_remove_file(path, metadata)


def _remove_tree(path: Path, identity: os.stat_result) -> None:
	directories: list[tuple[Path, os.stat_result]] = []
	pending = [(path, identity)]
	while pending:
		directory, expected = pending.pop()
		entries = _directory_entries_for_removal(directory, expected)
		if entries is None:
			continue
		directories.append((directory, expected))
		for child_path, scanned_identity in entries:
			_remove_scanned_child(directory, expected, child_path, scanned_identity, pending)
	for directory, expected in reversed(directories):
		_remove_collected_directory(directory, expected)


def _directory_entries_for_removal(
	directory: Path, expected: os.stat_result
) -> list[tuple[Path, os.stat_result]] | None:
	try:
		before = directory.lstat()
	except FileNotFoundError:
		return None
	if not _same_file_identity(expected, before):
		raise OSError(f"temporary workspace identity changed while removing it: {directory}")
	if is_link_or_reparse(before):
		_remove_link(directory, before)
		return None
	if not stat.S_ISDIR(before.st_mode):
		_remove_file(directory, before)
		return None
	try:
		with os.scandir(directory) as children:
			entries = [(Path(child.path), Path(child.path).lstat()) for child in children]
	except FileNotFoundError:
		return None
	after = directory.lstat()
	if is_link_or_reparse(after) or not _same_file_identity(before, after):
		raise OSError(f"temporary workspace directory changed while scanning it: {directory}")
	return entries


def _remove_scanned_child(
	parent: Path,
	parent_identity: os.stat_result,
	path: Path,
	scanned_identity: os.stat_result,
	pending: list[tuple[Path, os.stat_result]],
) -> None:
	parent_now = parent.lstat()
	if is_link_or_reparse(parent_now) or not _same_file_identity(parent_identity, parent_now):
		raise OSError(f"temporary workspace directory changed during removal: {parent}")
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return
	if not _same_file_identity(scanned_identity, metadata):
		raise OSError(f"temporary workspace entry changed during removal: {path}")
	if is_link_or_reparse(metadata):
		_remove_link(path, metadata)
	elif stat.S_ISDIR(metadata.st_mode):
		pending.append((path, metadata))
	else:
		_remove_file(path, metadata)


def _remove_collected_directory(path: Path, expected: os.stat_result) -> None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return
	if not _same_file_identity(expected, metadata):
		raise OSError(f"temporary workspace identity changed before removal: {path}")
	if is_link_or_reparse(metadata):
		_remove_link(path, metadata)
	else:
		_remove_directory(path, expected_identity=expected)


def _check_ordinary_directory(path: Path, *, allow_missing: bool = False) -> os.stat_result | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		if allow_missing:
			return None
		raise
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		raise OSError(errno.ELOOP, "temporary workspace path is not an ordinary directory", path)
	return metadata


def _remove_link(path: Path, metadata: os.stat_result) -> None:
	current = path.lstat()
	if not _same_file_identity(metadata, current):
		raise OSError(f"temporary workspace link changed before removal: {path}")
	if stat.S_ISDIR(metadata.st_mode):
		path.rmdir()
	else:
		path.unlink()


def _remove_file(path: Path, expected_identity: os.stat_result) -> None:
	metadata = path.lstat()
	if not _same_file_identity(expected_identity, metadata):
		raise OSError(f"temporary workspace file changed before removal: {path}")
	try:
		path.unlink()
	except PermissionError:
		metadata = path.lstat()
		if (
			not _same_file_identity(expected_identity, metadata)
			or is_link_or_reparse(metadata)
			or not stat.S_ISREG(metadata.st_mode)
		):
			raise
		os.chmod(path, stat.S_IWRITE)
		metadata = path.lstat()
		if (
			not _same_file_identity(expected_identity, metadata)
			or is_link_or_reparse(metadata)
			or not stat.S_ISREG(metadata.st_mode)
		):
			raise OSError(f"temporary workspace file changed before removal: {path}") from None
		path.unlink()


def _remove_directory(path: Path, *, expected_identity: os.stat_result) -> None:
	metadata = path.lstat()
	if not _same_file_identity(expected_identity, metadata):
		raise OSError(f"temporary workspace directory changed before removal: {path}")
	try:
		path.rmdir()
	except PermissionError:
		metadata = path.lstat()
		if (
			not _same_file_identity(expected_identity, metadata)
			or is_link_or_reparse(metadata)
			or not stat.S_ISDIR(metadata.st_mode)
		):
			raise
		os.chmod(path, stat.S_IWRITE)
		metadata = path.lstat()
		if (
			not _same_file_identity(expected_identity, metadata)
			or is_link_or_reparse(metadata)
			or not stat.S_ISDIR(metadata.st_mode)
		):
			raise OSError(f"temporary workspace directory changed before removal: {path}") from None
		path.rmdir()


def _clean_stale_entries(temp_root: Path) -> None:
	for owner_record in tuple(temp_root.iterdir()):
		entry = _run_directory_from_owner_record(temp_root, owner_record)
		if entry is not None:
			_reclaim_stale_workspace(temp_root, entry)


def _run_directory_from_owner_record(temp_root: Path, owner_record: Path) -> Path | None:
	if not owner_record.name.startswith(_OWNERSHIP_RECORD_PREFIX):
		return None
	run_name = owner_record.name.removeprefix(_OWNERSHIP_RECORD_PREFIX)
	if not run_name.startswith("qg-") or Path(run_name).name != run_name:
		return None
	return temp_root / run_name


def _reclaim_stale_workspace(temp_root: Path, entry: Path) -> None:
	try:
		metadata = entry.lstat()
	except FileNotFoundError:
		return
	if not _is_owned_workspace(temp_root, entry, metadata):
		return
	owner_valid, owner_identity = _has_valid_ownership_record(temp_root, entry, metadata)
	if not owner_valid or owner_identity is None:
		return
	if not _can_reclaim_workspace(temp_root, entry, metadata, owner_identity):
		return
	try:
		_remove_entry(entry, expected_identity=metadata)
	except OSError:
		return
	_remove_ownership_record(temp_root, entry, metadata, expected_record=owner_identity)


def _is_owned_workspace(temp_root: Path, entry: Path, metadata: os.stat_result) -> bool:
	return (
		stat.S_ISDIR(metadata.st_mode)
		and not is_link_or_reparse(metadata)
		and _has_valid_ownership_marker(temp_root, entry, expected_directory=metadata)
	)


def _can_reclaim_workspace(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
) -> bool:
	try:
		active_lock = _open_locked_file(entry / _ACTIVE_LOCK_NAME, blocking=False, create=False)
	except OSError:
		return False
	if active_lock is None:
		return False
	try:
		return _workspace_ownership_is_current(temp_root, entry, metadata, owner_identity)
	finally:
		_unlock(active_lock)


def _workspace_ownership_is_current(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
) -> bool:
	try:
		current = entry.lstat()
	except OSError:
		return False
	return (
		not is_link_or_reparse(current)
		and stat.S_ISDIR(current.st_mode)
		and _same_file_identity(metadata, current)
		and _has_valid_ownership_record(temp_root, entry, metadata, expected_record=owner_identity)[
			0
		]
		and _has_valid_ownership_marker(temp_root, entry, expected_directory=metadata)
	)


def _ownership_marker(temp_root: Path, run_directory: Path) -> bytes:
	return (
		json.dumps(
			{
				"format": "quality-gate-temp-workspace",
				"root": os.path.normcase(os.path.abspath(temp_root)),
				"run": run_directory.name,
				"schema": 1,
			},
			sort_keys=True,
			separators=(",", ":"),
		)
		+ "\n"
	).encode("utf-8")


def _ownership_record(
	temp_root: Path, run_directory: Path, directory_identity: os.stat_result
) -> bytes:
	return (
		json.dumps(
			{
				"device": directory_identity.st_dev,
				"format": "quality-gate-temp-workspace-owner",
				"inode": directory_identity.st_ino,
				"root": os.path.normcase(os.path.abspath(temp_root)),
				"run": run_directory.name,
				"schema": 1,
			},
			sort_keys=True,
			separators=(",", ":"),
		)
		+ "\n"
	).encode("utf-8")


def _ownership_record_path(temp_root: Path, run_directory: Path) -> Path:
	return temp_root / f"{_OWNERSHIP_RECORD_PREFIX}{run_directory.name}"


def _has_valid_ownership_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
	verify_directory: bool = True,
) -> tuple[bool, os.stat_result | None]:
	path = _ownership_record_path(temp_root, run_directory)
	try:
		metadata = path.lstat()
		if (
			not _plain_lock_file(path, metadata)
			or metadata.st_size > _OWNERSHIP_RECORD_MAX_BYTES
			or (expected_record is not None and not _same_file_identity(expected_record, metadata))
		):
			return False, metadata
		with path.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
				return False, metadata
			content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
		current = path.lstat()
	except OSError:
		return False, None
	if not _same_file_identity(metadata, current):
		return False, metadata
	if verify_directory:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False, metadata
		if not _same_file_identity(directory_identity, current_directory):
			return False, metadata
	return content == _ownership_record(temp_root, run_directory, directory_identity), metadata


def _write_ownership_record(
	temp_root: Path, run_directory: Path, directory_identity: os.stat_result
) -> os.stat_result:
	path = _ownership_record_path(temp_root, run_directory)
	created_identity: os.stat_result | None = None
	try:
		with path.open("xb") as stream:
			created_identity = os.fstat(stream.fileno())
			stream.write(_ownership_record(temp_root, run_directory, directory_identity))
			stream.flush()
			os.fsync(stream.fileno())
		valid, identity = _has_valid_ownership_record(temp_root, run_directory, directory_identity)
		if (
			not valid
			or identity is None
			or created_identity is None
			or not _same_file_identity(created_identity, identity)
		):
			raise OSError(f"temporary workspace ownership could not be recorded: {run_directory}")
		return identity
	except OSError:
		if created_identity is not None:
			try:
				_remove_file(path, created_identity)
			except OSError:
				pass
		raise


def _remove_ownership_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
) -> None:
	valid, identity = _has_valid_ownership_record(
		temp_root,
		run_directory,
		directory_identity,
		expected_record=expected_record,
		verify_directory=False,
	)
	if valid and identity is not None:
		_remove_file(_ownership_record_path(temp_root, run_directory), identity)


def _has_valid_ownership_marker(
	temp_root: Path,
	run_directory: Path,
	*,
	expected_directory: os.stat_result | None = None,
) -> bool:
	if expected_directory is not None:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False
		if (
			is_link_or_reparse(current_directory)
			or not stat.S_ISDIR(current_directory.st_mode)
			or not _same_file_identity(expected_directory, current_directory)
		):
			return False
	marker = run_directory / _OWNERSHIP_MARKER_NAME
	try:
		metadata = marker.lstat()
		if not _plain_lock_file(marker, metadata) or metadata.st_size > _OWNERSHIP_MARKER_MAX_BYTES:
			return False
		with marker.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(marker, opened):
				return False
			content = stream.read(_OWNERSHIP_MARKER_MAX_BYTES + 1)
	except OSError:
		return False
	if expected_directory is not None:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False
		if (
			is_link_or_reparse(current_directory)
			or not stat.S_ISDIR(current_directory.st_mode)
			or not _same_file_identity(expected_directory, current_directory)
		):
			return False
	return content == _ownership_marker(temp_root, run_directory)


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
	handle = _open_locked_file(path, blocking=True)
	if handle is None:
		raise OSError(f"could not acquire temporary workspace lock: {path}")
	try:
		yield
	finally:
		_unlock(handle)


def _create_run_directory(
	temp_root: Path,
) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
	root_lock_path = temp_root / _ROOT_LOCK_NAME
	with _locked_file(root_lock_path):
		_clean_stale_entries(temp_root)
		run_directory = Path(tempfile.mkdtemp(prefix="qg-", dir=temp_root))
		run_identity: os.stat_result | None = None
		owner_identity: os.stat_result | None = None
		active_lock: BinaryIO | None = None
		try:
			run_identity = run_directory.lstat()
			active_lock = _open_locked_file(run_directory / _ACTIVE_LOCK_NAME, blocking=True)
			if active_lock is None:
				raise OSError("could not acquire active repository temp lock")
			with (run_directory / _OWNERSHIP_MARKER_NAME).open("xb") as marker:
				marker.write(_ownership_marker(temp_root, run_directory))
				marker.flush()
			owner_identity = _write_ownership_record(temp_root, run_directory, run_identity)
		except OSError:
			if active_lock is not None:
				_unlock(active_lock)
			if run_identity is not None:
				_remove_entry(run_directory, expected_identity=run_identity)
			if owner_identity is not None and run_identity is not None:
				_remove_ownership_record(
					temp_root,
					run_directory,
					run_identity,
					expected_record=owner_identity,
				)
			raise
		if run_identity is None or owner_identity is None:
			raise OSError("temporary workspace identity could not be captured")
	return run_directory, root_lock_path, active_lock, run_identity, owner_identity


def _prepare_workspace(
	root: Path,
) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
	temp_root = root / TEMP_DIRECTORY_NAME
	try:
		_check_ordinary_directory(temp_root, allow_missing=True)
		temp_root.mkdir(parents=True, exist_ok=True)
		_check_ordinary_directory(temp_root)
		return _create_run_directory(temp_root)
	except (OSError, RuntimeError) as error:
		raise TemporaryWorkspaceError(
			f"could not prepare repository-local temporary workspace under {root}: {error}"
		) from error


def _fallback_workspace_root(root: Path) -> Path:
	user_identity = (
		os.environ.get("USER")
		or os.environ.get("USERNAME")
		or os.environ.get("LOGNAME")
		or os.environ.get("USERPROFILE")
		or os.environ.get("HOME")
		or str(SYSTEM_TEMP_DIRECTORY)
	)
	user_key = hashlib.sha256(os.path.normcase(user_identity).encode("utf-8")).hexdigest()[:16]
	root_key = os.path.normcase(str(root)).encode("utf-8")
	root_hash = hashlib.sha256(root_key).hexdigest()[:20]
	user_root = SYSTEM_TEMP_DIRECTORY / f"quality-gate-workspaces-{user_key}"
	_check_ordinary_directory(user_root, allow_missing=True)
	user_root.mkdir(mode=0o700, parents=True, exist_ok=True)
	_check_ordinary_directory(user_root)
	fallback_root = user_root / root_hash
	_check_ordinary_directory(fallback_root, allow_missing=True)
	fallback_root.mkdir(mode=0o700, exist_ok=True)
	_check_ordinary_directory(fallback_root)
	return fallback_root


def _prepare_workspace_with_fallback(
	root: Path,
) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
	try:
		return _prepare_workspace(root)
	except TemporaryWorkspaceError as error:
		cause = error.__cause__
		if not isinstance(cause, OSError) or cause.errno not in {
			errno.EACCES,
			errno.EPERM,
			errno.EROFS,
		}:
			raise
	try:
		fallback_root = _fallback_workspace_root(root)
		return _prepare_workspace(fallback_root)
	except OSError as fallback_error:
		raise TemporaryWorkspaceError(
			f"repository temp under {root} is not writable and its per-repository "
			f"system-temp fallback failed: {fallback_error}"
		) from fallback_error


def _set_process_workspace(
	run_directory: Path,
) -> tuple[str | None, dict[str, str | None]]:
	previous_tempdir = tempfile.tempdir
	environment_keys = (*_TEMP_ENVIRONMENT, _SYSTEM_TEMP_ENVIRONMENT)
	previous_environment = {key: os.environ.get(key) for key in environment_keys}
	tempfile.tempdir = str(run_directory)
	for key in _TEMP_ENVIRONMENT:
		os.environ[key] = str(run_directory)
	os.environ.setdefault(_SYSTEM_TEMP_ENVIRONMENT, str(SYSTEM_TEMP_DIRECTORY))
	return previous_tempdir, previous_environment


def _restore_process_workspace(
	previous_tempdir: str | None,
	previous_environment: dict[str, str | None],
) -> None:
	tempfile.tempdir = previous_tempdir
	for key, value in previous_environment.items():
		if value is None:
			os.environ.pop(key, None)
		else:
			os.environ[key] = value


def _remove_workspace(
	root_lock_path: Path,
	run_directory: Path,
	active_lock: BinaryIO,
	run_identity: os.stat_result,
	owner_identity: os.stat_result,
) -> None:
	try:
		with _locked_file(root_lock_path):
			_unlock(active_lock)
			_remove_entry(run_directory, expected_identity=run_identity)
			_remove_ownership_record(
				root_lock_path.parent,
				run_directory,
				run_identity,
				expected_record=owner_identity,
			)
	except OSError as error:
		raise TemporaryWorkspaceError(
			f"could not remove repository-local temporary workspace {run_directory}: {error}"
		) from error


@contextmanager
def temporary_workspace(root: Path) -> Iterator[Path]:
	"""Use repository-local temp, with a system-temp fallback for read-only roots."""
	try:
		actual_root = root.expanduser().resolve()
	except (OSError, RuntimeError) as error:
		raise TemporaryWorkspaceError(
			f"could not resolve repository root for temporary workspace: {root}"
		) from error
	current = _active_workspace.get()
	if current is not None and current[0] == actual_root:
		yield current[1]
		return

	with _process_lock:
		run_directory, root_lock_path, active_lock, run_identity, owner_identity = (
			_prepare_workspace_with_fallback(actual_root)
		)
		previous_tempdir, previous_environment = _set_process_workspace(run_directory)
		token = _active_workspace.set((actual_root, run_directory))
		try:
			yield run_directory
		finally:
			_restore_process_workspace(previous_tempdir, previous_environment)
			_active_workspace.reset(token)
			_remove_workspace(
				root_lock_path, run_directory, active_lock, run_identity, owner_identity
			)
