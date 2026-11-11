"""Repository-local scratch space for Quality Gate executions."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import BinaryIO

from .workspace_constants import (
	_ACTIVE_LOCK_NAME,
	_OWNERSHIP_MARKER_NAME,
)
from .workspace_constants import (
	_CLEANUP_RECORD_PREFIX as _CLEANUP_RECORD_PREFIX,
)
from .workspace_constants import (
	_CLEANUP_RETRY_PREFIX as _CLEANUP_RETRY_PREFIX,
)
from .workspace_constants import (
	_OWNERSHIP_RECORD_MAX_BYTES as _OWNERSHIP_RECORD_MAX_BYTES,
)
from .workspace_constants import (
	_OWNERSHIP_RECORD_PREFIX as _OWNERSHIP_RECORD_PREFIX,
)
from .workspace_constants import (
	TEMP_DIRECTORY_NAME as TEMP_DIRECTORY_NAME,
)
from .workspace_keys import (
	_OWNERSHIP_KEY_BYTES as _OWNERSHIP_KEY_BYTES,
)
from .workspace_keys import (
	_OWNERSHIP_KEY_MODE as _OWNERSHIP_KEY_MODE,
)
from .workspace_keys import (
	_OWNERSHIP_KEY_NAME as _OWNERSHIP_KEY_NAME,
)
from .workspace_keys import (
	_RECOVERY_KEY_NAME as _RECOVERY_KEY_NAME,
)
from .workspace_keys import (
	_canonical_json as _canonical_json,
)
from .workspace_keys import (
	_current_user_id as _current_user_id,
)
from .workspace_keys import (
	_ensure_posix_private_directory as _ensure_posix_private_directory,
)
from .workspace_keys import (
	_optional_os_flag as _optional_os_flag,
)
from .workspace_keys import (
	_ownership_key as _ownership_key,
)
from .workspace_keys import (
	_plain_lock_file as _plain_lock_file,
)
from .workspace_keys import (
	_publish_new_file as _publish_new_file,
)
from .workspace_keys import (
	_read_ownership_key as _read_ownership_key,
)
from .workspace_keys import (
	_rename_without_replacement as _rename_without_replacement,
)
from .workspace_keys import (
	_same_file_identity as _same_file_identity,
)
from .workspace_keys import (
	_signed_record as _signed_record,
)
from .workspace_keys import (
	_user_state_directory as _user_state_directory,
)
from .workspace_keys import (
	_valid_signed_record as _valid_signed_record,
)
from .workspace_locks import _open_locked_file
from .workspace_locks import _unlock as _unlock
from .workspace_quarantine import (
	_check_ordinary_directory,
)
from .workspace_quarantine import (
	_open_cleanup_area as _open_cleanup_area,
)
from .workspace_reclaim import (
	_clean_stale_entries as _clean_stale_entries,
)
from .workspace_reclaim import (
	_reclaim_stale_workspace as _reclaim_stale_workspace,
)
from .workspace_reclaim import (
	_workspace_ownership_is_current as _workspace_ownership_is_current,
)
from .workspace_records import (
	_cleanup_record_path as _cleanup_record_path,
)
from .workspace_records import (
	_cleanup_retry_path as _cleanup_retry_path,
)
from .workspace_records import (
	_has_valid_cleanup_record as _has_valid_cleanup_record,
)
from .workspace_records import (
	_has_valid_ownership_marker as _has_valid_ownership_marker,
)
from .workspace_records import (
	_has_valid_ownership_record as _has_valid_ownership_record,
)
from .workspace_records import (
	_ownership_marker as _ownership_marker,
)
from .workspace_records import (
	_ownership_record_path as _ownership_record_path,
)
from .workspace_records import (
	_remove_cleanup_record as _remove_cleanup_record,
)
from .workspace_records import (
	_remove_ownership_record as _remove_ownership_record,
)
from .workspace_records import (
	_write_cleanup_record as _write_cleanup_record,
)
from .workspace_records import (
	_write_ownership_record as _write_ownership_record,
)
from .workspace_tree import _remove_entry as _remove_entry

_SYSTEM_TEMP_ENVIRONMENT = "QUALITY_GATE_SYSTEM_TEMP"
SYSTEM_TEMP_DIRECTORY = Path(os.environ.get(_SYSTEM_TEMP_ENVIRONMENT) or tempfile.gettempdir())
_ROOT_LOCK_NAME = ".quality-gate.lock"
_TEMP_ENVIRONMENT = ("TMP", "TEMP", "TMPDIR")
_active_workspace: ContextVar[tuple[Path, Path] | None] = ContextVar(
	"quality_gate_active_workspace", default=None
)
# ponytail: process-global tempfile and environment overrides serialize workspace scopes; use
# per-call temp paths if all scratch-producing APIs later accept explicit directory injection.
_process_lock = threading.RLock()


class TemporaryWorkspaceError(OSError):
	"""Repository-local scratch space could not be prepared or cleaned."""


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
	if _ownership_key(create=True) is None:
		raise OSError(f"temporary workspace ownership key is unavailable: {temp_root}")
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
			if not _workspace_ownership_is_current(
				root_lock_path.parent,
				run_directory,
				run_identity,
				owner_identity,
			):
				raise OSError(
					f"temporary workspace ownership changed before cleanup: {run_directory}"
				)
			active_identity = os.fstat(active_lock.fileno())
			marker_identity = (run_directory / _OWNERSHIP_MARKER_NAME).lstat()
			cleanup_identity = _write_cleanup_record(
				root_lock_path.parent,
				run_directory,
				run_identity,
				active_identity,
				marker_identity,
			)
			if cleanup_identity is None:
				raise OSError(f"temporary workspace cleanup could not be recorded: {run_directory}")
			cleanup_controls = (
				(active_identity.st_dev, active_identity.st_ino),
				(marker_identity.st_dev, marker_identity.st_ino),
			)
			if not _workspace_ownership_is_current(
				root_lock_path.parent,
				run_directory,
				run_identity,
				owner_identity,
				cleanup_identity=cleanup_identity,
				cleanup_controls=cleanup_controls,
			):
				raise OSError(
					f"temporary workspace controls changed before cleanup: {run_directory}"
				)
			_unlock(active_lock)
			_remove_entry(run_directory, expected_identity=run_identity)
			_remove_cleanup_record(
				root_lock_path.parent,
				run_directory,
				run_identity,
				expected_record=cleanup_identity,
			)
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
