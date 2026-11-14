"""Quality Gate scratch space."""

from __future__ import annotations

import errno
import hashlib
import math
import os
import sys
import tempfile
import threading
import time as time
from collections.abc import Callable
from collections.abc import Iterator as Iterator
from contextlib import contextmanager as contextmanager
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
from .workspace_locks import _check_workspace_deadline, _open_locked_file
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
def _locked_file(path: Path, *, deadline: float | None = None) -> Iterator[None]:
	try:
		handle = _open_locked_file(path, blocking=True, deadline=deadline)
	except TimeoutError:
		raise
	except OSError as error:
		if "symbolic link" in str(error):
			raise
		raise OSError(f"could not acquire temporary workspace lock: {path}") from error
	if handle is None:
		raise OSError(f"could not acquire temporary workspace lock: {path}")
	try:
		yield
	finally:
		_unlock(handle)


def _create_run_directory(
	temp_root: Path,
	*,
	deadline: float | None = None,
) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
	if _ownership_key(create=True) is None:
		raise OSError(f"temporary workspace ownership key is unavailable: {temp_root}")
	root_lock_path = temp_root / _ROOT_LOCK_NAME
	stale_entries_reclaimed = _bounded_stale_workspace_cleanup(temp_root, deadline)
	with _locked_file(root_lock_path, deadline=deadline):
		if not stale_entries_reclaimed:
			_clean_stale_entries(temp_root, deadline=deadline)
		_check_workspace_deadline(deadline)
		run_directory = Path(tempfile.mkdtemp(prefix="qg-", dir=temp_root))
		run_identity: os.stat_result | None = None
		owner_identity: os.stat_result | None = None
		active_lock: BinaryIO | None = None
		try:
			run_identity = run_directory.lstat()
			active_lock = _open_locked_file(
				run_directory / _ACTIVE_LOCK_NAME, blocking=True, deadline=deadline
			)
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
	*,
	deadline: float | None = None,
) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
	_check_workspace_deadline(deadline)
	temp_root = root / TEMP_DIRECTORY_NAME
	try:
		_check_ordinary_directory(temp_root, allow_missing=True)
		_check_workspace_deadline(deadline)
		temp_root.mkdir(parents=True, exist_ok=True)
		_check_ordinary_directory(temp_root)
		return _create_run_directory(temp_root, deadline=deadline)
	except (OSError, RuntimeError) as error:
		_raise_operation_timeout_if_expired(deadline, error)
		raise TemporaryWorkspaceError(
			f"could not prepare repository-local temporary workspace under {root}: {error}"
		) from error


def _fallback_workspace_root(root: Path, *, deadline: float | None = None) -> Path:
	_check_workspace_deadline(deadline)
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
	_check_workspace_deadline(deadline)
	user_root.mkdir(mode=0o700, parents=True, exist_ok=True)
	_check_ordinary_directory(user_root)
	_check_workspace_deadline(deadline)
	fallback_root = user_root / root_hash
	_check_ordinary_directory(fallback_root, allow_missing=True)
	fallback_root.mkdir(mode=0o700, exist_ok=True)
	_check_ordinary_directory(fallback_root)
	_check_workspace_deadline(deadline)
	return fallback_root


def _prepare_workspace_with_fallback(
	root: Path,
	*,
	deadline: float | None = None,
) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
	try:
		return _prepare_workspace(root, deadline=deadline)
	except TemporaryWorkspaceError as error:
		cause = error.__cause__
		if not isinstance(cause, OSError) or cause.errno not in {
			errno.EACCES,
			errno.EPERM,
			errno.EROFS,
		}:
			raise
	try:
		fallback_root = _fallback_workspace_root(root, deadline=deadline)
		return _prepare_workspace(fallback_root, deadline=deadline)
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
def temporary_workspace(
	root: Path,
	*,
	cleanup_owner: Callable[[Path, Path], None] | None = None,
	workspace_registered: Callable[[Path, Path], None] | None = None,
	deadline: float | None = None,
) -> Iterator[Path]:
	"""Use repository-local temp, registering operation ownership before yielding."""
	try:
		if deadline is not None and (
			isinstance(deadline, bool)
			or not isinstance(deadline, (int, float))
			or not math.isfinite(deadline)
		):
			raise ValueError("temporary workspace deadline must be a finite monotonic timestamp")
		deadline = None if deadline is None else float(deadline)
		_check_workspace_deadline(deadline)
		actual_root = root.expanduser().resolve()
	except (OSError, RuntimeError) as error:
		raise TemporaryWorkspaceError(
			f"could not resolve repository root for temporary workspace: {root}"
		) from error
	current = _active_workspace.get()
	if current is not None and current[0] == actual_root:
		yield current[1]
		return

	with _locked_process_scope(deadline):
		run_directory, root_lock_path, active_lock, run_identity, owner_identity = (
			_prepare_workspace_with_fallback(actual_root, deadline=deadline)
		)
		if workspace_registered is not None:
			try:
				workspace_registered(root_lock_path.parent, run_directory)
			except Exception as error:
				try:
					_remove_workspace(
						root_lock_path,
						run_directory,
						active_lock,
						run_identity,
						owner_identity,
					)
				except TemporaryWorkspaceError as cleanup_error:
					raise TemporaryWorkspaceError(
						"temporary workspace registration and local cleanup failed: "
						f"{run_directory}"
					) from cleanup_error
				raise TemporaryWorkspaceError(
					f"temporary workspace ownership could not be registered: {run_directory}"
				) from error
		previous_tempdir, previous_environment = _set_process_workspace(run_directory)
		token = _active_workspace.set((actual_root, run_directory))
		try:
			yield run_directory
		finally:
			_restore_process_workspace(previous_tempdir, previous_environment)
			_active_workspace.reset(token)
			if cleanup_owner is None:
				_remove_workspace(
					root_lock_path, run_directory, active_lock, run_identity, owner_identity
				)
			else:
				try:
					cleanup_owner(root_lock_path.parent, run_directory)
				except Exception as error:
					raise TemporaryWorkspaceError(
						f"temporary workspace cleanup ownership could not be transferred: "
						f"{run_directory}"
					) from error
				finally:
					_unlock(active_lock)


