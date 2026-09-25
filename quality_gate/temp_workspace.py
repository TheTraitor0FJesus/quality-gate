"""Repository-local scratch space for Quality Gate executions."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast

TEMP_DIRECTORY_NAME = "temp"
_SYSTEM_TEMP_ENVIRONMENT = "QUALITY_GATE_SYSTEM_TEMP"
SYSTEM_TEMP_DIRECTORY = Path(os.environ.get(_SYSTEM_TEMP_ENVIRONMENT) or tempfile.gettempdir())
_ROOT_LOCK_NAME = ".quality-gate.lock"
_ACTIVE_LOCK_NAME = ".active.lock"
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


def _open_locked_file(path: Path, *, blocking: bool) -> BinaryIO | None:
	if path.is_symlink():
		raise OSError(f"refusing a symbolic link used as a temporary lock: {path}")
	handle = path.open("a+b")
	try:
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


def _remove_entry(path: Path) -> None:
	if path.is_symlink() or not path.is_dir():
		path.unlink()
	else:
		shutil.rmtree(path, onerror=_retry_readonly_entry)


def _retry_readonly_entry(
	function: Callable[..., object], path: str, error_info: tuple[object, ...]
) -> None:
	error = error_info[1]
	if isinstance(error, PermissionError) and not Path(path).is_symlink():
		os.chmod(path, stat.S_IWRITE)
		function(path)
		return
	if isinstance(error, BaseException):
		raise error
	raise OSError(f"could not remove temporary workspace entry: {path}")


def _clean_stale_entries(temp_root: Path) -> None:
	for entry in tuple(temp_root.iterdir()):
		if entry.name == _ROOT_LOCK_NAME:
			continue
		active_lock_path = entry / _ACTIVE_LOCK_NAME
		if not entry.is_symlink() and entry.is_dir() and active_lock_path.is_file():
			active = _open_locked_file(active_lock_path, blocking=False)
			if active is None:
				continue
			_unlock(active)
		_remove_entry(entry)


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
	handle = _open_locked_file(path, blocking=True)
	if handle is None:
		raise OSError(f"could not acquire temporary workspace lock: {path}")
	try:
		yield
	finally:
		_unlock(handle)


def _create_run_directory(temp_root: Path) -> tuple[Path, Path, BinaryIO]:
	root_lock_path = temp_root / _ROOT_LOCK_NAME
	with _locked_file(root_lock_path):
		_clean_stale_entries(temp_root)
		run_directory = Path(tempfile.mkdtemp(prefix="qg-", dir=temp_root))
		try:
			active_lock = _open_locked_file(run_directory / _ACTIVE_LOCK_NAME, blocking=True)
			if active_lock is None:
				raise OSError("could not acquire active repository temp lock")
		except OSError:
			_remove_entry(run_directory)
			raise
	return run_directory, root_lock_path, active_lock


def _prepare_workspace(root: Path) -> tuple[Path, Path, BinaryIO]:
	temp_root = root / TEMP_DIRECTORY_NAME
	if temp_root.is_symlink():
		raise TemporaryWorkspaceError(
			f"refusing a symbolic link as the repository temp directory: {temp_root}"
		)
	try:
		temp_root.mkdir(parents=True, exist_ok=True)
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
	if user_root.is_symlink():
		raise OSError(f"refusing a symbolic link as the system-temp workspace root: {user_root}")
	user_root.mkdir(mode=0o700, parents=True, exist_ok=True)
	fallback_root = user_root / root_hash
	if fallback_root.is_symlink():
		raise OSError(f"refusing a symbolic link as the repository fallback workspace: {root}")
	fallback_root.mkdir(mode=0o700, exist_ok=True)
	return fallback_root


def _prepare_workspace_with_fallback(root: Path) -> tuple[Path, Path, BinaryIO]:
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
) -> None:
	try:
		with _locked_file(root_lock_path):
			_unlock(active_lock)
			_remove_entry(run_directory)
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
		run_directory, root_lock_path, active_lock = _prepare_workspace_with_fallback(actual_root)
		previous_tempdir, previous_environment = _set_process_workspace(run_directory)
		token = _active_workspace.set((actual_root, run_directory))
		try:
			yield run_directory
		finally:
			_restore_process_workspace(previous_tempdir, previous_environment)
			_active_workspace.reset(token)
			_remove_workspace(root_lock_path, run_directory, active_lock)
