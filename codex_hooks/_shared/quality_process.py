"""Bounded subprocess supervision for synchronous edit hooks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import BinaryIO

from _shared.quality_windows_jobs import CREATE_SUSPENDED, WindowsJob

MAX_OUTPUT_BYTES = 256 * 1024
CLEANUP_SECONDS = 3.0


class ProcessError(RuntimeError):
	"""A fixed, source-free reason for incomplete process execution."""


@dataclass(frozen=True, slots=True)
class Execution:
	returncode: int
	stdout: str
	stderr: str
	elapsed_ms: int


def stop_tree(process: subprocess.Popen[bytes], deadline: float, job: WindowsJob | None) -> None:
	"""Terminate only the process tree started by this invocation."""
	if job is not None:
		job.terminate()
	elif sys.platform != "win32":
		try:
			os.killpg(process.pid, signal.SIGKILL)
		except ProcessLookupError:
			pass
	else:
		raise ProcessError("process_cleanup")
	if process.poll() is None:
		process.kill()
	process.wait(timeout=max(0.001, deadline - monotonic()))


def finish_process(
	process: subprocess.Popen[bytes],
	workers: tuple[threading.Thread, ...],
	*,
	cancel: bool,
	job: WindowsJob | None,
) -> None:
	"""Share one cleanup deadline across cancellation and pipe draining."""
	deadline = monotonic() + CLEANUP_SECONDS
	try:
		if sys.platform != "win32" or job is not None or cancel or process.poll() is None:
			stop_tree(process, deadline, job)
		if job is not None:
			job.wait_empty(deadline)
	except (OSError, subprocess.TimeoutExpired) as error:
		if process.poll() is None:
			process.kill()
		raise ProcessError("process_cleanup") from error
	finally:
		try:
			if job is not None:
				job.close()
		finally:
			for worker in workers:
				worker.join(max(0, deadline - monotonic()))
	if any(worker.is_alive() for worker in workers):
		raise ProcessError("process_cleanup")


def read_bounded(
	stream: BinaryIO, buffer: bytearray, limit: int, overflow: threading.Event
) -> None:
	try:
		while block := stream.read(4096):
			remaining = max(0, limit - len(buffer))
			buffer.extend(block[:remaining])
			if len(block) > remaining:
				overflow.set()
				return
	finally:
		stream.close()


def send_input(stream: BinaryIO, input_text: str) -> None:
	try:
		stream.write(input_text.encode("utf-8"))
		stream.flush()
	except (BrokenPipeError, OSError):
		# A CLI may reject the request before reading all bounded input.
		pass
	finally:
		try:
			stream.close()
		except (BrokenPipeError, OSError):
			pass


def await_workers(
	process: subprocess.Popen[bytes],
	workers: tuple[threading.Thread, ...],
	overflow: threading.Event,
	deadline: float,
	cancel_event: threading.Event | None = None,
) -> str | None:
	"""Stop on output overflow or deadline, including while draining inherited pipes."""
	while process.poll() is None or any(worker.is_alive() for worker in workers):
		if cancel_event is not None and cancel_event.is_set():
			return "cancelled"
		if overflow.is_set():
			return "output_limit"
		remaining = deadline - monotonic()
		if remaining <= 0:
			return "deadline"
		overflow.wait(min(remaining, 0.005))
	return None


def validate_limits(deadline: float, output_limit: int) -> None:
	if monotonic() >= deadline:
		raise ProcessError("deadline")
	if output_limit <= 0 or output_limit > MAX_OUTPUT_BYTES:
		raise ProcessError("invalid_output_limit")


def start_process(
	arguments: list[str], cwd: Path | None, environment: dict[str, str], deadline: float
) -> tuple[subprocess.Popen[bytes], WindowsJob | None]:
	job = None
	process = None
	try:
		job = WindowsJob() if sys.platform == "win32" else None
		process = subprocess.Popen(
			arguments,
			cwd=cwd,
			env=environment,
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			start_new_session=os.name != "nt",
			creationflags=CREATE_SUSPENDED if job is not None else 0,
		)
		if job is not None:
			job.attach_and_resume(process.pid, deadline)
	except OSError as error:
		if process is not None:
			try:
				finish_process(process, (), cancel=True, job=job)
			finally:
				for stream in (process.stdin, process.stdout, process.stderr):
					if stream is not None:
						stream.close()
		elif job is not None:
			job.close()
		raise ProcessError("process_unavailable") from error
	return process, job


def execute(
	arguments: list[str],
	*,
	deadline: float,
	input_text: str = "",
	cwd: Path | None = None,
	environment: dict[str, str] | None = None,
	output_limit: int = MAX_OUTPUT_BYTES,
	cancel_event: threading.Event | None = None,
) -> Execution:
	"""Capture bounded UTF-8 output under one absolute monotonic deadline."""
	started = monotonic()
	validate_limits(deadline, output_limit)
	if cancel_event is not None and cancel_event.is_set():
		raise ProcessError("cancelled")
	child_environment = dict(os.environ if environment is None else environment)
	child_environment.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
	process, job = start_process(arguments, cwd, child_environment, deadline)
	if process.stdin is None or process.stdout is None or process.stderr is None:
		finish_process(process, (), cancel=True, job=job)
		raise ProcessError("process_io_unavailable")
	overflow = threading.Event()
	stdout, stderr = bytearray(), bytearray()
	workers = (
		threading.Thread(
			target=read_bounded, args=(process.stdout, stdout, output_limit, overflow), daemon=True
		),
		threading.Thread(
			target=read_bounded, args=(process.stderr, stderr, output_limit, overflow), daemon=True
		),
		threading.Thread(target=send_input, args=(process.stdin, input_text), daemon=True),
	)
	for worker in workers:
		worker.start()
	reason = None
	try:
		reason = await_workers(process, workers, overflow, deadline, cancel_event)
	finally:
		finish_process(process, workers, cancel=reason is not None, job=job)
	if reason is not None or overflow.is_set():
		raise ProcessError(reason or "output_limit")
	try:
		return Execution(
			process.returncode,
			stdout.decode("utf-8"),
			stderr.decode("utf-8"),
			int((monotonic() - started) * 1000),
		)
	except UnicodeError as error:
		raise ProcessError("invalid_output") from error