def _raise_operation_timeout_if_expired(deadline: float | None, error: BaseException) -> None:
	if deadline is None or time.monotonic() < deadline:
		return
	from .operation import OperationTimeoutError, current_operation

	operation = current_operation()
	if operation is None:
		return
	try:
		operation.remaining()
	except OperationTimeoutError as timeout:
		raise timeout from error


@contextmanager
def _locked_process_scope(deadline: float | None) -> Iterator[None]:
	acquired = False
	try:
		if deadline is None:
			_process_lock.acquire()
			acquired = True
		else:
			while not acquired:
				_check_workspace_deadline(deadline)
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					continue
				acquired = _process_lock.acquire(timeout=min(0.05, remaining))
			_check_workspace_deadline(deadline)
	except TimeoutError as error:
		if acquired:
			_process_lock.release()
		_raise_operation_timeout_if_expired(deadline, error)
		raise TemporaryWorkspaceError(
			"temporary workspace process lock wait exceeded its deadline"
		) from error
	try:
		yield
	finally:
		_process_lock.release()


def _clean_stale_workspace_entries(temp_root: Path) -> None:
	_check_ordinary_directory(temp_root)
	with _locked_file(temp_root / _ROOT_LOCK_NAME):
		_clean_stale_entries(temp_root)


def _bounded_stale_workspace_cleanup(temp_root: Path, deadline: float | None) -> bool:
	if deadline is None:
		return False
	from .operation import current_operation

	operation = current_operation()
	if operation is None:
		return False
	_check_workspace_deadline(deadline)
	source_root = Path(__file__).resolve().parent.parent
	body = (
		"import sys\n"
		"from pathlib import Path\n"
		"sys.path.insert(0, sys.argv[1])\n"
		"from quality_gate.temp_workspace import _clean_stale_workspace_entries\n"
		"_clean_stale_workspace_entries(Path(sys.argv[2]))\n"
	)
	try:
		returncode, _output, _diagnostics = operation.run_process(
			[
				sys.executable,
				"-I",
				"-S",
				"-B",
				"-c",
				body,
				str(source_root),
				str(temp_root),
			],
			root=operation.root,
			environment=dict(os.environ),
			timeout_seconds=operation.remaining(),
			limit=4096,
		)
	except Exception as error:
		raise TemporaryWorkspaceError(
			"stale temporary workspace cleanup did not complete within the operation budget"
		) from error
	if returncode != 0:
		raise OSError("stale temporary workspace cleanup failed")
	_check_workspace_deadline(deadline)
	return True


def cleanup_handed_off_workspace(temp_root: Path, run_directory: Path) -> None:
	"""Reclaim one workspace transferred by ``temporary_workspace``."""
	if temp_root.is_symlink() or run_directory.is_symlink():
		raise OSError("temporary workspace cleanup path became a symbolic link")
	try:
		actual_temp_root = temp_root.expanduser().resolve()
		actual_run_directory = run_directory.expanduser().resolve()
	except (OSError, RuntimeError) as error:
		raise OSError("temporary workspace path could not be resolved") from error
	if actual_run_directory.parent != actual_temp_root or not actual_run_directory.name.startswith(
		"qg-"
	):
		raise OSError("temporary workspace cleanup handoff is invalid")
	_check_ordinary_directory(actual_temp_root)
	with _locked_file(actual_temp_root / _ROOT_LOCK_NAME):
		_reclaim_stale_workspace(actual_temp_root, actual_run_directory)
	if os.path.lexists(actual_run_directory):
		raise OSError("handed-off temporary workspace was not safely removed")
