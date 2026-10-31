"""Repository-local scratch space for Quality Gate executions."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import json
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

from .path_safety import is_link_or_reparse, is_reparse_point

TEMP_DIRECTORY_NAME = "temp"
_SYSTEM_TEMP_ENVIRONMENT = "QUALITY_GATE_SYSTEM_TEMP"
SYSTEM_TEMP_DIRECTORY = Path(os.environ.get(_SYSTEM_TEMP_ENVIRONMENT) or tempfile.gettempdir())
_ROOT_LOCK_NAME = ".quality-gate.lock"
_ACTIVE_LOCK_NAME = ".active.lock"
_OWNERSHIP_MARKER_NAME = ".quality-gate-workspace"
_ROOT_CONTROL_FILE_NAMES = frozenset({_ACTIVE_LOCK_NAME, _OWNERSHIP_MARKER_NAME})
_OWNERSHIP_MARKER_MAX_BYTES = 4096
_OWNERSHIP_RECORD_PREFIX = ".quality-gate-owner-"
_CLEANUP_RECORD_PREFIX = ".quality-gate-cleanup-record-"
_CLEANUP_RETRY_PREFIX = ".quality-gate-cleanup-retry-"
_OWNERSHIP_RECORD_MAX_BYTES = 4096
_CLEANUP_AREA_NAME = ".quality-gate-cleanup"
_CLEANUP_AREA_MARKER_NAME = ".quality-gate-cleanup-area"
_OWNERSHIP_KEY_NAME = "temp-workspace.key"
_RECOVERY_KEY_NAME = "temp-workspace-recovery.key"
_OWNERSHIP_KEY_BYTES = 32
_SIGNATURE_HEX_LENGTH = 64
_OWNERSHIP_KEY_MODE = 0o600
_OWNERSHIP_RECORD_SCHEMA = 2
_CLEANUP_RECORD_SCHEMA = 2
_QUARANTINE_JOURNAL_MAX_BYTES = 4096
_QUARANTINE_JOURNAL_SCHEMA = 1
_WINDOWS_ACL_SIZE_INFORMATION = 2
_WINDOWS_ACE_COUNT = 2
_WINDOWS_ACE_ALLOW = 0
_WINDOWS_ACE_INHERIT_FLAGS = 0x03
_WINDOWS_ALL_ACCESS = 0x001F01FF
_WINDOWS_LOCAL_SYSTEM_SID = "S-1-5-18"
_WINDOWS_ERROR_ALREADY_EXISTS = 183
_WINDOWS_DACL_SECURITY_INFORMATION = 0x00000004
_WINDOWS_DACL_PROTECTED = 0x1000
_WINDOWS_TOKEN_QUERY = 0x0008
_WINDOWS_TOKEN_USER_INFORMATION = 1
_WINDOWS_DELETE_ACCESS = 0x00010000
_WINDOWS_READ_ATTRIBUTES_ACCESS = 0x00000080
_WINDOWS_WRITE_ATTRIBUTES_ACCESS = 0x00000100
_WINDOWS_SHARE_READ = 0x00000001
_WINDOWS_SHARE_WRITE = 0x00000002
_WINDOWS_SHARE_DELETE = 0x00000004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_ATTRIBUTE_READONLY = 0x00000001
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_FILE_BASIC_INFORMATION = 0
_WINDOWS_FILE_DISPOSITION_INFORMATION = 4
_WINDOWS_FILE_ID_INFORMATION = 18
_PUBLICATION_LINK_SETTLE_ATTEMPTS = 100
_PUBLICATION_LINK_SETTLE_DELAY_SECONDS = 0.001
_PUBLICATION_TRANSITION_LINK_COUNT = 2
_PRIVATE_DIRECTORY_OWNER_ACCESS = 0o300
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


def _optional_os_flag(name: str) -> int:
	return cast(int, getattr(os, name, 0))


def _current_user_id() -> int:
	get_uid_name = "getuid"
	get_uid = cast(Any, getattr(os, get_uid_name))
	return cast(int, get_uid())


def _canonical_json(value: dict[str, object]) -> bytes:
	return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _user_state_directory() -> Path:
	if os.name == "nt":
		base = os.environ.get("LOCALAPPDATA")
		if not base:
			base = str(Path.home() / "AppData" / "Local")
	else:
		base = os.environ.get("XDG_STATE_HOME")
		if not base or not Path(base).is_absolute():
			base = str(Path.home() / ".local" / "state")
	return Path(base) / "quality-gate" / "temp-workspace"


def _windows_security_api() -> tuple[Any, Any]:
	loader_name = "WinDLL"
	loader = cast(Any, getattr(ctypes, loader_name))
	kernel32 = cast(Any, loader("kernel32", use_last_error=True))
	advapi32 = cast(Any, loader("advapi32", use_last_error=True))
	from ctypes import wintypes

	get_process = cast(Any, kernel32.GetCurrentProcess)
	get_process.restype = wintypes.HANDLE
	create_file = cast(Any, kernel32.CreateFileW)
	create_file.argtypes = [
		wintypes.LPCWSTR,
		wintypes.DWORD,
		wintypes.DWORD,
		ctypes.c_void_p,
		wintypes.DWORD,
		wintypes.DWORD,
		wintypes.HANDLE,
	]
	create_file.restype = wintypes.HANDLE
	get_file_information = cast(Any, kernel32.GetFileInformationByHandle)
	get_file_information.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
	get_file_information.restype = wintypes.BOOL
	get_file_information_ex = cast(Any, kernel32.GetFileInformationByHandleEx)
	get_file_information_ex.argtypes = [
		wintypes.HANDLE,
		wintypes.DWORD,
		ctypes.c_void_p,
		wintypes.DWORD,
	]
	get_file_information_ex.restype = wintypes.BOOL
	set_file_information = cast(Any, kernel32.SetFileInformationByHandle)
	set_file_information.argtypes = [
		wintypes.HANDLE,
		wintypes.DWORD,
		ctypes.c_void_p,
		wintypes.DWORD,
	]
	set_file_information.restype = wintypes.BOOL
	close_handle = cast(Any, kernel32.CloseHandle)
	close_handle.argtypes = [wintypes.HANDLE]
	close_handle.restype = wintypes.BOOL
	local_free = cast(Any, kernel32.LocalFree)
	local_free.argtypes = [ctypes.c_void_p]
	local_free.restype = ctypes.c_void_p
	return kernel32, advapi32


def _windows_error(code: int | None = None) -> OSError:
	win_error_name = "WinError"
	last_error_name = "get_last_error"
	win_error = cast(Any, getattr(ctypes, win_error_name))
	get_last_error = cast(Any, getattr(ctypes, last_error_name))
	return cast(OSError, win_error(get_last_error() if code is None else code))


def _windows_user_sid() -> str:
	from ctypes import wintypes

	class SidAndAttributes(ctypes.Structure):
		_fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

	class TokenUser(ctypes.Structure):
		_fields_ = [("user", SidAndAttributes)]

	kernel32, advapi32 = _windows_security_api()
	open_process_token = cast(Any, advapi32.OpenProcessToken)
	open_process_token.argtypes = [
		wintypes.HANDLE,
		wintypes.DWORD,
		ctypes.POINTER(wintypes.HANDLE),
	]
	open_process_token.restype = wintypes.BOOL
	get_process = cast(Any, kernel32.GetCurrentProcess)
	get_token_information = cast(Any, advapi32.GetTokenInformation)
	get_token_information.argtypes = [
		wintypes.HANDLE,
		wintypes.DWORD,
		ctypes.c_void_p,
		wintypes.DWORD,
		ctypes.POINTER(wintypes.DWORD),
	]
	get_token_information.restype = wintypes.BOOL
	convert_sid = cast(Any, advapi32.ConvertSidToStringSidW)
	convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
	convert_sid.restype = wintypes.BOOL
	close_handle = cast(Any, kernel32.CloseHandle)
	local_free = cast(Any, kernel32.LocalFree)
	token = wintypes.HANDLE()
	if not open_process_token(get_process(), _WINDOWS_TOKEN_QUERY, ctypes.byref(token)):
		raise _windows_error()
	try:
		required = wintypes.DWORD()
		get_token_information(
			token, _WINDOWS_TOKEN_USER_INFORMATION, None, 0, ctypes.byref(required)
		)
		buffer = (ctypes.c_byte * required.value)()
		if not get_token_information(
			token,
			_WINDOWS_TOKEN_USER_INFORMATION,
			ctypes.byref(buffer),
			required.value,
			ctypes.byref(required),
		):
			raise _windows_error()
		user = ctypes.cast(ctypes.byref(buffer), ctypes.POINTER(TokenUser)).contents
		sid_text = wintypes.LPWSTR()
		if not convert_sid(user.user.sid, ctypes.byref(sid_text)):
			raise _windows_error()
		try:
			return str(sid_text.value)
		finally:
			local_free(ctypes.cast(sid_text, ctypes.c_void_p))
	finally:
		close_handle(token)


def _windows_directory_acl_is_private(path: Path) -> bool:
	from ctypes import wintypes

	kernel32, advapi32 = _windows_security_api()
	get_named_security_info = cast(Any, advapi32.GetNamedSecurityInfoW)
	get_descriptor_control = cast(Any, advapi32.GetSecurityDescriptorControl)
	get_descriptor_dacl = cast(Any, advapi32.GetSecurityDescriptorDacl)
	local_free = cast(Any, kernel32.LocalFree)
	descriptor = ctypes.c_void_p()
	dacl = ctypes.c_void_p()
	status = get_named_security_info(
		ctypes.c_wchar_p(str(path)),
		1,
		_WINDOWS_DACL_SECURITY_INFORMATION,
		None,
		None,
		ctypes.byref(dacl),
		None,
		ctypes.byref(descriptor),
	)
	if status != 0:
		return False
	try:
		control = wintypes.WORD()
		revision = wintypes.DWORD()
		present = wintypes.BOOL()
		defaulted = wintypes.BOOL()
		checked_dacl = ctypes.c_void_p()
		if not get_descriptor_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
			return False
		if not (control.value & _WINDOWS_DACL_PROTECTED) or not get_descriptor_dacl(
			descriptor,
			ctypes.byref(present),
			ctypes.byref(checked_dacl),
			ctypes.byref(defaulted),
		):
			return False
		if not present.value or checked_dacl.value is None:
			return False
		return _windows_dacl_is_private(checked_dacl)
	finally:
		local_free(descriptor)


def _windows_dacl_is_private(dacl: ctypes.c_void_p) -> bool:
	from ctypes import wintypes

	class AclSizeInformation(ctypes.Structure):
		_fields_ = [
			("ace_count", wintypes.DWORD),
			("acl_bytes_in_use", wintypes.DWORD),
			("acl_bytes_free", wintypes.DWORD),
		]

	class AceHeader(ctypes.Structure):
		_fields_ = [
			("ace_type", ctypes.c_ubyte),
			("ace_flags", ctypes.c_ubyte),
			("ace_size", wintypes.WORD),
		]

	kernel32, advapi32 = _windows_security_api()
	get_acl_information = cast(Any, advapi32.GetAclInformation)
	get_ace = cast(Any, advapi32.GetAce)
	convert_sid = cast(Any, advapi32.ConvertSidToStringSidW)
	local_free = cast(Any, kernel32.LocalFree)
	acl_info = AclSizeInformation()
	if not get_acl_information(
		dacl, ctypes.byref(acl_info), ctypes.sizeof(acl_info), _WINDOWS_ACL_SIZE_INFORMATION
	):
		return False
	if acl_info.ace_count != _WINDOWS_ACE_COUNT:
		return False
	aces: set[tuple[str, int, int]] = set()
	for index in range(acl_info.ace_count):
		ace = ctypes.c_void_p()
		if not get_ace(dacl, index, ctypes.byref(ace)):
			return False
		header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
		if header.ace_type != _WINDOWS_ACE_ALLOW or header.ace_flags != _WINDOWS_ACE_INHERIT_FLAGS:
			return False
		ace_address = ace.value
		if ace_address is None:
			return False
		mask = ctypes.c_uint32.from_address(ace_address + ctypes.sizeof(AceHeader)).value
		sid = ctypes.c_void_p(
			ace_address + ctypes.sizeof(AceHeader) + ctypes.sizeof(wintypes.DWORD)
		)
		sid_text = wintypes.LPWSTR()
		if not convert_sid(sid, ctypes.byref(sid_text)):
			return False
		try:
			aces.add((str(sid_text.value), int(mask), int(header.ace_flags)))
		finally:
			local_free(ctypes.cast(sid_text, ctypes.c_void_p))
	return aces == {
		(_windows_user_sid(), _WINDOWS_ALL_ACCESS, _WINDOWS_ACE_INHERIT_FLAGS),
		(_WINDOWS_LOCAL_SYSTEM_SID, _WINDOWS_ALL_ACCESS, _WINDOWS_ACE_INHERIT_FLAGS),
	}


def _ensure_private_directory(path: Path, *, restrict_existing: bool = False) -> os.stat_result:
	path.parent.mkdir(parents=True, exist_ok=True)
	if os.name == "nt":
		return _ensure_windows_private_directory(path)
	return _ensure_posix_private_directory(path, restrict_existing=restrict_existing)


def _ensure_windows_private_directory(path: Path) -> os.stat_result:
	from ctypes import wintypes

	class SecurityAttributes(ctypes.Structure):
		_fields_ = [
			("length", wintypes.DWORD),
			("security_descriptor", ctypes.c_void_p),
			("inherit_handle", wintypes.BOOL),
		]

	kernel32, advapi32 = _windows_security_api()
	convert_sddl = cast(Any, advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW)
	create_directory = cast(Any, kernel32.CreateDirectoryW)
	local_free = cast(Any, kernel32.LocalFree)
	sddl = f"D:P(A;OICI;FA;;;{_windows_user_sid()})(A;OICI;FA;;;SY)"
	descriptor = ctypes.c_void_p()
	if not convert_sddl(ctypes.c_wchar_p(sddl), 1, ctypes.byref(descriptor), None):
		raise _windows_error()
	try:
		attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
		if not create_directory(ctypes.c_wchar_p(str(path)), ctypes.byref(attributes)):
			last_error_name = "get_last_error"
			get_last_error = cast(Any, getattr(ctypes, last_error_name))
			error = get_last_error()
			if error != _WINDOWS_ERROR_ALREADY_EXISTS:
				raise _windows_error(error)
	finally:
		local_free(descriptor)
	metadata = path.lstat()
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		raise OSError(f"private temporary directory is unsafe: {path}")
	if not _windows_directory_acl_is_private(path):
		raise OSError(f"private temporary directory ACL is unsafe: {path}")
	return metadata


def _ensure_posix_private_directory(path: Path, *, restrict_existing: bool) -> os.stat_result:
	try:
		path.mkdir(mode=0o700)
	except FileExistsError:
		pass
	metadata = path.lstat()
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		raise OSError(f"private temporary directory is unsafe: {path}")
	if metadata.st_uid != _current_user_id():
		raise OSError(f"private temporary directory has a foreign owner: {path}")
	if stat.S_IMODE(metadata.st_mode) & 0o077 and restrict_existing:
		os.chmod(path, 0o700)
		metadata = path.lstat()
	if stat.S_IMODE(metadata.st_mode) & 0o077:
		raise OSError(f"private temporary directory permissions are unsafe: {path}")
	return metadata


def _ownership_key(*, create: bool) -> bytes | None:
	path = _user_state_directory() / _OWNERSHIP_KEY_NAME
	if create:
		_ensure_private_directory(path.parent, restrict_existing=True)
	try:
		key = _read_ownership_key(path)
	except OSError:
		if not _incomplete_ownership_key(path):
			raise
		path = path.with_name(_RECOVERY_KEY_NAME)
		key = _read_ownership_key(path)
	if key is None and path.name == _OWNERSHIP_KEY_NAME:
		recovery = path.with_name(_RECOVERY_KEY_NAME)
		recovery_key = _read_ownership_key(recovery)
		if recovery_key is not None:
			return recovery_key
	if key is not None or not create:
		return key
	_create_ownership_key_file(path)
	return _read_ownership_key(path)


def _incomplete_ownership_key(path: Path) -> bool:
	try:
		metadata = path.lstat()
	except OSError:
		return False
	return (
		_plain_lock_file(path, metadata)
		and metadata.st_size < _OWNERSHIP_KEY_BYTES
		and (os.name == "nt" or metadata.st_uid == _current_user_id())
		and (os.name == "nt" or stat.S_IMODE(metadata.st_mode) == _OWNERSHIP_KEY_MODE)
	)


def _remove_published_temporary(parent: int, name: str, identity: os.stat_result) -> None:
	try:
		current = os.stat(name, dir_fd=parent, follow_symlinks=False)
	except FileNotFoundError:
		return
	if _same_file_identity(identity, current):
		os.unlink(name, dir_fd=parent)


def _publish_hard_link(
	parent: int, temporary_name: str, target_name: str, identity: os.stat_result
) -> None:
	os.link(temporary_name, target_name, src_dir_fd=parent, dst_dir_fd=parent)
	_remove_published_temporary(parent, temporary_name, identity)


def _publish_new_file(path: Path, content: bytes) -> None:
	"""Publish complete bytes without replacing an existing name."""
	descriptor, temporary_name = tempfile.mkstemp(prefix=".quality-gate-pending-", dir=path.parent)
	temporary = Path(temporary_name)
	identity = os.fstat(descriptor)
	try:
		with os.fdopen(descriptor, "wb") as stream:
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		if os.name == "nt":
			os.rename(temporary, path)
		else:
			flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
			parent = os.open(path.parent, flags)
			try:
				if not _same_file_identity(os.fstat(parent), path.parent.lstat()):
					raise OSError(f"temporary publication directory changed: {path.parent}")
				use_hard_link = False
				try:
					_rename_without_replacement(temporary.name, path.name, parent, parent)
				except AttributeError:
					use_hard_link = True
				except OSError as error:
					if error.errno not in (None, errno.ENOSYS, errno.EINVAL):
						raise
					use_hard_link = True
				if use_hard_link:
					_publish_hard_link(parent, temporary.name, path.name, identity)
				os.fsync(parent)
			finally:
				os.close(parent)
	finally:
		try:
			current = temporary.lstat()
			if _same_file_identity(identity, current):
				temporary.unlink()
		except FileNotFoundError:
			pass


def _create_ownership_key_file(path: Path) -> None:
	try:
		_publish_new_file(path, os.urandom(_OWNERSHIP_KEY_BYTES))
	except FileExistsError:
		pass


def _private_state_directory_fd(path: Path) -> int | None:
	flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
	try:
		parent = os.open(path.parent, flags)
	except OSError:
		return None
	try:
		identity = os.fstat(parent)
		path_identity = path.parent.lstat()
	except OSError:
		os.close(parent)
		return None
	if (
		not stat.S_ISDIR(identity.st_mode)
		or not _same_file_identity(identity, path_identity)
		or identity.st_uid != _current_user_id()
		or stat.S_IMODE(identity.st_mode) & 0o077
		or (stat.S_IMODE(identity.st_mode) & _PRIVATE_DIRECTORY_OWNER_ACCESS)
		!= _PRIVATE_DIRECTORY_OWNER_ACCESS
	):
		os.close(parent)
		return None
	return parent


def _matching_key_staging_name(parent: int, target: os.stat_result) -> str | None:
	matches: list[str] = []
	for name in os.listdir(parent):
		if not name.startswith(".quality-gate-pending-"):
			continue
		try:
			candidate = os.stat(name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			continue
		if (
			_same_file_identity(target, candidate)
			and stat.S_ISREG(candidate.st_mode)
			and candidate.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
			and candidate.st_uid == _current_user_id()
			and stat.S_IMODE(candidate.st_mode) == _OWNERSHIP_KEY_MODE
		):
			matches.append(name)
	return matches[0] if len(matches) == 1 else None


def _remove_key_staging_link(parent: int, path: Path, expected: os.stat_result, name: str) -> None:
	try:
		descriptor = os.open(
			name,
			os.O_RDONLY | _optional_os_flag("O_NOFOLLOW") | _optional_os_flag("O_NONBLOCK"),
			dir_fd=parent,
		)
	except FileNotFoundError:
		return
	try:
		staging_identity = os.fstat(descriptor)
		try:
			current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
			staging = os.stat(name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			return
		if (
			_same_file_identity(expected, current)
			and _same_file_identity(current, staging_identity)
			and _same_file_identity(staging_identity, staging)
			and current.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
			and staging.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
			and stat.S_ISREG(staging_identity.st_mode)
		):
			os.unlink(name, dir_fd=parent)
			os.fsync(parent)
	finally:
		os.close(descriptor)


def _recover_interrupted_key_publication(
	path: Path, metadata: os.stat_result
) -> os.stat_result | None:
	"""Remove only a matching staging link left by a stopped key publisher."""
	if (
		os.name == "nt"
		or path.name not in {_OWNERSHIP_KEY_NAME, _RECOVERY_KEY_NAME}
		or metadata.st_nlink != _PUBLICATION_TRANSITION_LINK_COUNT
		or not stat.S_ISREG(metadata.st_mode)
		or metadata.st_uid != _current_user_id()
		or stat.S_IMODE(metadata.st_mode) != _OWNERSHIP_KEY_MODE
	):
		return None
	parent = _private_state_directory_fd(path)
	if parent is None:
		return None
	try:
		try:
			current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			return None
		if (
			not _same_file_identity(metadata, current)
			or current.st_nlink != _PUBLICATION_TRANSITION_LINK_COUNT
			or not stat.S_ISREG(current.st_mode)
			or current.st_uid != _current_user_id()
			or stat.S_IMODE(current.st_mode) != _OWNERSHIP_KEY_MODE
		):
			return None
		staging_name = _matching_key_staging_name(parent, current)
		if staging_name is None:
			return None
		_remove_key_staging_link(parent, path, current, staging_name)
		try:
			current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			return None
		if (
			_same_file_identity(metadata, current)
			and current.st_nlink == 1
			and stat.S_ISREG(current.st_mode)
			and current.st_uid == _current_user_id()
			and stat.S_IMODE(current.st_mode) == _OWNERSHIP_KEY_MODE
		):
			return current
		return None
	finally:
		os.close(parent)


def _read_ownership_key(path: Path) -> bytes | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return None
	if metadata.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT:
		recovered = _recover_interrupted_key_publication(path, metadata)
		if recovered is not None:
			metadata = recovered
	if not _plain_lock_file(path, metadata) or metadata.st_size != _OWNERSHIP_KEY_BYTES:
		raise OSError(f"temporary workspace ownership key is unsafe: {path}")
	metadata = _check_ownership_key_permissions(path, metadata)
	try:
		with path.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
				return None
			key = stream.read(_OWNERSHIP_KEY_BYTES + 1)
		current = path.lstat()
	except OSError:
		return None
	if (
		len(key) != _OWNERSHIP_KEY_BYTES
		or not _same_file_identity(metadata, current)
		or not _plain_lock_file(path, current)
	):
		return None
	return key


def _check_ownership_key_permissions(path: Path, metadata: os.stat_result) -> os.stat_result:
	if os.name != "nt":
		if metadata.st_uid != _current_user_id():
			raise OSError(f"temporary workspace ownership key has a foreign owner: {path}")
		if stat.S_IMODE(metadata.st_mode) != _OWNERSHIP_KEY_MODE:
			os.chmod(path, _OWNERSHIP_KEY_MODE)
			metadata = path.lstat()
			if stat.S_IMODE(metadata.st_mode) != _OWNERSHIP_KEY_MODE:
				raise OSError(f"temporary workspace ownership key permissions are unsafe: {path}")
	return metadata


def _signed_record(payload: dict[str, object], *, create_key: bool) -> bytes | None:
	key = _ownership_key(create=create_key)
	if key is None:
		return None
	signature = hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()
	return _canonical_json({**payload, "signature": signature})


def _valid_signed_record(content: bytes) -> dict[str, object] | None:
	try:
		record = json.loads(content)
	except (json.JSONDecodeError, UnicodeDecodeError):
		return None
	if not isinstance(record, dict):
		return None
	signature = record.get("signature")
	if (
		not isinstance(signature, str)
		or len(signature) != _SIGNATURE_HEX_LENGTH
		or any(character not in "0123456789abcdef" for character in signature)
	):
		return None
	if content != _canonical_json(record):
		return None
	payload = {key: value for key, value in record.items() if key != "signature"}
	key = _ownership_key(create=False)
	keys = [key] if key is not None else []
	try:
		recovery_key = _read_ownership_key(_user_state_directory() / _RECOVERY_KEY_NAME)
	except OSError:
		recovery_key = None
	if recovery_key is not None and recovery_key not in keys:
		keys.append(recovery_key)
	for candidate in keys:
		expected = hmac.new(candidate, _canonical_json(payload), hashlib.sha256).hexdigest()
		if hmac.compare_digest(signature, expected):
			return payload
	return None


def _plain_lock_file(path: Path, metadata: os.stat_result) -> bool:
	if metadata.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT:
		for _ in range(_PUBLICATION_LINK_SETTLE_ATTEMPTS):
			time.sleep(_PUBLICATION_LINK_SETTLE_DELAY_SECONDS)
			try:
				current = path.lstat()
			except OSError:
				return False
			if not _same_file_identity(metadata, current):
				return False
			if current.st_nlink != _PUBLICATION_TRANSITION_LINK_COUNT:
				metadata = current
				break
		else:
			return False
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
	deferred_root_controls: list[tuple[Path, os.stat_result]] = []
	pending = [(path, identity)]
	while pending:
		directory, expected = pending.pop()
		entries = _directory_entries_for_removal(directory, expected)
		if entries is None:
			continue
		directories.append((directory, expected))
		for child_path, scanned_identity in entries:
			_remove_scanned_child(
				directory,
				expected,
				child_path,
				scanned_identity,
				pending,
				deferred_root_controls=(deferred_root_controls if directory == path else None),
			)
	for directory, expected in reversed(directories[1:]):
		_remove_collected_directory(directory, expected)
	for child_path, child_identity in deferred_root_controls:
		_remove_scanned_child(
			path,
			identity,
			child_path,
			child_identity,
			pending,
			root_control=True,
		)
	if directories:
		_remove_collected_directory(*directories[0])


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
	*,
	deferred_root_controls: list[tuple[Path, os.stat_result]] | None = None,
	root_control: bool = False,
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
	if root_control and not _plain_lock_file(path, metadata):
		raise OSError(f"temporary workspace control file changed during removal: {path}")
	if deferred_root_controls is not None and path.name in _ROOT_CONTROL_FILE_NAMES:
		if not _plain_lock_file(path, metadata):
			raise OSError(f"temporary workspace control file changed during removal: {path}")
		deferred_root_controls.append((path, scanned_identity))
		return
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


def _temporary_root_for_entry(path: Path) -> Path:
	if path.name.startswith(
		(_OWNERSHIP_RECORD_PREFIX, _CLEANUP_RECORD_PREFIX, _CLEANUP_RETRY_PREFIX)
	):
		return path.parent
	workspace_directories = [
		candidate for candidate in (path, *path.parents) if candidate.name.startswith("qg-")
	]
	managed_workspace = next(
		(
			candidate
			for candidate in workspace_directories
			if candidate.parent.name == TEMP_DIRECTORY_NAME
		),
		None,
	)
	if managed_workspace is not None:
		return managed_workspace.parent
	if workspace_directories:
		return workspace_directories[-1].parent
	raise OSError(f"temporary workspace entry has no managed root: {path}")


def _cleanup_area_payload(temp_root: Path, identity: os.stat_result) -> dict[str, object]:
	return {
		"device": identity.st_dev,
		"format": "quality-gate-temp-workspace-cleanup-area",
		"inode": identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"schema": 1,
	}


def _cleanup_area_marker(temp_root: Path, identity: os.stat_result) -> bytes:
	marker = _signed_record(_cleanup_area_payload(temp_root, identity), create_key=True)
	if marker is None:
		raise OSError(f"temporary workspace signing key is unavailable: {temp_root}")
	return marker


def _open_cleanup_area(temp_root: Path) -> tuple[Path, int]:
	_check_ordinary_directory(temp_root)
	key = _ownership_key(create=True)
	if key is None:
		raise OSError(f"temporary workspace signing key is unavailable: {temp_root}")
	legacy = _open_cleanup_area_at(temp_root, temp_root / _CLEANUP_AREA_NAME, create=False)
	try:
		recovery_key = _read_ownership_key(_user_state_directory() / _RECOVERY_KEY_NAME)
	except OSError:
		recovery_key = None
	keys = [key]
	if recovery_key is not None and recovery_key not in keys:
		keys.append(recovery_key)
	selected = _open_existing_cleanup_areas(temp_root, keys)
	if selected is not None:
		if legacy is not None:
			os.close(legacy[1])
		return selected
	if legacy is not None:
		return legacy
	for area in _cleanup_area_candidates(temp_root, key):
		opened = _open_cleanup_area_at(temp_root, area, create=True)
		if opened is not None:
			return opened
	raise OSError(f"no safe temporary workspace cleanup area available: {temp_root}")


def _cleanup_area_candidates(temp_root: Path, key: bytes) -> Iterator[Path]:
	digest = hmac.new(key, b"quality-gate-cleanup-area", hashlib.sha256).hexdigest()[:32]
	for index in range(8):
		suffix = "" if index == 0 else f"-{index}"
		yield temp_root / f"{_CLEANUP_AREA_NAME}-{digest}{suffix}"


def _open_existing_cleanup_areas(temp_root: Path, keys: list[bytes]) -> tuple[Path, int] | None:
	selected: tuple[Path, int] | None = None
	for candidate_key in keys:
		for area in _cleanup_area_candidates(temp_root, candidate_key):
			opened = _open_cleanup_area_at(temp_root, area, create=False)
			if opened is None:
				continue
			if candidate_key == keys[0] and selected is None:
				selected = opened
			else:
				os.close(opened[1])
	return selected


def _open_cleanup_area_at(temp_root: Path, area: Path, *, create: bool) -> tuple[Path, int] | None:
	created = False
	if create:
		try:
			area.mkdir(mode=0o700)
			created = True
		except FileExistsError:
			pass
	try:
		identity = (
			_ensure_posix_private_directory(area, restrict_existing=False)
			if create
			else area.lstat()
		)
		if (
			is_link_or_reparse(identity)
			or not stat.S_ISDIR(identity.st_mode)
			or identity.st_uid != _current_user_id()
			or stat.S_IMODE(identity.st_mode) & 0o077
		):
			return None
	except OSError:
		return None
	marker = area / _CLEANUP_AREA_MARKER_NAME
	if created:
		_publish_new_file(marker, _cleanup_area_marker(temp_root, identity))
	if not _cleanup_area_marker_is_current(temp_root, marker, identity):
		return None
	flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
	descriptor = os.open(area, flags)
	opened_area = os.fstat(descriptor)
	current_area = area.lstat()
	if not _same_file_identity(identity, opened_area) or not _same_file_identity(
		identity, current_area
	):
		os.close(descriptor)
		raise OSError(f"temporary workspace cleanup area changed: {area}")
	os.fsync(descriptor)
	_recover_quarantined_entries(temp_root, descriptor)
	return area, descriptor


def _cleanup_area_marker_is_current(
	temp_root: Path, marker: Path, identity: os.stat_result
) -> bool:
	try:
		marker_identity = marker.lstat()
	except FileNotFoundError:
		return False
	if (
		not _plain_lock_file(marker, marker_identity)
		or marker_identity.st_size > _OWNERSHIP_RECORD_MAX_BYTES
	):
		return False
	try:
		with marker.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(marker_identity, opened) or not _plain_lock_file(
				marker, opened
			):
				return False
			content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
		current_marker = marker.lstat()
	except (FileNotFoundError, PermissionError):
		return False
	return _valid_signed_record(content) == _cleanup_area_payload(
		temp_root, identity
	) and _same_file_identity(marker_identity, current_marker)


def _recover_quarantined_entries(temp_root: Path, cleanup_descriptor: int) -> None:
	for name in os.listdir(cleanup_descriptor):
		if not name.startswith("pending-") or not name.endswith(".json"):
			continue
		_recover_quarantined_entry(temp_root, name, cleanup_descriptor)


def _recover_quarantined_entry(temp_root: Path, journal_name: str, cleanup_descriptor: int) -> None:
	journal_identity, payload = _read_quarantine_journal(cleanup_descriptor, journal_name)
	if payload is None or payload["root"] != os.path.normcase(os.path.abspath(temp_root)):
		return
	quarantine_name = payload["quarantine"]
	if not isinstance(quarantine_name, str):
		return
	try:
		metadata = os.stat(quarantine_name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	except FileNotFoundError:
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		return
	expected_identity = (cast(int, payload["device"]), cast(int, payload["inode"]))
	if (metadata.st_dev, metadata.st_ino) != expected_identity:
		_remove_restored_quarantine_journal(
			temp_root, payload, journal_name, journal_identity, quarantine_name, cleanup_descriptor
		)
		return
	if not _quarantined_kind_matches(metadata, str(payload["kind"])):
		return
	try:
		_delete_quarantined_entry(quarantine_name, str(payload["kind"]), cleanup_descriptor)
	except OSError:
		_remove_restored_quarantine_journal(
			temp_root, payload, journal_name, journal_identity, quarantine_name, cleanup_descriptor
		)
		return
	os.fsync(cleanup_descriptor)
	_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)


def _delete_quarantined_entry(name: str, kind: str, cleanup_descriptor: int) -> None:
	if kind == "directory":
		os.rmdir(name, dir_fd=cleanup_descriptor)
	else:
		os.unlink(name, dir_fd=cleanup_descriptor)


def _remove_restored_quarantine_journal(
	temp_root: Path,
	payload: dict[str, object],
	journal_name: str,
	journal_identity: os.stat_result | None,
	quarantine_name: str,
	cleanup_descriptor: int,
) -> None:
	if _restore_pending_quarantined_entry(temp_root, payload, quarantine_name, cleanup_descriptor):
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)


def _restore_pending_quarantined_entry(
	temp_root: Path,
	payload: dict[str, object],
	quarantine_name: str,
	cleanup_descriptor: int,
) -> bool:
	try:
		parent_relative = Path(str(payload["parent"]))
		parent_path = temp_root / parent_relative
		parent_identity = (
			cast(int, payload["parent_device"]),
			cast(int, payload["parent_inode"]),
		)
		parent = os.open(
			parent_path,
			os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW"),
		)
	except (OSError, TypeError, ValueError):
		return False
	try:
		opened_parent = os.fstat(parent)
		if (opened_parent.st_dev, opened_parent.st_ino) != parent_identity:
			return False
		_restore_quarantined_entry(
			Path(str(payload["name"])),
			quarantine_name,
			parent,
			cleanup_descriptor,
		)
		os.fsync(parent)
		os.fsync(cleanup_descriptor)
		return True
	except OSError:
		return False
	finally:
		os.close(parent)


def _remove_quarantine_journal(
	name: str, expected_identity: os.stat_result | None, cleanup_descriptor: int
) -> None:
	if expected_identity is None:
		return
	try:
		current = os.stat(name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	except FileNotFoundError:
		return
	if _same_file_identity(expected_identity, current):
		os.unlink(name, dir_fd=cleanup_descriptor)
		os.fsync(cleanup_descriptor)


def _write_quarantine_journal(
	temp_root: Path,
	path: Path,
	expected_identity: os.stat_result,
	parent_identity: os.stat_result,
	kind: str,
	token: str,
	quarantine_name: str,
	cleanup_descriptor: int,
) -> tuple[str, os.stat_result]:
	journal_name = f"pending-{token}.json"
	parent_relative = path.parent.relative_to(temp_root).as_posix()
	payload: dict[str, object] = {
		"device": expected_identity.st_dev,
		"format": "quality-gate-temp-workspace-quarantine",
		"inode": expected_identity.st_ino,
		"kind": kind,
		"name": path.name,
		"parent": parent_relative,
		"parent_device": parent_identity.st_dev,
		"parent_inode": parent_identity.st_ino,
		"quarantine": quarantine_name,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"schema": 1,
		"token": token,
	}
	content = _signed_record(payload, create_key=False)
	if content is None:
		raise OSError(f"temporary workspace quarantine key is unavailable: {path}")
	flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _optional_os_flag("O_NOFOLLOW")
	descriptor = os.open(journal_name, flags, 0o600, dir_fd=cleanup_descriptor)
	created_identity = os.fstat(descriptor)
	try:
		with os.fdopen(descriptor, "wb") as stream:
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		os.fsync(cleanup_descriptor)
		return journal_name, created_identity
	except OSError:
		_remove_quarantine_journal(journal_name, created_identity, cleanup_descriptor)
		raise


def _quarantined_kind_matches(metadata: os.stat_result, kind: str) -> bool:
	if kind == "link":
		return is_link_or_reparse(metadata)
	if kind == "directory":
		return stat.S_ISDIR(metadata.st_mode) and not is_link_or_reparse(metadata)
	return (
		kind == "file" and not stat.S_ISDIR(metadata.st_mode) and not is_link_or_reparse(metadata)
	)


def _read_quarantine_journal(
	cleanup_descriptor: int, name: str
) -> tuple[os.stat_result | None, dict[str, object] | None]:
	try:
		metadata = os.stat(name, dir_fd=cleanup_descriptor, follow_symlinks=False)
		if (
			not stat.S_ISREG(metadata.st_mode)
			or metadata.st_nlink != 1
			or metadata.st_size > _QUARANTINE_JOURNAL_MAX_BYTES
		):
			return metadata, None
		descriptor = os.open(
			name,
			os.O_RDONLY | _optional_os_flag("O_NOFOLLOW"),
			dir_fd=cleanup_descriptor,
		)
		with os.fdopen(descriptor, "rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened):
				return metadata, None
			content = stream.read(_QUARANTINE_JOURNAL_MAX_BYTES + 1)
			current = os.stat(name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	except OSError:
		return None, None
	if len(content) > _QUARANTINE_JOURNAL_MAX_BYTES or not _same_file_identity(metadata, current):
		return metadata, None
	payload = _valid_signed_record(content)
	if payload is None or set(payload) != {
		"device",
		"format",
		"inode",
		"kind",
		"name",
		"parent",
		"parent_device",
		"parent_inode",
		"quarantine",
		"root",
		"schema",
		"token",
	}:
		return metadata, None
	token = name.removeprefix("pending-").removesuffix(".json")
	if (
		payload["format"] != "quality-gate-temp-workspace-quarantine"
		or payload["schema"] != _QUARANTINE_JOURNAL_SCHEMA
		or payload["token"] != token
		or type(payload["device"]) is not int
		or cast(int, payload["device"]) < 0
		or type(payload["inode"]) is not int
		or cast(int, payload["inode"]) < 0
		or type(payload["parent_device"]) is not int
		or cast(int, payload["parent_device"]) < 0
		or type(payload["parent_inode"]) is not int
		or cast(int, payload["parent_inode"]) < 0
		or type(payload["schema"]) is not int
		or cast(int, payload["schema"]) != _QUARANTINE_JOURNAL_SCHEMA
		or not isinstance(payload["kind"], str)
		or payload["kind"] not in {"file", "link", "directory"}
		or not isinstance(payload["name"], str)
		or not payload["name"]
		or Path(payload["name"]).name != payload["name"]
		or not isinstance(payload["parent"], str)
		or Path(payload["parent"]).is_absolute()
		or ".." in Path(payload["parent"]).parts
		or not isinstance(payload["root"], str)
		or not isinstance(payload["quarantine"], str)
	):
		return metadata, None
	expected_quarantine_name = (
		f"entry-{cast(int, payload['device']):x}-{cast(int, payload['inode']):x}-{token}"
	)
	if payload["quarantine"] != expected_quarantine_name:
		return metadata, None
	return metadata, payload


def _rename_entry_to_cleanup_area(
	path: Path,
	quarantine_name: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	try:
		os.rename(
			path.name,
			quarantine_name,
			src_dir_fd=parent_descriptor,
			dst_dir_fd=cleanup_descriptor,
		)
	except PermissionError:
		parent_mode = os.fstat(parent_descriptor).st_mode
		fchmod_name = "fchmod"
		fchmod = cast(Any, getattr(os, fchmod_name))
		fchmod(parent_descriptor, stat.S_IMODE(parent_mode) | stat.S_IWUSR | stat.S_IXUSR)
		os.rename(
			path.name,
			quarantine_name,
			src_dir_fd=parent_descriptor,
			dst_dir_fd=cleanup_descriptor,
		)


def _rename_without_replacement(
	source_name: str,
	destination_name: str,
	source_descriptor: int,
	destination_descriptor: int,
) -> None:
	result: int
	if sys.platform.startswith("linux"):
		libc = cast(Any, ctypes.CDLL(None, use_errno=True))
		renameat2 = cast(Any, libc.renameat2)
		result = renameat2(
			source_descriptor,
			os.fsencode(source_name),
			destination_descriptor,
			os.fsencode(destination_name),
			1,
		)
	elif sys.platform == "darwin":
		libc = cast(Any, ctypes.CDLL(None, use_errno=True))
		renameatx = cast(Any, libc.renameatx_np)
		result = renameatx(
			source_descriptor,
			os.fsencode(source_name),
			destination_descriptor,
			os.fsencode(destination_name),
			0x00000004,
		)
	else:
		raise OSError("safe recovery of a replaced temporary entry is unavailable on this platform")
	if result != 0:
		error = ctypes.get_errno()
		raise OSError(error, os.strerror(error), destination_name)


def _restore_quarantined_entry(
	path: Path,
	quarantine_name: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	_rename_without_replacement(quarantine_name, path.name, cleanup_descriptor, parent_descriptor)


def _remove_by_quarantine(
	path: Path,
	expected_identity: os.stat_result,
	*,
	kind: str,
) -> None:
	if os.name == "nt":
		_remove_by_windows_handle(path, expected_identity, kind=kind)
		return
	_remove_by_posix_quarantine(path, expected_identity, kind=kind)


def _remove_by_posix_quarantine(
	path: Path,
	expected_identity: os.stat_result,
	*,
	kind: str,
) -> None:
	root = _temporary_root_for_entry(path)
	parent_identity = path.parent.lstat()
	if is_link_or_reparse(parent_identity) or not stat.S_ISDIR(parent_identity.st_mode):
		raise OSError(f"temporary workspace parent changed before removal: {path.parent}")
	flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
	parent_descriptor = os.open(path.parent, flags)
	try:
		if not _same_file_identity(parent_identity, os.fstat(parent_descriptor)):
			raise OSError(f"temporary workspace parent changed before removal: {path.parent}")
		current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
		if not _same_file_identity(expected_identity, current):
			raise OSError(f"temporary workspace identity changed before removal: {path}")
		_area, cleanup_descriptor = _open_cleanup_area(root)
		try:
			_remove_quarantined_entry(
				root,
				path,
				expected_identity,
				parent_identity,
				kind,
				parent_descriptor,
				cleanup_descriptor,
			)
		finally:
			os.close(cleanup_descriptor)
	finally:
		os.close(parent_descriptor)


def _remove_quarantined_entry(
	temp_root: Path,
	path: Path,
	expected_identity: os.stat_result,
	parent_identity: os.stat_result,
	kind: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	token = os.urandom(16).hex()
	quarantine_name = f"entry-{expected_identity.st_dev:x}-{expected_identity.st_ino:x}-{token}"
	journal_name, journal_identity = _write_quarantine_journal(
		temp_root,
		path,
		expected_identity,
		parent_identity,
		kind,
		token,
		quarantine_name,
		cleanup_descriptor,
	)
	try:
		_rename_entry_to_cleanup_area(path, quarantine_name, parent_descriptor, cleanup_descriptor)
	except OSError:
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		raise
	moved = os.stat(quarantine_name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	if not _same_file_identity(expected_identity, moved) or not _quarantined_kind_matches(
		moved, kind
	):
		_restore_quarantined_entry(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		os.fsync(parent_descriptor)
		os.fsync(cleanup_descriptor)
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		raise OSError(f"temporary workspace entry changed during removal: {path}")
	try:
		_delete_quarantined_entry(quarantine_name, kind, cleanup_descriptor)
		os.fsync(cleanup_descriptor)
	except OSError:
		_restore_failed_quarantine(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		raise
	_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)


def _restore_failed_quarantine(
	path: Path,
	quarantine_name: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	try:
		_restore_quarantined_entry(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		os.fsync(parent_descriptor)
		os.fsync(cleanup_descriptor)
	except OSError as restore_error:
		raise OSError(
			f"temporary workspace entry could not be restored from cleanup area: {path}"
		) from restore_error


def _open_windows_removal_handle(path: Path, kernel32: object) -> int:
	from ctypes import wintypes

	api = cast(Any, kernel32)
	handle = api.CreateFileW(
		ctypes.c_wchar_p(str(path)),
		_WINDOWS_DELETE_ACCESS | _WINDOWS_READ_ATTRIBUTES_ACCESS | _WINDOWS_WRITE_ATTRIBUTES_ACCESS,
		_WINDOWS_SHARE_READ | _WINDOWS_SHARE_WRITE | _WINDOWS_SHARE_DELETE,
		None,
		_WINDOWS_OPEN_EXISTING,
		_WINDOWS_OPEN_REPARSE_POINT | _WINDOWS_BACKUP_SEMANTICS,
		None,
	)
	if handle == wintypes.HANDLE(-1).value:
		raise _windows_error()
	return cast(int, handle)


def _windows_file_identity(
	handle: int, kernel32: object
) -> tuple[tuple[int, int], tuple[int, int] | None, int]:
	from ctypes import wintypes

	class ByHandleFileInformation(ctypes.Structure):
		_fields_ = [
			("attributes", wintypes.DWORD),
			("creation_time_low", wintypes.DWORD),
			("creation_time_high", wintypes.DWORD),
			("last_access_low", wintypes.DWORD),
			("last_access_high", wintypes.DWORD),
			("last_write_low", wintypes.DWORD),
			("last_write_high", wintypes.DWORD),
			("volume_serial", wintypes.DWORD),
			("size_high", wintypes.DWORD),
			("size_low", wintypes.DWORD),
			("links", wintypes.DWORD),
			("index_high", wintypes.DWORD),
			("index_low", wintypes.DWORD),
		]

	class FileIdentifier(ctypes.Structure):
		_fields_ = [("low", ctypes.c_uint64), ("high", ctypes.c_uint64)]

	class FileIdInformation(ctypes.Structure):
		_fields_ = [
			("volume_serial", ctypes.c_uint64),
			("file_id", FileIdentifier),
		]

	information = ByHandleFileInformation()
	api = cast(Any, kernel32)
	get_file_information = api.GetFileInformationByHandle
	if not get_file_information(handle, ctypes.byref(information)):
		raise _windows_error()
	legacy_identity = (
		int(information.volume_serial),
		(int(information.index_high) << 32) | int(information.index_low),
	)
	file_id_information = FileIdInformation()
	get_file_information_ex = api.GetFileInformationByHandleEx
	modern_identity: tuple[int, int] | None = None
	if get_file_information_ex(
		handle,
		_WINDOWS_FILE_ID_INFORMATION,
		ctypes.byref(file_id_information),
		ctypes.sizeof(file_id_information),
	):
		modern_file_id = int(file_id_information.file_id.low) | (
			int(file_id_information.file_id.high) << 64
		)
		modern_identity = (int(file_id_information.volume_serial), modern_file_id)
	return legacy_identity, modern_identity, int(information.attributes)


def _validate_windows_removal_identity(
	path: Path,
	expected_identity: os.stat_result,
	kind: str,
	legacy_identity: tuple[int, int],
	modern_identity: tuple[int, int] | None,
	attributes: int,
) -> None:
	expected_file_identity = (expected_identity.st_dev, expected_identity.st_ino)
	if expected_file_identity not in {legacy_identity, modern_identity}:
		raise OSError(
			f"temporary workspace identity changed before removal: {path}; "
			f"expected {expected_file_identity}, opened {legacy_identity} / {modern_identity}"
		)
	reparse = bool(attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)
	is_directory = bool(attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
	expected_type = (
		(kind == "link" and reparse)
		or (kind == "directory" and is_directory)
		or (kind == "file" and not reparse and not is_directory)
	)
	if not expected_type:
		raise OSError(f"temporary workspace entry type changed before removal: {path}")


def _clear_windows_readonly_attribute(handle: int, attributes: int, kernel32: object) -> None:
	from ctypes import wintypes

	class FileBasicInformation(ctypes.Structure):
		_fields_ = [
			("creation_time", ctypes.c_longlong),
			("last_access_time", ctypes.c_longlong),
			("last_write_time", ctypes.c_longlong),
			("change_time", ctypes.c_longlong),
			("attributes", wintypes.DWORD),
		]

	if not attributes & _WINDOWS_FILE_ATTRIBUTE_READONLY:
		return
	api = cast(Any, kernel32)
	get_file_information = api.GetFileInformationByHandleEx
	set_file_information = api.SetFileInformationByHandle
	basic = FileBasicInformation()
	if not get_file_information(
		handle,
		_WINDOWS_FILE_BASIC_INFORMATION,
		ctypes.byref(basic),
		ctypes.sizeof(basic),
	):
		raise _windows_error()
	basic.attributes &= ~_WINDOWS_FILE_ATTRIBUTE_READONLY
	if basic.attributes == 0:
		basic.attributes = _WINDOWS_FILE_ATTRIBUTE_NORMAL
	if not set_file_information(
		handle,
		_WINDOWS_FILE_BASIC_INFORMATION,
		ctypes.byref(basic),
		ctypes.sizeof(basic),
	):
		raise _windows_error()


def _mark_windows_handle_for_deletion(handle: int, kernel32: object) -> None:
	from ctypes import wintypes

	class FileDispositionInformation(ctypes.Structure):
		_fields_ = [("delete_file", wintypes.BOOLEAN)]

	disposition = FileDispositionInformation(True)
	set_file_information = cast(Any, kernel32).SetFileInformationByHandle
	if not set_file_information(
		handle,
		_WINDOWS_FILE_DISPOSITION_INFORMATION,
		ctypes.byref(disposition),
		ctypes.sizeof(disposition),
	):
		raise _windows_error()


def _remove_by_windows_handle(path: Path, expected_identity: os.stat_result, *, kind: str) -> None:
	kernel32, _advapi32 = _windows_security_api()
	handle = _open_windows_removal_handle(path, kernel32)
	try:
		legacy_identity, modern_identity, attributes = _windows_file_identity(handle, kernel32)
		_validate_windows_removal_identity(
			path, expected_identity, kind, legacy_identity, modern_identity, attributes
		)
		_clear_windows_readonly_attribute(handle, attributes, kernel32)
		_mark_windows_handle_for_deletion(handle, kernel32)
	finally:
		cast(Any, kernel32).CloseHandle(handle)


def _remove_link(path: Path, metadata: os.stat_result) -> None:
	_remove_by_quarantine(path, metadata, kind="link")


def _remove_file(path: Path, expected_identity: os.stat_result) -> None:
	_remove_by_quarantine(path, expected_identity, kind="file")


def _remove_directory(path: Path, *, expected_identity: os.stat_result) -> None:
	_remove_by_quarantine(path, expected_identity, kind="directory")


def _clean_stale_entries(temp_root: Path) -> None:
	seen: set[str] = set()
	for owner_record in tuple(temp_root.iterdir()):
		entry = _run_directory_from_owner_record(temp_root, owner_record)
		if entry is not None and entry.name not in seen:
			seen.add(entry.name)
			_reclaim_stale_workspace(temp_root, entry)


def _run_directory_from_owner_record(temp_root: Path, owner_record: Path) -> Path | None:
	prefix = next(
		(
			candidate
			for candidate in (
				_OWNERSHIP_RECORD_PREFIX,
				_CLEANUP_RECORD_PREFIX,
				_CLEANUP_RETRY_PREFIX,
			)
			if owner_record.name.startswith(candidate)
		),
		None,
	)
	if prefix is None:
		return None
	run_name = owner_record.name.removeprefix(prefix)
	if not run_name.startswith("qg-") or Path(run_name).name != run_name:
		return None
	return temp_root / run_name


def _reclaim_stale_workspace(temp_root: Path, entry: Path) -> None:
	try:
		metadata = entry.lstat()
	except FileNotFoundError:
		_remove_orphan_ownership_record(temp_root, entry)
		return
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		return
	owner_valid, owner_identity = _has_valid_ownership_record(temp_root, entry, metadata)
	if not owner_valid or owner_identity is None:
		return
	cleanup_valid, cleanup_identity, active_identity, marker_identity = _has_valid_cleanup_record(
		temp_root, entry, metadata
	)
	if not cleanup_valid:
		cleanup_identity = None
	cleanup_controls = (
		(active_identity, marker_identity)
		if active_identity is not None and marker_identity is not None
		else None
	)
	can_reclaim, cleanup_identity, cleanup_controls = _can_reclaim_workspace(
		temp_root,
		entry,
		metadata,
		owner_identity,
		cleanup_identity=cleanup_identity,
		cleanup_controls=cleanup_controls,
	)
	if not can_reclaim or cleanup_identity is None or cleanup_controls is None:
		return
	try:
		_remove_entry(entry, expected_identity=metadata)
	except OSError:
		return
	_remove_cleanup_record(temp_root, entry, metadata, expected_record=cleanup_identity)
	_remove_ownership_record(temp_root, entry, metadata, expected_record=owner_identity)


def _remove_orphan_ownership_record(temp_root: Path, run_directory: Path) -> None:
	for path, expected_format, expected_schema in (
		(
			_ownership_record_path(temp_root, run_directory),
			"quality-gate-temp-workspace-owner",
			_OWNERSHIP_RECORD_SCHEMA,
		),
		(
			_cleanup_record_path(temp_root, run_directory),
			"quality-gate-temp-workspace-cleanup",
			_CLEANUP_RECORD_SCHEMA,
		),
		(
			_cleanup_retry_path(temp_root, run_directory),
			"quality-gate-temp-workspace-cleanup",
			_CLEANUP_RECORD_SCHEMA,
		),
	):
		try:
			metadata = path.lstat()
			if (
				not _plain_lock_file(path, metadata)
				or metadata.st_size > _OWNERSHIP_RECORD_MAX_BYTES
			):
				continue
			with path.open("rb") as stream:
				opened = os.fstat(stream.fileno())
				if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
					continue
				content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
			current = path.lstat()
		except OSError:
			continue
		if (
			len(content) > _OWNERSHIP_RECORD_MAX_BYTES
			or not _same_file_identity(metadata, current)
			or not _plain_lock_file(path, current)
		):
			continue
		payload = _valid_signed_record(content)
		valid_fields = {"device", "format", "inode", "root", "run", "schema"}
		if expected_format == "quality-gate-temp-workspace-cleanup":
			valid_fields |= {
				"active_device",
				"active_inode",
				"marker_device",
				"marker_inode",
			}
		if (
			payload is None
			or set(payload) != valid_fields
			or type(payload["device"]) is not int
			or payload["device"] < 0
			or type(payload["inode"]) is not int
			or payload["inode"] < 0
			or type(payload["schema"]) is not int
			or payload["schema"] != expected_schema
			or payload["format"] != expected_format
			or payload["root"] != os.path.normcase(os.path.abspath(temp_root))
			or payload["run"] != run_directory.name
			or (
				expected_format == "quality-gate-temp-workspace-cleanup"
				and any(
					type(payload[field]) is not int or cast(int, payload[field]) < 0
					for field in (
						"active_device",
						"active_inode",
						"marker_device",
						"marker_inode",
					)
				)
			)
		):
			continue
		try:
			_remove_file(path, metadata)
		except OSError:
			continue


def _can_reclaim_workspace(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	*,
	cleanup_identity: os.stat_result | None,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
) -> tuple[
	bool,
	os.stat_result | None,
	tuple[tuple[int, int], tuple[int, int]] | None,
]:
	try:
		active_lock = _open_locked_file(entry / _ACTIVE_LOCK_NAME, blocking=False, create=False)
	except OSError:
		return False, cleanup_identity, cleanup_controls
	if active_lock is None:
		return _can_reclaim_without_active_lock(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity,
			cleanup_controls,
		)
	try:
		if cleanup_identity is None:
			return _begin_workspace_cleanup(temp_root, entry, metadata, owner_identity, active_lock)
		return _resume_workspace_cleanup(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity,
			cleanup_controls,
		)
	finally:
		_unlock(active_lock)


def _can_reclaim_without_active_lock(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	cleanup_identity: os.stat_result | None,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
) -> tuple[bool, os.stat_result | None, tuple[tuple[int, int], tuple[int, int]] | None]:
	if cleanup_identity is None or cleanup_controls is None:
		return False, cleanup_identity, cleanup_controls
	try:
		(entry / _ACTIVE_LOCK_NAME).lstat()
	except FileNotFoundError:
		return (
			_workspace_ownership_is_current(
				temp_root,
				entry,
				metadata,
				owner_identity,
				cleanup_identity=cleanup_identity,
				cleanup_controls=cleanup_controls,
				allow_missing_marker=True,
			),
			cleanup_identity,
			cleanup_controls,
		)
	return False, cleanup_identity, cleanup_controls


def _begin_workspace_cleanup(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	active_lock: BinaryIO,
) -> tuple[bool, os.stat_result | None, tuple[tuple[int, int], tuple[int, int]] | None]:
	active_file_identity = os.fstat(active_lock.fileno())
	try:
		marker_file_identity = (entry / _OWNERSHIP_MARKER_NAME).lstat()
	except FileNotFoundError:
		return False, None, None
	if not _workspace_ownership_is_current(temp_root, entry, metadata, owner_identity):
		return False, None, None
	try:
		active_current = (entry / _ACTIVE_LOCK_NAME).lstat()
		marker_current = (entry / _OWNERSHIP_MARKER_NAME).lstat()
	except FileNotFoundError:
		return False, None, None
	if not _same_file_identity(active_file_identity, active_current) or not _same_file_identity(
		marker_file_identity, marker_current
	):
		return False, None, None
	cleanup_identity = _write_cleanup_record(
		temp_root, entry, metadata, active_file_identity, marker_file_identity
	)
	if cleanup_identity is None:
		return False, None, None
	cleanup_controls = (
		(active_file_identity.st_dev, active_file_identity.st_ino),
		(marker_file_identity.st_dev, marker_file_identity.st_ino),
	)
	return (
		_workspace_ownership_is_current(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity=cleanup_identity,
			cleanup_controls=cleanup_controls,
		),
		cleanup_identity,
		cleanup_controls,
	)


def _resume_workspace_cleanup(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	cleanup_identity: os.stat_result,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
) -> tuple[bool, os.stat_result | None, tuple[tuple[int, int], tuple[int, int]] | None]:
	if cleanup_controls is None:
		return False, cleanup_identity, cleanup_controls
	return (
		_workspace_ownership_is_current(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity=cleanup_identity,
			cleanup_controls=cleanup_controls,
			allow_missing_marker=True,
		),
		cleanup_identity,
		cleanup_controls,
	)


def _workspace_ownership_is_current(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	*,
	cleanup_identity: os.stat_result | None = None,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None = None,
	allow_missing_marker: bool = False,
) -> bool:
	if not _workspace_owner_is_current(temp_root, entry, metadata, owner_identity):
		return False
	if cleanup_identity is None:
		return _has_valid_ownership_marker(temp_root, entry, expected_directory=metadata)
	return _cleanup_controls_are_current(
		temp_root,
		entry,
		metadata,
		cleanup_identity,
		cleanup_controls,
		allow_missing_marker=allow_missing_marker,
	)


def _workspace_owner_is_current(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
) -> bool:
	try:
		current = entry.lstat()
	except OSError:
		return False
	if (
		is_link_or_reparse(current)
		or not stat.S_ISDIR(current.st_mode)
		or not _same_file_identity(metadata, current)
		or not _has_valid_ownership_record(
			temp_root, entry, metadata, expected_record=owner_identity
		)[0]
	):
		return False
	return True


def _cleanup_controls_are_current(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	cleanup_identity: os.stat_result,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
	*,
	allow_missing_marker: bool,
) -> bool:
	cleanup_controls_from_record: tuple[tuple[int, int], tuple[int, int]] | None = None
	(
		cleanup_valid,
		_cleanup_record_identity,
		active_identity,
		marker_identity,
	) = _has_valid_cleanup_record(temp_root, entry, metadata, expected_record=cleanup_identity)
	if not cleanup_valid or active_identity is None or marker_identity is None:
		return False
	cleanup_controls_from_record = (active_identity, marker_identity)
	if cleanup_controls != cleanup_controls_from_record:
		return False
	active_path = entry / _ACTIVE_LOCK_NAME
	try:
		active_metadata = active_path.lstat()
	except FileNotFoundError:
		if not allow_missing_marker:
			return False
	else:
		if (active_metadata.st_dev, active_metadata.st_ino) != active_identity:
			return False
	marker_path = entry / _OWNERSHIP_MARKER_NAME
	try:
		marker_metadata = marker_path.lstat()
	except FileNotFoundError:
		return allow_missing_marker
	if (marker_metadata.st_dev, marker_metadata.st_ino) != cleanup_controls_from_record[1]:
		return False
	return _has_valid_ownership_marker(temp_root, entry, expected_directory=metadata)


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
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	create_key: bool = True,
) -> bytes:
	payload: dict[str, object] = {
		"device": directory_identity.st_dev,
		"format": "quality-gate-temp-workspace-owner",
		"inode": directory_identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"run": run_directory.name,
		"schema": _OWNERSHIP_RECORD_SCHEMA,
	}
	record = _signed_record(payload, create_key=create_key)
	if record is None:
		raise OSError(f"temporary workspace ownership key is unavailable: {run_directory}")
	return record


def _cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	active_lock_identity: os.stat_result,
	marker_identity: os.stat_result,
	*,
	create_key: bool = True,
) -> bytes:
	payload: dict[str, object] = {
		"active_device": active_lock_identity.st_dev,
		"active_inode": active_lock_identity.st_ino,
		"device": directory_identity.st_dev,
		"format": "quality-gate-temp-workspace-cleanup",
		"inode": directory_identity.st_ino,
		"marker_device": marker_identity.st_dev,
		"marker_inode": marker_identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"run": run_directory.name,
		"schema": _CLEANUP_RECORD_SCHEMA,
	}
	record = _signed_record(payload, create_key=create_key)
	if record is None:
		raise OSError(f"temporary workspace cleanup key is unavailable: {run_directory}")
	return record


def _ownership_record_path(temp_root: Path, run_directory: Path) -> Path:
	return temp_root / f"{_OWNERSHIP_RECORD_PREFIX}{run_directory.name}"


def _cleanup_record_path(temp_root: Path, run_directory: Path) -> Path:
	return temp_root / f"{_CLEANUP_RECORD_PREFIX}{run_directory.name}"


def _cleanup_retry_path(temp_root: Path, run_directory: Path) -> Path:
	return temp_root / f"{_CLEANUP_RETRY_PREFIX}{run_directory.name}"


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
	payload = _valid_signed_record(content)
	expected: dict[str, object] = {
		"device": directory_identity.st_dev,
		"format": "quality-gate-temp-workspace-owner",
		"inode": directory_identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"run": run_directory.name,
		"schema": _OWNERSHIP_RECORD_SCHEMA,
	}
	return payload is not None and _canonical_json(payload) == _canonical_json(expected), metadata


def _has_valid_cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
	verify_directory: bool = True,
) -> tuple[
	bool,
	os.stat_result | None,
	tuple[int, int] | None,
	tuple[int, int] | None,
]:
	last_identity: os.stat_result | None = None
	for path in (
		_cleanup_record_path(temp_root, run_directory),
		_cleanup_retry_path(temp_root, run_directory),
	):
		result = _read_cleanup_record(
			path,
			temp_root,
			run_directory,
			directory_identity,
			expected_record=expected_record,
			verify_directory=verify_directory,
		)
		if result[0]:
			return result
		if result[1] is not None:
			last_identity = result[1]
	return False, last_identity, None, None


def _read_cleanup_record(
	path: Path,
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None,
	verify_directory: bool,
) -> tuple[bool, os.stat_result | None, tuple[int, int] | None, tuple[int, int] | None]:
	try:
		metadata = path.lstat()
		if (
			not _plain_lock_file(path, metadata)
			or metadata.st_size > _OWNERSHIP_RECORD_MAX_BYTES
			or (expected_record is not None and not _same_file_identity(expected_record, metadata))
		):
			return False, metadata, None, None
		with path.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
				return False, metadata, None, None
			content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
		current = path.lstat()
	except OSError:
		return False, None, None, None
	if not _same_file_identity(metadata, current):
		return False, metadata, None, None
	if verify_directory:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False, metadata, None, None
		if not _same_file_identity(directory_identity, current_directory):
			return False, metadata, None, None
	payload = _valid_signed_record(content)
	if (
		payload is None
		or set(payload)
		!= {
			"active_device",
			"active_inode",
			"device",
			"format",
			"inode",
			"marker_device",
			"marker_inode",
			"root",
			"run",
			"schema",
		}
		or type(payload["active_device"]) is not int
		or payload["active_device"] < 0
		or type(payload["active_inode"]) is not int
		or payload["active_inode"] < 0
		or type(payload["device"]) is not int
		or payload["device"] != directory_identity.st_dev
		or type(payload["inode"]) is not int
		or payload["inode"] != directory_identity.st_ino
		or type(payload["marker_device"]) is not int
		or payload["marker_device"] < 0
		or type(payload["marker_inode"]) is not int
		or payload["marker_inode"] < 0
		or type(payload["schema"]) is not int
		or payload["schema"] != _CLEANUP_RECORD_SCHEMA
		or payload["format"] != "quality-gate-temp-workspace-cleanup"
		or payload["root"] != os.path.normcase(os.path.abspath(temp_root))
		or payload["run"] != run_directory.name
	):
		return False, metadata, None, None
	return (
		True,
		metadata,
		(payload["active_device"], payload["active_inode"]),
		(payload["marker_device"], payload["marker_inode"]),
	)


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


def _write_cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	active_lock_identity: os.stat_result,
	marker_identity: os.stat_result,
) -> os.stat_result | None:
	path = _cleanup_record_path(temp_root, run_directory)
	try:
		path.lstat()
	except FileNotFoundError:
		pass
	except OSError:
		return None
	else:
		valid, identity, active_identity, marker_file = _has_valid_cleanup_record(
			temp_root, run_directory, directory_identity
		)
		if valid:
			return (
				identity
				if active_identity == (active_lock_identity.st_dev, active_lock_identity.st_ino)
				and marker_file == (marker_identity.st_dev, marker_identity.st_ino)
				else None
			)
		path = _cleanup_retry_path(temp_root, run_directory)
	try:
		_publish_new_file(
			path,
			_cleanup_record(
				temp_root, run_directory, directory_identity, active_lock_identity, marker_identity
			),
		)
		valid, identity, active_identity, marker_file = _has_valid_cleanup_record(
			temp_root, run_directory, directory_identity
		)
		if (
			not valid
			or identity is None
			or (active_identity != (active_lock_identity.st_dev, active_lock_identity.st_ino))
			or (marker_file != (marker_identity.st_dev, marker_identity.st_ino))
		):
			raise OSError(f"temporary workspace cleanup could not be recorded: {run_directory}")
		return identity
	except FileExistsError:
		valid, identity, active_identity, marker_file = _has_valid_cleanup_record(
			temp_root, run_directory, directory_identity
		)
		return (
			identity
			if valid
			and active_identity == (active_lock_identity.st_dev, active_lock_identity.st_ino)
			and marker_file == (marker_identity.st_dev, marker_identity.st_ino)
			else None
		)
	except OSError:
		return None


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


def _remove_cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
) -> None:
	valid, identity, _active_identity, _marker_identity = _has_valid_cleanup_record(
		temp_root,
		run_directory,
		directory_identity,
		expected_record=expected_record,
		verify_directory=False,
	)
	if valid and identity is not None:
		for path in (
			_cleanup_record_path(temp_root, run_directory),
			_cleanup_retry_path(temp_root, run_directory),
		):
			try:
				current = path.lstat()
			except FileNotFoundError:
				continue
			if _same_file_identity(identity, current):
				_remove_file(path, identity)
				break


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
