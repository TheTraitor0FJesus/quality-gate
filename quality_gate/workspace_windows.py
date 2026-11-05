"""Windows security and identity-bound temporary file removal."""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path
from typing import Any, cast

from .path_safety import is_link_or_reparse

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
