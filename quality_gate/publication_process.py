"""Supervise bounded publisher subprocess trees."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event, Lock

from .publication import PublicationError

WINDOWS_HELPER_MARKER = "--quality-gate-bounded-child-v1"
WINDOWS_HELPER_READY = b"\x00quality-gate-ready-v2\n"
WINDOWS_HELPER_GATE = b"\x00quality-gate-run-v1\n"


def _kill_process_group(process_id: int) -> None:
	try:
		os.killpg(process_id, signal.SIGKILL)  # type: ignore[attr-defined]
	except ProcessLookupError:
		pass


class _WindowsJob:
	"""Contain one helper and its ordinary descendants in a kill-on-close job."""

	def __init__(self) -> None:
		from ctypes import wintypes

		self._kernel32: ctypes.CDLL = ctypes.WinDLL(  # type: ignore[attr-defined]
			"kernel32", use_last_error=True
		)
		self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
		self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
		self._kernel32.SetInformationJobObject.argtypes = [
			wintypes.HANDLE,
			wintypes.INT,
			ctypes.c_void_p,
			wintypes.DWORD,
		]
		self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
		self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
		self._kernel32.OpenProcess.restype = wintypes.HANDLE
		self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
		self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
		self._kernel32.IsProcessInJob.argtypes = [
			wintypes.HANDLE,
			wintypes.HANDLE,
			ctypes.POINTER(wintypes.BOOL),
		]
		self._kernel32.IsProcessInJob.restype = wintypes.BOOL
		self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
		self._kernel32.CloseHandle.restype = wintypes.BOOL
		self._handle: object | None = self._kernel32.CreateJobObjectW(None, None)
		if not self._handle:
			raise self._last_error()
		try:
			self._set_kill_on_close()
		except BaseException:
			self.close()
			raise

	@staticmethod
	def _last_error() -> OSError:
		code = ctypes.get_last_error()  # type: ignore[attr-defined]
		return OSError(code, "Windows process containment operation failed")

	def _set_kill_on_close(self) -> None:
		from ctypes import wintypes

		class BasicLimitInformation(ctypes.Structure):
			_fields_ = [
				("PerProcessUserTimeLimit", ctypes.c_longlong),
				("PerJobUserTimeLimit", ctypes.c_longlong),
				("LimitFlags", wintypes.DWORD),
				("MinimumWorkingSetSize", ctypes.c_size_t),
				("MaximumWorkingSetSize", ctypes.c_size_t),
				("ActiveProcessLimit", wintypes.DWORD),
				("Affinity", ctypes.c_size_t),
				("PriorityClass", wintypes.DWORD),
				("SchedulingClass", wintypes.DWORD),
			]

		class IoCounters(ctypes.Structure):
			_fields_ = [
				(name, ctypes.c_ulonglong)
				for name in (
					"ReadOperationCount",
					"WriteOperationCount",
					"OtherOperationCount",
					"ReadTransferCount",
					"WriteTransferCount",
					"OtherTransferCount",
				)
			]

		class ExtendedLimitInformation(ctypes.Structure):
			_fields_ = [
				("BasicLimitInformation", BasicLimitInformation),
				("IoInfo", IoCounters),
				("ProcessMemoryLimit", ctypes.c_size_t),
				("JobMemoryLimit", ctypes.c_size_t),
				("PeakProcessMemoryUsed", ctypes.c_size_t),
				("PeakJobMemoryUsed", ctypes.c_size_t),
			]

		information = ExtendedLimitInformation()
		information.BasicLimitInformation.LimitFlags = 0x2000
		if not self._kernel32.SetInformationJobObject(
			self._handle,
			9,
			ctypes.byref(information),
			ctypes.sizeof(information),
		):
			raise self._last_error()

	def assign(self, process_id: int) -> None:
		from ctypes import wintypes

		process_handle = self._kernel32.OpenProcess(0x0100 | 0x0001 | 0x1000, False, process_id)
		if not process_handle:
			raise self._last_error()
		try:
			if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
				raise self._last_error()
			is_in_job = wintypes.BOOL()
			if not self._kernel32.IsProcessInJob(
				process_handle, self._handle, ctypes.byref(is_in_job)
			):
				raise self._last_error()
			if not is_in_job.value:
				raise OSError("Windows helper is not contained in its Job Object")
		finally:
			if not self._kernel32.CloseHandle(wintypes.HANDLE(process_handle)):
				raise self._last_error()

	def close(self) -> None:
		if self._handle:
			handle = self._handle
			if self._kernel32.CloseHandle(handle):
				self._handle = None
			else:
				raise self._last_error()


class _CommandControl:
	"""Serialize launch-gate release and bounded process-tree teardown."""

	def __init__(
		self,
		process: subprocess.Popen[bytes],
		*,
		job: _WindowsJob | None = None,
		gated: bool = False,
	) -> None:
		self.process = process
		self.job = job
		self.gated = gated
		self._assigned = False
		self._aborted = False
		self._closed = False
		self._lock = Lock()

	def assign(self, process_id: int | None = None) -> None:
		with self._lock:
			if self._aborted or self._closed:
				raise PublicationError("command launch was cancelled before assignment")
			if not self.gated:
				return
			if process_id is None or self.job is None:
				raise PublicationError("command containment helper identity is unavailable")
			self.job.assign(process_id)
			self._assigned = True

	def release(self) -> None:
		with self._lock:
			if self._aborted or self._closed:
				raise PublicationError("command launch was cancelled before release")
			if not self.gated:
				return
			if not self._assigned or self.process.stdin is None:
				raise PublicationError("command launch gate is not assigned")
			try:
				written = self.process.stdin.write(WINDOWS_HELPER_GATE)
				if written != len(WINDOWS_HELPER_GATE):
					raise OSError("command launch gate write was incomplete")
				self.process.stdin.flush()
				self.process.stdin.close()
			except (OSError, ValueError) as error:
				self._terminate_locked()
				raise PublicationError("command launch gate could not be released") from error

	def terminate(self) -> None:
		with self._lock:
			if self._closed:
				return
			self._aborted = True
			self._terminate_locked()

	def _terminate_locked(self) -> None:
		if self.job is not None and self._assigned:
			job = self.job
			job.close()
			self.job = None
		else:
			if os.name == "nt":
				try:
					self.process.kill()
				except OSError:
					if self.process.poll() is None:
						raise
			else:
				_kill_process_group(self.process.pid)
		if self.gated and self.process.stdin is not None and not self.process.stdin.closed:
			self.process.stdin.close()

	def close(self) -> None:
		with self._lock:
			if self._closed:
				return
			self._aborted = True
			try:
				self._terminate_locked()
			finally:
				if self.job is not None:
					job = self.job
					job.close()
					self.job = None
			self._closed = True


class _CommandSession:
	"""Own a subprocess, its process tree, and any Windows launch gate."""

	def __init__(
		self,
		process: subprocess.Popen[bytes],
		control: _CommandControl,
		*,
		startup_prefix: bytes = b"",
	) -> None:
		self.process = process
		self.control = control
		self.startup_prefix = startup_prefix


class _CommandOutput:
	"""Collect bounded child output and wake the process supervisor on failure."""

	def __init__(self) -> None:
		self.stdout = bytearray()
		self.stderr = bytearray()
		self.startup_ready = Event()
		self.changed = Event()
		self.failures: list[str] = []
		self.startup_process_id: int | None = None
		self._lock = Lock()

	def fail(self, detail: str, control: _CommandControl) -> None:
		with self._lock:
			self.failures.append(detail)
			self.changed.set()
		try:
			control.terminate()
		except Exception:
			with self._lock:
				self.failures.append("command process containment failed")
				self.changed.set()

	def failure(self) -> str | None:
		with self._lock:
			return self.failures[0] if self.failures else None

	def ready(self, process_id: int) -> None:
		with self._lock:
			self.startup_process_id = process_id
			self.startup_ready.set()
			self.changed.set()

	def helper_process_id(self) -> int | None:
		with self._lock:
			return self.startup_process_id

	def failures_snapshot(self) -> tuple[str, ...]:
		with self._lock:
			return tuple(self.failures)


def _spawn_bounded_command(command: list[str], environment: dict[str, str]) -> _CommandSession:
	if os.name != "nt":
		process = subprocess.Popen(
			command,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=environment,
			start_new_session=True,
		)
		return _CommandSession(process, _CommandControl(process))

	job = _WindowsJob()
	helper = Path(__file__).with_name("_windows_command.py")
	try:
		process = subprocess.Popen(
			[
				sys.executable,
				"-I",
				"-S",
				"-B",
				str(helper),
				WINDOWS_HELPER_MARKER,
				*command,
			],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=environment,
		)
	except BaseException:
		job.close()
		raise
	return _CommandSession(
		process,
		_CommandControl(process, job=job, gated=True),
		startup_prefix=WINDOWS_HELPER_READY,
	)
