"""Cross-platform locks for temporary workspaces."""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from .workspace_keys import _plain_lock_file, _same_file_identity


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
