"""Own Windows descendants even after the original process exits.

Create the process suspended, assign its job, then resume its initial thread.
The job handle is never inherited; closing it terminates all remaining members.
ABI reference: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from time import monotonic, sleep
from typing import Any, ClassVar

CREATE_SUSPENDED = 0x00000004
KILL_ON_CLOSE = 0x00002000
EXTENDED_LIMIT_INFORMATION = 9
PROCESS_ASSIGN_ACCESS = 0x0100 | 0x0001
THREAD_SUSPEND_RESUME = 0x0002
SNAPSHOT_THREADS = 0x00000004
INVALID_HANDLE = ctypes.c_void_p(-1).value
RESUME_FAILED = 0xFFFFFFFF
BASIC_ACCOUNTING_INFORMATION = 1


class BasicLimits(ctypes.Structure):
	_fields_: ClassVar[list[tuple[str, Any]]] = [
		("process_time", ctypes.c_int64),
		("job_time", ctypes.c_int64),
		("flags", wintypes.DWORD),
		("minimum_working_set", ctypes.c_size_t),
		("maximum_working_set", ctypes.c_size_t),
		("active_processes", wintypes.DWORD),
		("affinity", ctypes.c_size_t),
		("priority", wintypes.DWORD),
		("scheduling", wintypes.DWORD),
	]


class IoCounters(ctypes.Structure):
	_fields_: ClassVar[list[tuple[str, Any]]] = [
		("read_operations", ctypes.c_uint64),
		("write_operations", ctypes.c_uint64),
		("other_operations", ctypes.c_uint64),
		("read_bytes", ctypes.c_uint64),
		("write_bytes", ctypes.c_uint64),
		("other_bytes", ctypes.c_uint64),
	]


class ExtendedLimits(ctypes.Structure):
	_fields_: ClassVar[list[tuple[str, Any]]] = [
		("basic", BasicLimits),
		("io", IoCounters),
		("process_memory", ctypes.c_size_t),
		("job_memory", ctypes.c_size_t),
		("peak_process_memory", ctypes.c_size_t),
		("peak_job_memory", ctypes.c_size_t),
	]


class ThreadEntry(ctypes.Structure):
	_fields_: ClassVar[list[tuple[str, Any]]] = [
		("size", wintypes.DWORD),
		("usage", wintypes.DWORD),
		("thread_id", wintypes.DWORD),
		("process_id", wintypes.DWORD),
		("base_priority", wintypes.LONG),
		("delta_priority", wintypes.LONG),
		("flags", wintypes.DWORD),
	]


class JobAccounting(ctypes.Structure):
	_fields_: ClassVar[list[tuple[str, Any]]] = [
		("user_time", ctypes.c_int64),
		("kernel_time", ctypes.c_int64),
		("period_user_time", ctypes.c_int64),
		("period_kernel_time", ctypes.c_int64),
		("page_faults", wintypes.DWORD),
		("total_processes", wintypes.DWORD),
		("active_processes", wintypes.DWORD),
		("terminated_processes", wintypes.DWORD),
	]


def kernel_api() -> ctypes.CDLL:
	if sys.platform != "win32":
		raise OSError("windows_jobs_unavailable")
	kernel = ctypes.WinDLL("kernel32", use_last_error=True)
	definitions = {
		"CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
		"SetInformationJobObject": (
			[wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
			wintypes.BOOL,
		),
		"AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
		"OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
		"CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
		"Thread32First": ([wintypes.HANDLE, wintypes.LPVOID], wintypes.BOOL),
		"Thread32Next": ([wintypes.HANDLE, wintypes.LPVOID], wintypes.BOOL),
		"OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
		"ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
		"TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
		"CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
		"QueryInformationJobObject": (
			[wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID],
			wintypes.BOOL,
		),
	}
	for name, (arguments, result) in definitions.items():
		function = getattr(kernel, name)
		function.argtypes = arguments
		function.restype = result
	return kernel


def require_handle(handle: int | None) -> int:
	if not handle or handle == INVALID_HANDLE:
		raise OSError("windows_handle_unavailable")
	return handle


class WindowsJob:
	"""An unnamed kill-on-close job; no breakaway permission is enabled."""

	def __init__(self) -> None:
		self.kernel = kernel_api()
		self.handle: int | None = require_handle(self.kernel.CreateJobObjectW(None, None))
		limits = ExtendedLimits()
		limits.basic.flags = KILL_ON_CLOSE
		if not self.kernel.SetInformationJobObject(
			self.handle, EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
		):
			self.close()
			raise OSError("windows_job_unavailable")

	def attach_and_resume(self, pid: int, deadline: float) -> None:
		process = require_handle(self.kernel.OpenProcess(PROCESS_ASSIGN_ACCESS, False, pid))
		try:
			if not self.kernel.AssignProcessToJobObject(self.handle, process):
				raise OSError("windows_job_assignment_failed")
		finally:
			self.kernel.CloseHandle(process)
		self.resume_initial_thread(pid, deadline)

	def resume_initial_thread(self, pid: int, deadline: float) -> None:
		# Popen closes its initial thread handle. Toolhelp locates it while the
		# process is still suspended, before any application code can spawn children.
		snapshot = require_handle(self.kernel.CreateToolhelp32Snapshot(SNAPSHOT_THREADS, 0))
		entry = ThreadEntry()
		entry.size = ctypes.sizeof(entry)
		try:
			present = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
			while present and monotonic() < deadline:
				if entry.process_id == pid:
					self.resume_thread(entry.thread_id)
					return
				present = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
		finally:
			self.kernel.CloseHandle(snapshot)
		raise OSError("windows_thread_unavailable")

	def resume_thread(self, tid: int) -> None:
		thread = require_handle(self.kernel.OpenThread(THREAD_SUSPEND_RESUME, False, tid))
		try:
			previous = self.kernel.ResumeThread(thread)
			if previous == RESUME_FAILED or previous != 1:
				raise OSError("windows_resume_failed")
		finally:
			self.kernel.CloseHandle(thread)

	def terminate(self) -> None:
		if self.handle is not None and not self.kernel.TerminateJobObject(self.handle, 1):
			raise OSError("windows_job_termination_failed")

	def wait_empty(self, deadline: float) -> None:
		accounting = JobAccounting()
		while True:
			if not self.kernel.QueryInformationJobObject(
				self.handle,
				BASIC_ACCOUNTING_INFORMATION,
				ctypes.byref(accounting),
				ctypes.sizeof(accounting),
				None,
			):
				raise OSError("windows_job_query_failed")
			if accounting.active_processes == 0:
				return
			remaining = deadline - monotonic()
			if remaining <= 0:
				raise OSError("windows_job_cleanup_deadline")
			sleep(min(remaining, 0.005))

	def close(self) -> None:
		if self.handle is not None:
			handle, self.handle = self.handle, None
			if not self.kernel.CloseHandle(handle):
				raise OSError("windows_job_close_failed")
