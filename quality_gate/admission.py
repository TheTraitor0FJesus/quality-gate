"""Machine-local admission for independent full Quality Gate runs."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from .distribution import default_cache_root

QUEUE_TIMEOUT_ENV = "QUALITY_GATE_QUEUE_TIMEOUT_SECONDS"
DEFAULT_QUEUE_TIMEOUT_SECONDS = 120
MAX_QUEUE_TIMEOUT_SECONDS = 240
SLOT_COUNT = 2
POLL_INTERVAL_SECONDS = 0.1
WAIT_NOTICE_INTERVAL_SECONDS = 30
RELEASE_CONTROL_WAIT_SECONDS = 0.5
_SLOT_LOCK_NAME = ".gate-admission-{slot}.lock"
_CONTROL_LOCK_NAME = ".gate-admission-control.lock"
_RECORD_SIZE = 128
# Windows denies second-handle access to the locked byte range.
_SLOT_LOCK_OFFSET = _RECORD_SIZE
_LEASE_ROOT_ENVIRONMENT = "QUALITY_GATE_ADMISSION_ROOT"
_LEASE_SLOT_ENVIRONMENT = "QUALITY_GATE_ADMISSION_SLOT"
_LEASE_PROOF_ENVIRONMENT = "QUALITY_GATE_ADMISSION_TOKEN"
_CI_ENVIRONMENT = "QUALITY_GATE_ADMISSION_CI"
_LEASE_ENVIRONMENT = (
	_LEASE_SLOT_ENVIRONMENT,
	_LEASE_PROOF_ENVIRONMENT,
	_LEASE_ROOT_ENVIRONMENT,
)
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SLOT_PATTERN = re.compile(r"^[01]$")
_RECORD_PATTERN = re.compile(r"^QG1 ([1-9][0-9]{0,9}) ([0-9a-f]{64})$")
_active_lease: ContextVar[_Lease | None] = ContextVar("quality_gate_active_lease", default=None)


class AdmissionError(RuntimeError):
	"""A local Quality Gate run could not enter the machine-wide queue."""

	def __init__(self, message: str, recovery_action: str) -> None:
		super().__init__(message)
		self.recovery_action = recovery_action


class AdmissionConfigurationError(AdmissionError):
	"""A local queue setting or inherited admission identity is invalid."""


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


@dataclass(slots=True)
class _Lease:
	root: Path
	slot: int
	token: str
	handle: BinaryIO | None


def queue_timeout_seconds(environment: Mapping[str, str] | None = None) -> int:
	"""Read the bounded local wait budget, separate from the Gate execution budget."""
	selected = os.environ if environment is None else environment
	raw = selected.get(QUEUE_TIMEOUT_ENV)
	if raw is None:
		return DEFAULT_QUEUE_TIMEOUT_SECONDS
	if not re.fullmatch(r"[0-9]{1,3}", raw):
		raise AdmissionConfigurationError(
			f"{QUEUE_TIMEOUT_ENV} must be an integer from 0 through {MAX_QUEUE_TIMEOUT_SECONDS}",
			f"set {QUEUE_TIMEOUT_ENV} to a whole number from 0 through {MAX_QUEUE_TIMEOUT_SECONDS}",
		)
	seconds = int(raw)
	if seconds > MAX_QUEUE_TIMEOUT_SECONDS:
		raise AdmissionConfigurationError(
			f"{QUEUE_TIMEOUT_ENV} must be an integer from 0 through {MAX_QUEUE_TIMEOUT_SECONDS}",
			f"set {QUEUE_TIMEOUT_ENV} to a whole number from 0 through {MAX_QUEUE_TIMEOUT_SECONDS}",
		)
	return seconds


def running_in_ci(environment: Mapping[str, str] | None = None) -> bool:
	"""Keep local queue waits out of hosted CI jobs with their own deadlines."""
	selected = os.environ if environment is None else environment
	return any(
		selected.get(name, "").casefold() in {"1", "true", "yes", "on"}
		for name in ("CI", "GITHUB_ACTIONS", _CI_ENVIRONMENT)
	)


def child_admission_environment() -> dict[str, str]:
	"""Preserve CI admission context or a live lease for Gate descendants."""
	environment = {_CI_ENVIRONMENT: "1"} if running_in_ci() else {}
	lease = _active_lease.get()
	if lease is None:
		return environment
	return environment | {
		_LEASE_ROOT_ENVIRONMENT: str(lease.root),
		_LEASE_SLOT_ENVIRONMENT: str(lease.slot),
		_LEASE_PROOF_ENVIRONMENT: lease.token,
	}


def _slot_path(root: Path, slot: int) -> Path:
	return root / _SLOT_LOCK_NAME.format(slot=slot)


def _open_lock_file(path: Path) -> BinaryIO:
	if path.is_symlink():
		raise OSError(f"refusing a symbolic link used as a gate admission lock: {path}")
	flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
	if hasattr(os, "O_NOFOLLOW"):
		flags |= os.O_NOFOLLOW
	descriptor = os.open(path, flags, 0o600)
	try:
		if not stat.S_ISREG(os.fstat(descriptor).st_mode):
			raise OSError(f"gate admission lock is not a regular file: {path}")
		handle = os.fdopen(descriptor, "r+b", buffering=0)
	except OSError:
		os.close(descriptor)
		raise
	try:
		handle.seek(0, os.SEEK_END)
		if handle.tell() == 0:
			handle.write(b"\0")
			handle.flush()
		return handle
	except OSError:
		handle.close()
		raise


def _is_lock_contention(error: OSError) -> bool:
	if error.errno in {
		errno.EACCES,
		errno.EAGAIN,
		errno.EBUSY,
		getattr(errno, "EDEADLK", errno.EBUSY),
		getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
	}:
		return True
	return getattr(error, "winerror", None) in {33, 36}


def _try_lock(handle: BinaryIO, *, lock_offset: int = 0) -> bool:
	handle.seek(lock_offset)
	try:
		if os.name == "nt":
			msvcrt = cast(_WindowsLockModule, import_module("msvcrt"))
			msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
		else:
			fcntl = cast(_PosixLockModule, import_module("fcntl"))
			fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
	except OSError as error:
		if _is_lock_contention(error):
			return False
		raise
	return True


def _try_open_locked_file(path: Path, *, lock_offset: int = 0) -> BinaryIO | None:
	handle = _open_lock_file(path)
	try:
		if _try_lock(handle, lock_offset=lock_offset):
			return handle
	except BaseException:
		handle.close()
		raise
	handle.close()
	return None


def _try_open_slot(root: Path, slot: int) -> BinaryIO | None:
	return _try_open_locked_file(_slot_path(root, slot), lock_offset=_SLOT_LOCK_OFFSET)


def _unlock(handle: BinaryIO, *, lock_offset: int = 0) -> None:
	try:
		handle.seek(lock_offset)
		if os.name == "nt":
			msvcrt = cast(_WindowsLockModule, import_module("msvcrt"))
			msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
		else:
			fcntl = cast(_PosixLockModule, import_module("fcntl"))
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
	finally:
		handle.close()


def _write_lease(handle: BinaryIO, token: str) -> None:
	digest = hashlib.sha256(token.encode("ascii")).hexdigest()
	record = f"QG1 {os.getpid()} {digest}".encode("ascii")
	if len(record) + 1 > _RECORD_SIZE:
		raise OSError("gate admission identity exceeds its record size")
	handle.seek(0)
	handle.write(record + b"\n" + b"\0" * (_RECORD_SIZE - len(record) - 1))
	handle.flush()


def _clear_lease(handle: BinaryIO) -> None:
	handle.seek(0)
	handle.write(b"\0" * _RECORD_SIZE)
	handle.flush()


def _read_lease(path: Path) -> tuple[int, str] | None:
	try:
		# Buffered read-ahead can touch the following Windows-locked byte.
		with path.open("rb", buffering=0) as stream:
			raw = stream.read(_RECORD_SIZE)
	except FileNotFoundError:
		return None
	line = raw.split(b"\0", 1)[0].decode("ascii", errors="ignore").strip()
	match = _RECORD_PATTERN.fullmatch(line)
	if match is None:
		return None
	return int(match.group(1)), match.group(2)


def _parse_inherited_identity(
	environment: Mapping[str, str], requested_root: Path | None
) -> tuple[Path, int, str] | None:
	raw_slot, raw_token, raw_root = (environment.get(name) for name in _LEASE_ENVIRONMENT)
	if raw_slot is None and raw_token is None and raw_root is None:
		return None
	if raw_slot is None or raw_token is None or raw_root is None:
		raise AdmissionConfigurationError(
			"inherited local gate admission identity is incomplete",
			"remove the stale QUALITY_GATE_ADMISSION_* environment values and run the check again",
		)
	if not _SLOT_PATTERN.fullmatch(raw_slot) or not _TOKEN_PATTERN.fullmatch(raw_token):
		raise AdmissionConfigurationError(
			"inherited local gate admission identity is malformed",
			"remove the stale QUALITY_GATE_ADMISSION_* environment values and run the check again",
		)
	try:
		inherited_root = Path(raw_root).expanduser()
		if not inherited_root.is_absolute() or inherited_root.is_symlink():
			raise ValueError("admission root must be a canonical absolute directory")
		root = inherited_root.resolve()
		if inherited_root != root:
			raise ValueError("admission root must be canonical")
	except (OSError, RuntimeError, ValueError) as error:
		raise AdmissionConfigurationError(
			"inherited local gate admission root is malformed",
			"remove the stale QUALITY_GATE_ADMISSION_* environment values and run the check again",
		) from error
	if requested_root is not None and root != requested_root:
		# An explicit cache root is an isolated provider/test namespace.
		return None
	return root, int(raw_slot), raw_token


def _inherited_lease(root: Path, slot: int, token: str) -> _Lease | None:
	if not root.is_dir():
		return None
	control = _try_open_locked_file(root / _CONTROL_LOCK_NAME)
	if control is None:
		return None
	try:
		slot_handle = _try_open_slot(root, slot)
		if slot_handle is not None:
			_unlock(slot_handle, lock_offset=_SLOT_LOCK_OFFSET)
			return None
		record = _read_lease(_slot_path(root, slot))
		digest = hashlib.sha256(token.encode("ascii")).hexdigest()
		if record is None or record[1] != digest:
			return None
		return _Lease(root, slot, token, None)
	finally:
		_unlock(control)


def _try_admission(root: Path) -> _Lease | None:
	control = _try_open_locked_file(root / _CONTROL_LOCK_NAME)
	if control is None:
		return None
	try:
		for slot in range(SLOT_COUNT):
			slot_handle = _try_open_slot(root, slot)
			if slot_handle is None:
				continue
			try:
				token = os.urandom(16).hex()
				_write_lease(slot_handle, token)
				return _Lease(root, slot, token, slot_handle)
			except BaseException:
				_unlock(slot_handle, lock_offset=_SLOT_LOCK_OFFSET)
				raise
		return None
	finally:
		_unlock(control)


def _emit_waiting() -> None:
	sys.stderr.write(
		"QUALITY GATE: waiting for a local run slot; this check has not started and "
		"will continue automatically when a slot is released.\n"
	)
	sys.stderr.flush()


def _emit_progress() -> None:
	sys.stderr.write("QUALITY GATE: still waiting for a local run slot.\n")
	sys.stderr.flush()


def _emit_acquired() -> None:
	sys.stderr.write("QUALITY GATE: local run slot acquired; continuing the check.\n")
	sys.stderr.flush()


def _announce_acquired(lease: _Lease, waited: bool) -> _Lease:
	if not waited:
		return lease
	try:
		_emit_acquired()
	except BaseException as error:
		try:
			_release(lease)
		except BaseException as cleanup_error:
			raise cleanup_error from error
		raise
	return lease


def _acquire(
	root: Path,
	timeout_seconds: int,
	inherited_identity: tuple[Path, int, str] | None,
) -> _Lease:
	root.mkdir(parents=True, exist_ok=True)
	if root.is_symlink() or not root.is_dir():
		raise OSError(f"gate admission cache root is not a regular directory: {root}")
	deadline = time.monotonic() + timeout_seconds
	waited = False
	next_notice = 0.0
	while True:
		lease = _inherited_lease(*inherited_identity) if inherited_identity is not None else None
		if lease is None:
			lease = _try_admission(root)
		if lease is not None:
			return _announce_acquired(lease, waited)
		now = time.monotonic()
		if not waited:
			_emit_waiting()
			waited = True
			next_notice = now + WAIT_NOTICE_INTERVAL_SECONDS
		elif now >= next_notice:
			_emit_progress()
			next_notice = now + WAIT_NOTICE_INTERVAL_SECONDS
		if now >= deadline:
			raise AdmissionError(
				(
					f"local Quality Gate queue wait exceeded {timeout_seconds} seconds; "
					"no check was run"
				),
				(
					"free a local run slot or increase "
					"QUALITY_GATE_QUEUE_TIMEOUT_SECONDS, then run verification again"
				),
			)
		try:
			time.sleep(min(POLL_INTERVAL_SECONDS, deadline - now))
		except KeyboardInterrupt as error:
			sys.stderr.write("QUALITY GATE: local queue wait cancelled; no check was run.\n")
			sys.stderr.flush()
			raise AdmissionError(
				"local Quality Gate queue wait was cancelled; no check was run",
				"start verification again when ready",
			) from error


def _wait_for_control(root: Path) -> BinaryIO | None:
	deadline = time.monotonic() + RELEASE_CONTROL_WAIT_SECONDS
	while True:
		handle = _try_open_locked_file(root / _CONTROL_LOCK_NAME)
		if handle is not None:
			return handle
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			return None
		time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _unlock_preserving_error(
	handle: BinaryIO, error: BaseException | None, *, lock_offset: int = 0
) -> BaseException | None:
	try:
		_unlock(handle, lock_offset=lock_offset)
	except BaseException as unlock_error:
		return error or unlock_error
	return error


def _release(lease: _Lease) -> None:
	if lease.handle is None:
		return
	control: BinaryIO | None = None
	release_error: BaseException | None = None
	try:
		control = _wait_for_control(lease.root)
		if control is not None:
			_clear_lease(lease.handle)
	except BaseException as error:
		release_error = error
	release_error = _unlock_preserving_error(
		lease.handle, release_error, lock_offset=_SLOT_LOCK_OFFSET
	)
	if control is not None:
		release_error = _unlock_preserving_error(control, release_error)
	if release_error is not None:
		raise release_error
	if control is None:
		# The OS lock is authoritative; the next owner replaces this stale record.
		sys.stderr.write(
			"QUALITY GATE: released a local slot after bounded cleanup; "
			"stale lease data will be replaced on the next admission.\n"
		)
		sys.stderr.flush()


@contextmanager
def gate_admission(*, cache_root: Path | None = None) -> Iterator[None]:
	"""Hold one of two machine-local slots for one complete Gate run."""
	queue_timeout = queue_timeout_seconds()
	requested_root = cache_root.expanduser().resolve() if cache_root is not None else None
	active_lease = _active_lease.get()
	if active_lease is not None:
		if requested_root is not None and requested_root != active_lease.root:
			raise AdmissionConfigurationError(
				"nested local gate admission requested a different cache root",
				"reuse the active gate admission root for nested verification",
			)
		yield
		return
	inherited = _parse_inherited_identity(os.environ, requested_root)
	root = (requested_root or default_cache_root()).expanduser().resolve()
	try:
		lease = _acquire(root, queue_timeout, inherited)
	except OSError as error:
		raise AdmissionError(
			f"could not use the machine-local Quality Gate queue: {error}",
			"restore access to the user-local Quality Gate cache and run verification again",
		) from error
	context_token: Token[_Lease | None] = _active_lease.set(lease)
	try:
		yield
	finally:
		_active_lease.reset(context_token)
		try:
			_release(lease)
		except OSError as error:
			raise AdmissionError(
				f"could not release the machine-local Quality Gate slot: {error}",
				"restore access to the user-local Quality Gate cache and run verification again",
			) from error
