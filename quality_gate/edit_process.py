"""Bounded process IO and ownership for the edit invocation supervisor."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .edit_protocol import MAX_TOOL_BYTES, EditError


def _windows_job(process: subprocess.Popen[bytes]) -> tuple[Any, int]:
	# A job owns descendants even after their immediate parent has exited.
	import ctypes
	from ctypes import wintypes

	class Limits(ctypes.Structure):
		_fields_ = [
			("process_time", ctypes.c_int64),
			("job_time", ctypes.c_int64),
			("flags", wintypes.DWORD),
			("minimum", ctypes.c_size_t),
			("maximum", ctypes.c_size_t),
			("active", wintypes.DWORD),
			("affinity", ctypes.c_size_t),
			("priority", wintypes.DWORD),
			("scheduling", wintypes.DWORD),
		]

	class ExtendedLimits(ctypes.Structure):
		_fields_ = [
			("basic", Limits),
			("io", ctypes.c_uint64 * 6),
			("process_memory", ctypes.c_size_t),
			("job_memory", ctypes.c_size_t),
			("peak_process", ctypes.c_size_t),
			("peak_job", ctypes.c_size_t),
		]

	kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
	kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
	kernel.CreateJobObjectW.restype = wintypes.HANDLE
	kernel.SetInformationJobObject.argtypes = [
		wintypes.HANDLE,
		ctypes.c_int,
		ctypes.c_void_p,
		wintypes.DWORD,
	]
	kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
	kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
	kernel.OpenProcess.restype = wintypes.HANDLE
	kernel.CloseHandle.argtypes = [wintypes.HANDLE]
	job = kernel.CreateJobObjectW(None, None)
	limits = ExtendedLimits()
	limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if not job:
		raise EditError("runtime")
	process_handle = kernel.OpenProcess(0x0101, False, process.pid)
	assigned = (
		process_handle
		and kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
		and kernel.AssignProcessToJobObject(job, process_handle)
	)
	if process_handle:
		kernel.CloseHandle(process_handle)
	if not assigned:
		kernel.CloseHandle(job)
		raise EditError("runtime")
	return kernel, job


class _Streams:
	def __init__(self, process: subprocess.Popen[bytes], payload: bytes | None, limit: int) -> None:
		self.process = process
		self.payload = payload
		self.limit = limit
		self.outputs = [bytearray(), bytearray()]
		self.failures: list[str] = []
		self.readers = [
			threading.Thread(target=self.drain, args=(index,), daemon=True) for index in range(2)
		]
		if payload is not None:
			self.readers.append(threading.Thread(target=self.send, daemon=True))

	def drain(self, index: int) -> None:
		stream = self.process.stdout if index == 0 else self.process.stderr
		if stream is None:
			self.failures.append("tool_output")
			return
		try:
			with stream:
				while chunk := stream.read(4096):
					remaining = self.limit - len(self.outputs[index])
					self.outputs[index].extend(chunk[: max(0, remaining)])
					if len(chunk) > remaining and not self.failures:
						self.failures.append("limits")
		except (OSError, ValueError):
			self.failures.append("tool_output")

	def send(self) -> None:
		if self.process.stdin is None:
			return
		try:
			with self.process.stdin:
				self.process.stdin.write(self.payload or b"")
		except (OSError, ValueError):
			self.failures.append("tool_output")

	def join(self, deadline: float) -> None:
		for reader in self.readers:
			if reader.ident is not None:
				reader.join(timeout=max(0, deadline - time.monotonic()))

	def result(self, deadline: float) -> tuple[int, bytes, bytes]:
		self.join(deadline)
		if any(reader.is_alive() for reader in self.readers):
			raise EditError("deadline")
		if self.failures:
			raise EditError(self.failures[0])
		if self.process.returncode is None:
			raise EditError("tool_output")
		return self.process.returncode, bytes(self.outputs[0]), bytes(self.outputs[1])


def _stop(process: subprocess.Popen[bytes], job: tuple[Any, int] | None, own_tree: bool) -> None:
	if job is not None:
		kernel, handle = job
		kernel.CloseHandle(handle)
	elif own_tree and sys.platform != "win32":
		try:
			os.killpg(process.pid, signal.SIGKILL)
		except ProcessLookupError:
			pass
	elif process.poll() is None:
		process.kill()
	try:
		process.wait(timeout=1)
	except subprocess.TimeoutExpired as error:
		raise EditError("deadline") from error


def _resume_windows(process: subprocess.Popen[bytes]) -> None:
	"""Resume a native child only after its complete tree belongs to our job."""
	import ctypes
	from ctypes import wintypes

	kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
	kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
	kernel.OpenProcess.restype = wintypes.HANDLE
	kernel.CloseHandle.argtypes = [wintypes.HANDLE]
	ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
	ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
	ntdll.NtResumeProcess.restype = wintypes.LONG
	handle = kernel.OpenProcess(0x0800, False, process.pid)  # PROCESS_SUSPEND_RESUME
	if not handle:
		raise EditError("runtime")
	try:
		if ntdll.NtResumeProcess(handle) != 0:
			raise EditError("runtime")
	finally:
		kernel.CloseHandle(handle)


def run_process(
	command: list[str],
	*,
	root: Path,
	environment: dict[str, str],
	deadline: float,
	payload: bytes | None = None,
	limit: int = MAX_TOOL_BYTES,
	own_tree: bool = False,
	cancelled: Callable[[], bool] | None = None,
) -> tuple[int, bytes, bytes]:
	"""Drain bounded streams; the supervisor owns and reaps its whole child tree."""
	if time.monotonic() >= deadline:
		raise EditError("deadline")
	if cancelled is not None and cancelled():
		raise EditError("cancelled")
	process = subprocess.Popen(
		command,
		cwd=root,
		env=environment,
		stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		start_new_session=own_tree and os.name != "nt",
		creationflags=0x00000004 if own_tree and os.name == "nt" else 0,
	)
	job = None
	streams = _Streams(process, payload, limit)
	try:
		# Suspended creation also protects native scanners without a stdin handshake.
		if own_tree and os.name == "nt":
			job = _windows_job(process)
			_resume_windows(process)
		for reader in streams.readers:
			reader.start()
		_wait_process(process, deadline, cancelled)
		return streams.result(deadline)
	finally:
		_stop(process, job, own_tree)
		streams.join(time.monotonic() + 1)


def _wait_process(
	process: subprocess.Popen[bytes], deadline: float, cancelled: Callable[[], bool] | None
) -> None:
	while True:
		if cancelled is not None and cancelled():
			raise EditError("cancelled")
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise EditError("deadline")
		try:
			process.wait(timeout=min(0.05, remaining) if cancelled is not None else remaining)
			return
		except subprocess.TimeoutExpired as error:
			if time.monotonic() >= deadline:
				raise EditError("deadline") from error
