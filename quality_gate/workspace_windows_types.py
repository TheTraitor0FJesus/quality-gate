"""Windows handle information structures."""

import ctypes
from ctypes import wintypes


class _WindowsByHandleFileInformation(ctypes.Structure):
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


class _WindowsFileIdentifier(ctypes.Structure):
	_fields_ = [("low", ctypes.c_uint64), ("high", ctypes.c_uint64)]


class _WindowsFileIdInformation(ctypes.Structure):
	_fields_ = [
		("volume_serial", ctypes.c_uint64),
		("file_id", _WindowsFileIdentifier),
	]


class _WindowsFileBasicInformation(ctypes.Structure):
	_fields_ = [
		("creation_time", ctypes.c_longlong),
		("last_access_time", ctypes.c_longlong),
		("last_write_time", ctypes.c_longlong),
		("change_time", ctypes.c_longlong),
		("attributes", wintypes.DWORD),
	]


class _WindowsFileDispositionInformationEx(ctypes.Structure):
	_fields_ = [("flags", wintypes.DWORD)]


class _WindowsFileDispositionInformation(ctypes.Structure):
	_fields_ = [("delete_file", wintypes.BOOLEAN)]
