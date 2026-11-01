"""Cross-process file locks for short-lived and bounded cache ownership."""

from __future__ import annotations

import math
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from .path_safety import is_link_or_reparse


class FileLockUnavailableError(OSError):
	"""A lock could not be acquired within its caller's deadline."""


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


def lock_file(handle: BinaryIO, *, blocking: bool) -> None:
	"""Acquire an OS lock on an already-open file handle."""
	_lock(handle, blocking=blocking)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
	return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _plain_lock_file(path: Path, metadata: os.stat_result) -> bool:
	return (
		stat.S_ISREG(metadata.st_mode)
		and metadata.st_nlink == 1
		and not is_link_or_reparse(metadata)
		and not path.is_symlink()
	)


def _ensure_safe_parent_directories(path: Path) -> None:
	for parent in reversed(path.parents):
		try:
			metadata = parent.lstat()
		except FileNotFoundError:
			try:
				parent.mkdir()
			except FileExistsError:
				pass
			metadata = parent.lstat()
		if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
			raise OSError(f"refusing an unsafe directory in lock path: {parent}")


def open_locked_file(path: Path, *, blocking: bool) -> BinaryIO | None:
	"""Open and lock one file, returning None when a nonblocking lock is busy."""
	_ensure_safe_parent_directories(path)
	try:
		before = path.lstat()
	except FileNotFoundError:
		before = None
	if before is not None and not _plain_lock_file(path, before):
		raise OSError(f"refusing a link or reparse point or unsafe file used as a lock: {path}")
	_ensure_safe_parent_directories(path)
	handle = path.open("a+b")
	try:
		opened = os.fstat(handle.fileno())
		try:
			current = path.lstat()
		except OSError as error:
			raise OSError(f"lock path changed while opening: {path}") from error
		if (
			not _plain_lock_file(path, opened)
			or not _plain_lock_file(path, current)
			or not _same_file_identity(opened, current)
			or (before is not None and not _same_file_identity(before, opened))
		):
			raise OSError(f"lock path changed while opening: {path}")
		handle.seek(0, os.SEEK_END)
		if handle.tell() == 0:
			handle.write(b"\0")
			handle.flush()
		_lock(handle, blocking=blocking)
		return handle
	except OSError:
		handle.close()
		if blocking:
			raise
		return None


def unlock_file(handle: BinaryIO) -> None:
	"""Release a lock and close its file handle."""
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


@contextmanager
def locked_file(path: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
	"""Hold an exclusive file lock, waiting no longer than the supplied timeout."""
	if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds < 0):
		raise ValueError("lock timeout must be a finite nonnegative number")

	if timeout_seconds is None:
		handle = open_locked_file(path, blocking=True)
		if handle is None:
			raise FileLockUnavailableError(f"could not acquire lock: {path}")
	else:
		deadline = time.monotonic() + timeout_seconds
		handle = None
		while handle is None:
			handle = open_locked_file(path, blocking=False)
			if handle is not None:
				break
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise FileLockUnavailableError(f"lock is busy: {path}")
			time.sleep(min(0.05, remaining))

	try:
		yield
	finally:
		unlock_file(handle)
