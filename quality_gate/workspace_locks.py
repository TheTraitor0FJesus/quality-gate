"""Cross-platform locks for temporary workspaces."""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from typing import BinaryIO

from .file_lock import lock_file, unlock_file
from .workspace_keys import _plain_lock_file, _same_file_identity


def _lock(handle: BinaryIO, *, blocking: bool) -> None:
	lock_file(handle, blocking=blocking)


def _unlock(handle: BinaryIO) -> None:
	unlock_file(handle)


def _open_locked_file(
	path: Path,
	*,
	blocking: bool,
	create: bool = True,
	deadline: float | None = None,
) -> BinaryIO | None:
	_check_workspace_deadline(deadline)
	before = _existing_lock_file_identity(path, create=create)
	if before is None and not create:
		return None
	handle = path.open("a+b" if create else "r+b")
	try:
		_validate_open_lock_file(path, before, handle)
		_initialize_empty_lock_file(handle)
		if not _acquire_lock_file(handle, blocking=blocking, deadline=deadline):
			handle.close()
			return None
		return handle
	except OSError:
		handle.close()
		raise


def _check_workspace_deadline(deadline: float | None) -> None:
	if deadline is not None and time.monotonic() >= deadline:
		raise TimeoutError("temporary workspace preparation exceeded the operation deadline")


def _lock_until(handle: BinaryIO, deadline: float) -> None:
	contention_errors = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
	if hasattr(errno, "EWOULDBLOCK"):
		contention_errors.add(errno.EWOULDBLOCK)
	last_error: OSError | None = None
	while True:
		_check_workspace_deadline(deadline)
		try:
			_lock(handle, blocking=False)
			_check_workspace_deadline(deadline)
			return
		except OSError as error:
			if error.errno not in contention_errors:
				raise
			last_error = error
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise TimeoutError(
				"temporary workspace lock wait exceeded the operation deadline"
			) from last_error
		time.sleep(min(0.05, remaining))


def _existing_lock_file_identity(path: Path, *, create: bool) -> os.stat_result | None:
	try:
		before = path.lstat()
	except FileNotFoundError:
		before = None
	if before is not None and not _plain_lock_file(path, before):
		raise OSError(f"refusing an unsafe temporary lock file: {path}")
	if before is None and not create:
		return None
	return before


def _validate_open_lock_file(
	path: Path,
	before: os.stat_result | None,
	handle: BinaryIO,
) -> None:
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


def _initialize_empty_lock_file(handle: BinaryIO) -> None:
	handle.seek(0, os.SEEK_END)
	if handle.tell() == 0:
		handle.write(b"\0")
		handle.flush()


def _acquire_lock_file(
	handle: BinaryIO,
	*,
	blocking: bool,
	deadline: float | None,
) -> bool:
	try:
		if blocking and deadline is not None:
			_lock_until(handle, deadline)
		else:
			_lock(handle, blocking=blocking)
	except OSError:
		if blocking:
			raise
		return False
	return True
