"""Standard GitHub CLI transport without product installation or startup."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread
from time import monotonic
from typing import BinaryIO, NoReturn, Protocol
from urllib.parse import quote

from .publication import MissingResourceError, PublicationError
from .publication_artifacts import MAX_BUNDLE_BYTES, json_record
from .publication_evidence import positive, record
from .publication_writer import image_repository, preflight_image_archive, push_image_archive

JSON_LIMIT = 16 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 300
MAX_IMAGE_WRITER_TIMEOUT_SECONDS = 25 * 60
MAX_COMMAND_STDERR_BYTES = 4096
COMMAND_CLEANUP_GRACE_SECONDS = 2
MAX_GHCR_TOKEN_BYTES = 64 * 1024
MAX_GHCR_USERNAME_LENGTH = 255
WINDOWS_HELPER_MARKER = "--quality-gate-bounded-child-v1"
WINDOWS_HELPER_READY = b"\x00quality-gate-ready-v2\n"
WINDOWS_HELPER_PID_BYTES = 4
WINDOWS_HELPER_GATE = b"\x00quality-gate-run-v1\n"
WINDOWS_HELPER_UNAVAILABLE = b"\x00quality-gate-command-unavailable-v1\n"
WINDOWS_HELPER_UNAVAILABLE_EXIT = 127
GH_ESCAPE_SEQUENCE_ERROR = (
	b"the response contains terminal escape sequences; pass --allow-escape-sequences"
)
HTTP_STATUS = re.compile(
	rb"""(?:\(\s*HTTP\s+([1-5][0-9]{2})\)|\bHTTP\s+([1-5][0-9]{2})\b|
		(?:response\s+)?status\s+code:?\s*([1-5][0-9]{2})\b|\b(404)\s+Not Found\b)""",
	re.IGNORECASE | re.VERBOSE,
)
GH_API_ARGUMENTS = 3


def _operation(command: Sequence[str]) -> str:
	"""Describe one external operation without echoing arguments or query data."""
	if len(command) >= GH_API_ARGUMENTS and command[0] == "gh" and command[1] == "api":
		method = "GET"
		if "--method" in command:
			index = command.index("--method") + 1
			candidate = command[index] if index < len(command) else ""
			if candidate in {"GET", "POST", "PATCH", "DELETE"}:
				method = candidate
		endpoint = command[2].split("?", 1)[0]
		if endpoint.startswith("https://uploads.github.com/"):
			endpoint = endpoint.removeprefix("https://uploads.github.com/")
		if re.fullmatch(
			r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@-]+)*",
			endpoint,
		):
			return f"gh api {method} {endpoint}"
		return f"gh api {method} <endpoint>"
	if command and command[0] == "docker":
		return "docker command"
	if command and command[0] == "git":
		return "git command"
	return "external command"


def _http_status(detail: bytes) -> str | None:
	"""Extract only a bounded HTTP status from a child diagnostic."""
	match = HTTP_STATUS.search(detail)
	if match is None:
		return None
	return next((group.decode("ascii") for group in match.groups() if group), None)


def _kill_process_group(process_id: int) -> None:
	try:
		os.killpg(process_id, signal.SIGKILL)  # type: ignore[attr-defined]
	except ProcessLookupError:
		pass


@contextmanager
def registry_configuration(token: str, username: str) -> Iterator[None]:
	"""Expose the caller's short-lived GHCR token through a temporary Docker config."""
	if not token or not username:
		raise PublicationError("caller GITHUB_TOKEN and actor are required for GHCR access")
	if len(token.encode("utf-8")) > MAX_GHCR_TOKEN_BYTES:
		raise PublicationError("GHCR token exceeds 64 KiB")
	if any(character.isspace() or character == "\x00" for character in token):
		raise PublicationError("GHCR token contains invalid characters")
	if len(username) > MAX_GHCR_USERNAME_LENGTH or any(
		character.isspace() or character in ":\x00" for character in username
	):
		raise PublicationError("GitHub actor is invalid for GHCR authentication")
	auth = base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
	content = json.dumps({"auths": {"ghcr.io": {"auth": auth}}}, separators=(",", ":"))
	previous = os.environ.get("DOCKER_CONFIG")
	with tempfile.TemporaryDirectory(prefix="publisher-registry-") as directory:
		path = Path(directory) / "config.json"
		path.write_text(content, encoding="utf-8")
		path.chmod(0o600)
		os.environ["DOCKER_CONFIG"] = directory
		try:
			yield
		finally:
			if previous is None:
				os.environ.pop("DOCKER_CONFIG", None)
			else:
				os.environ["DOCKER_CONFIG"] = previous


class Command(Protocol):
	def __call__(
		self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
	) -> bytes: ...


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


class EscapeSequenceError(PublicationError):
	"""GitHub CLI refused a raw response containing terminal escape sequences."""


class BoundedCommand:
	"""Bound child commands, optionally sharing a monotonic deadline across calls."""

	def __init__(
		self,
		timeout_seconds: float,
		*,
		environment: Mapping[str, str] | None = None,
		maximum_timeout_seconds: float = MAX_TIMEOUT_SECONDS,
		aggregate_timeout_seconds: float | None = None,
	) -> None:
		if (
			not 0 < maximum_timeout_seconds <= MAX_IMAGE_WRITER_TIMEOUT_SECONDS
			or not 0 < timeout_seconds <= maximum_timeout_seconds
			or (
				aggregate_timeout_seconds is not None
				and not 0 < aggregate_timeout_seconds <= maximum_timeout_seconds
			)
		):
			raise PublicationError("external command timeout exceeds its configured bound")
		self.timeout_seconds = timeout_seconds
		self.aggregate_timeout_seconds = aggregate_timeout_seconds
		self._aggregate_deadline: float | None = None
		self._deadline_lock = Lock()
		self.environment = dict(environment or {})

	def __call__(
		self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
	) -> bytes:
		deadline = self._begin_aggregate_budget()
		with tempfile.TemporaryDirectory(prefix="publisher-") as directory:
			command = list(arguments)
			if content is not None:
				input_file = Path(directory) / "input"
				input_file.write_bytes(content)
				command.extend(["--input", str(input_file)])
			return self._execute(command, limit, deadline)

	def _begin_aggregate_budget(self) -> float | None:
		if self.aggregate_timeout_seconds is None:
			return None
		with self._deadline_lock:
			if self._aggregate_deadline is None:
				self._aggregate_deadline = monotonic() + self.aggregate_timeout_seconds
			return self._aggregate_deadline

	def _remaining_timeout(self, operation: str, deadline: float | None) -> float:
		budget_seconds = self.aggregate_timeout_seconds
		if deadline is None or budget_seconds is None:
			return self.timeout_seconds
		remaining = deadline - monotonic()
		if remaining <= 0:
			raise PublicationError(
				f"{operation}: shared image-command budget of "
				f"{budget_seconds:g} seconds is exhausted"
			)
		return min(self.timeout_seconds, remaining)

	def _execute(self, command: list[str], limit: int, deadline: float | None) -> bytes:
		operation = _operation(command)
		environment = os.environ.copy()
		environment.update(self.environment)
		try:
			self._remaining_timeout(operation, deadline)
			return self._run_bounded(command, limit, deadline, operation, environment)
		except subprocess.TimeoutExpired as error:
			try:
				self._raise_timeout(operation, deadline)
			except PublicationError as timeout_error:
				raise timeout_error from error
		except OSError as error:
			raise PublicationError(f"{operation}: command unavailable") from error

	def _run_bounded(
		self,
		command: list[str],
		limit: int,
		deadline: float | None,
		operation: str,
		environment: dict[str, str],
	) -> bytes:
		command_deadline = monotonic() + self._remaining_timeout(operation, deadline)
		session: _CommandSession | None = None
		threads: list[Thread] = []
		output = _CommandOutput()
		try:
			session = _spawn_bounded_command(command, environment)
			self._start_output_readers(session, output, limit, threads)
			self._release_command(session, output, operation, command_deadline, deadline)
			code = self._wait_for_command(session, operation, command_deadline, deadline)
		except BaseException as error:
			primary_error = self._cleanup_after_failure(
				error, operation, deadline, session, threads, output
			)
			if primary_error is not None:
				raise primary_error from error
			raise
		if session is None:
			raise PublicationError(f"{operation}: command was not started")
		return self._finish_command(session, threads, output, code, operation)

	@staticmethod
	def _start_output_readers(
		session: _CommandSession,
		output: _CommandOutput,
		limit: int,
		threads: list[Thread],
	) -> None:
		process = session.process
		if process.stdout is None or process.stderr is None:
			raise PublicationError("command has no readable output")
		readers = (
			Thread(
				target=BoundedCommand._capture_stream,
				args=(
					process.stdout,
					output.stdout,
					limit,
					"external command response exceeds its size limit",
					output,
					session.control,
					"stdout",
					session.startup_prefix,
				),
				daemon=True,
			),
			Thread(
				target=BoundedCommand._capture_stream,
				args=(
					process.stderr,
					output.stderr,
					MAX_COMMAND_STDERR_BYTES,
					"external command error output exceeds its size limit",
					output,
					session.control,
					"stderr",
					b"",
				),
				daemon=True,
			),
		)
		for reader in readers:
			try:
				reader.start()
			except RuntimeError as error:
				raise PublicationError("external command output reader could not start") from error
			threads.append(reader)

	def _release_command(
		self,
		session: _CommandSession,
		output: _CommandOutput,
		operation: str,
		command_deadline: float,
		aggregate_deadline: float | None,
	) -> None:
		if session.startup_prefix:
			self._await_helper_ready(
				session, output, operation, command_deadline, aggregate_deadline
			)
			helper_process_id = output.helper_process_id()
			if helper_process_id is None:
				raise PublicationError("command containment helper identity is unavailable")
			session.control.assign(helper_process_id)
		else:
			session.control.assign()
		self._time_left(operation, command_deadline, aggregate_deadline)
		session.control.release()

	def _wait_for_command(
		self,
		session: _CommandSession,
		operation: str,
		command_deadline: float,
		aggregate_deadline: float | None,
	) -> int:
		return session.process.wait(
			timeout=self._time_left(operation, command_deadline, aggregate_deadline)
		)

	def _finish_command(
		self,
		session: _CommandSession,
		threads: list[Thread],
		output: _CommandOutput,
		code: int,
		operation: str,
	) -> bytes:
		cleanup_failure = self._cleanup_session(session, threads)
		reader_failure = output.failure()
		if reader_failure is not None:
			primary_error = PublicationError(reader_failure)
			if cleanup_failure is not None:
				raise PublicationError(
					f"{primary_error}; command cleanup did not complete: {cleanup_failure}"
				) from primary_error
			raise primary_error
		if cleanup_failure is not None:
			raise PublicationError(
				f"{operation}: command cleanup did not complete: {cleanup_failure}"
			)
		if bytes(output.stderr).startswith(WINDOWS_HELPER_UNAVAILABLE):
			raise PublicationError(f"{operation}: command unavailable")
		if code:
			self._raise_command_error(bytes(output.stderr), operation, code)
		return bytes(output.stdout)

	def _cleanup_after_failure(
		self,
		error: BaseException,
		operation: str,
		deadline: float | None,
		session: _CommandSession | None,
		threads: list[Thread],
		output: _CommandOutput,
	) -> PublicationError | None:
		if session is None:
			return None
		reader_failures_before_cleanup = output.failures_snapshot()
		primary_error: BaseException = error
		if reader_failures_before_cleanup:
			primary_error = PublicationError(reader_failures_before_cleanup[0])
		elif isinstance(error, subprocess.TimeoutExpired):
			try:
				self._raise_timeout(operation, deadline)
			except PublicationError as timeout_error:
				primary_error = timeout_error

		cleanup_failure = self._cleanup_session(session, threads)
		reader_failures_after_cleanup = output.failures_snapshot()
		secondary_reader_failures = reader_failures_after_cleanup[
			len(reader_failures_before_cleanup) :
		]
		if (cleanup_failure is not None or secondary_reader_failures) and isinstance(
			primary_error, OSError
		):
			primary_error = PublicationError(f"{operation}: command unavailable")
		secondary_details = (
			[f"{operation}: command cleanup did not complete: {cleanup_failure}"]
			if cleanup_failure is not None
			else []
		)
		secondary_details.extend(
			f"{operation}: secondary output reader failure: {failure}"
			for failure in secondary_reader_failures
		)
		if secondary_details:
			return PublicationError(f"{primary_error}; {'; '.join(secondary_details)}")
		if primary_error is error:
			return None
		return primary_error if isinstance(primary_error, PublicationError) else None

	def _time_left(
		self,
		operation: str,
		command_deadline: float,
		aggregate_deadline: float | None,
	) -> float:
		remaining = command_deadline - monotonic()
		if aggregate_deadline is not None:
			remaining = min(remaining, aggregate_deadline - monotonic())
		if remaining <= 0:
			self._raise_timeout(operation, aggregate_deadline)
		return remaining

	@staticmethod
	def _capture_stream(
		stream: BinaryIO,
		collected: bytearray,
		limit: int,
		overflow_message: str,
		output: _CommandOutput,
		control: _CommandControl,
		name: str,
		startup_prefix: bytes,
	) -> None:
		try:
			if startup_prefix:
				output.ready(BoundedCommand._read_helper_identity(stream, startup_prefix))
			BoundedCommand._capture_response(stream, collected, limit, overflow_message)
		except Exception as error:
			detail = (
				str(error)
				if isinstance(error, PublicationError)
				else f"external command {name} could not be read"
			)
			output.fail(detail, control)
		finally:
			try:
				stream.close()
			except (OSError, ValueError):
				output.fail(f"external command {name} stream could not be closed", control)
			output.changed.set()

	@staticmethod
	def _read_helper_identity(stream: BinaryIO, startup_prefix: bytes) -> int:
		prefix = BoundedCommand._read_exact(
			stream,
			len(startup_prefix),
			"command containment helper exited before it was assigned",
		)
		if prefix != startup_prefix:
			raise PublicationError("command containment helper sent an invalid startup marker")
		process_id = BoundedCommand._read_exact(
			stream,
			WINDOWS_HELPER_PID_BYTES,
			"command containment helper omitted its process identity",
		)
		helper_process_id = int.from_bytes(process_id, "little")
		if helper_process_id == 0:
			raise PublicationError("command containment helper sent an invalid process identity")
		return helper_process_id

	@staticmethod
	def _read_exact(stream: BinaryIO, size: int, failure_message: str) -> bytes:
		content = bytearray()
		while len(content) < size:
			chunk = BoundedCommand._read_available(stream, size - len(content))
			if not chunk:
				raise PublicationError(failure_message)
			content.extend(chunk)
		return bytes(content)

	@staticmethod
	def _capture_response(
		stream: BinaryIO, collected: bytearray, limit: int, overflow_message: str
	) -> None:
		while chunk := BoundedCommand._read_available(
			stream, min(64 * 1024, limit - len(collected) + 1)
		):
			if len(collected) + len(chunk) > limit:
				raise PublicationError(overflow_message)
			collected.extend(chunk)

	@staticmethod
	def _read_available(stream: BinaryIO, size: int) -> bytes:
		read = getattr(stream, "read1", stream.read)
		return read(size)

	def _await_helper_ready(
		self,
		session: _CommandSession,
		output: _CommandOutput,
		operation: str,
		command_deadline: float,
		aggregate_deadline: float | None,
	) -> None:
		while True:
			output.changed.clear()
			self._raise_reader_failure(output)
			if output.startup_ready.is_set():
				return
			if session.process.poll() is not None:
				if bytes(output.stderr).startswith(WINDOWS_HELPER_UNAVAILABLE):
					raise PublicationError(f"{operation}: command unavailable")
				raise PublicationError(f"{operation}: command containment helper failed to start")
			output.changed.wait(
				min(0.05, self._time_left(operation, command_deadline, aggregate_deadline))
			)

	@staticmethod
	def _raise_reader_failure(output: _CommandOutput) -> None:
		failure = output.failure()
		if failure is not None:
			raise PublicationError(failure)

	@staticmethod
	def _cleanup_session(session: _CommandSession, threads: list[Thread]) -> str | None:
		"""Stop the process tree, reap its leader, and join readers within one grace period."""
		deadline = monotonic() + COMMAND_CLEANUP_GRACE_SECONDS
		failures: list[str] = []
		for failure in (
			BoundedCommand._terminate_process_tree(session),
			BoundedCommand._reap_process(session.process, deadline),
			BoundedCommand._join_readers(threads, deadline),
		):
			if failure is not None:
				failures.append(failure)
		return "; ".join(failures) or None

	@staticmethod
	def _terminate_process_tree(session: _CommandSession) -> str | None:
		try:
			session.control.close()
		except Exception:
			try:
				session.control.terminate()
			except Exception:
				return "process-tree termination and fallback failed"
			return "process-tree termination failed"
		return None

	@staticmethod
	def _reap_process(process: subprocess.Popen[bytes], deadline: float) -> str | None:
		try:
			process.wait(timeout=min(0.25, max(0.0, deadline - monotonic()) / 4))
		except subprocess.TimeoutExpired:
			return BoundedCommand._terminate_and_reap(process, deadline)
		except OSError:
			failure = BoundedCommand._terminate_process(process)
			if failure is not None:
				return f"command process could not be inspected or reaped; {failure}"
			return "command process could not be inspected or reaped"
		return None

	@staticmethod
	def _terminate_and_reap(process: subprocess.Popen[bytes], deadline: float) -> str | None:
		failure = BoundedCommand._terminate_process(process)
		if failure is not None:
			return failure
		try:
			process.wait(timeout=max(0.0, deadline - monotonic()))
		except subprocess.TimeoutExpired:
			return "command process did not exit after termination"
		except OSError:
			return "command process could not be reaped"
		return None

	@staticmethod
	def _terminate_process(process: subprocess.Popen[bytes]) -> str | None:
		try:
			process.kill()
		except OSError:
			try:
				if process.poll() is None:
					return "command process could not be terminated"
			except OSError:
				return "command process could not be inspected"
		return None

	@staticmethod
	def _join_readers(threads: list[Thread], deadline: float) -> str | None:
		for reader in threads:
			reader.join(timeout=max(0.0, deadline - monotonic()))
		if any(reader.is_alive() for reader in threads):
			return "command output reader did not stop"
		return None

	@staticmethod
	def _raise_command_error(detail: bytes, operation: str, code: int) -> None:
		status = _http_status(detail)
		if status == "404":
			raise MissingResourceError(f"{operation}: resource is absent (HTTP 404)")
		if status is None and GH_ESCAPE_SEQUENCE_ERROR in detail:
			raise EscapeSequenceError(
				f"{operation}: raw response contains terminal escape sequences"
			)
		status_detail = f", HTTP {status}" if status else ""
		raise PublicationError(f"{operation}: command failed (exit {code}{status_detail})")

	def _raise_timeout(self, operation: str, deadline: float | None) -> NoReturn:
		budget_seconds = self.aggregate_timeout_seconds
		if deadline is not None and budget_seconds is not None and deadline <= monotonic():
			raise PublicationError(
				f"{operation}: shared image-command budget of "
				f"{budget_seconds:g} seconds is exhausted"
			)
		raise PublicationError(f"{operation}: timed out after {self.timeout_seconds:g} seconds")


class GitHubCLI:
	"""GitHub.com repository API plus content-addressed registry readback."""

	def __init__(
		self,
		repository: str,
		*,
		timeout_seconds: float,
		command: Command | None = None,
		image_command: Command | None = None,
	) -> None:
		if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
			raise PublicationError("invalid GitHub repository")
		self.repository = repository
		self.command = command or BoundedCommand(timeout_seconds)
		self.image_command = image_command or self.command

	def _arguments(
		self, path: str, method: str, *, allow_escape_sequences: bool = False
	) -> list[str]:
		if not path.startswith("/") or ".." in path or "\\" in path:
			raise PublicationError("invalid repository API path")
		endpoint = f"repos/{self.repository}" + ("" if path == "/" else path)
		arguments = [
			"gh",
			"api",
			endpoint,
			"--method",
			method,
			"--header",
			"Accept: application/vnd.github+json",
			"--header",
			"X-GitHub-Api-Version: 2022-11-28",
		]
		if allow_escape_sequences:
			arguments.append("--allow-escape-sequences")
		return arguments

	def _json(
		self,
		path: str,
		method: str,
		payload: Mapping[str, object] | None = None,
	) -> object:
		content = json.dumps(payload).encode("utf-8") if payload is not None else None
		raw = self.command(self._arguments(path, method), content=content, limit=JSON_LIMIT)
		try:
			value = json.loads(raw)
		except (UnicodeError, json.JSONDecodeError) as error:
			raise PublicationError("GitHub returned invalid JSON") from error
		if isinstance(value, dict) and "message" in value:
			raise PublicationError("GitHub returned an API error object")
		return value

	def get(self, path: str) -> object:
		try:
			return self._json(path, "GET")
		except MissingResourceError:
			return None

	def source(self, sha: str, path: str) -> str:
		if (
			re.fullmatch(r"[0-9a-f]{40}", sha) is None
			or PurePosixPath(path).is_absolute()
			or ".." in PurePosixPath(path).parts
			or re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None
		):
			raise PublicationError("invalid exact-source path or revision")
		response = record(self.get(f"/contents/{path}?ref={sha}"), "source content")
		if response.get("encoding") != "base64" or not isinstance(response.get("content"), str):
			raise PublicationError("source content is missing or too large for the contents API")
		try:
			content = base64.b64decode(str(response["content"]).replace("\n", ""), validate=True)
			if len(content) > 1024 * 1024:
				raise PublicationError("source content exceeds 1 MiB")
			return content.decode("utf-8")
		except (ValueError, UnicodeError) as error:
			raise PublicationError("source content encoding is invalid") from error

	def post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		try:
			return record(self._json(path, "POST", payload), "created resource")
		except PublicationError as error:
			if path == "/git/refs" and "HTTP 403" in str(error):
				raise PublicationError(
					"GitHub denied creation of the release tag (HTTP 403). "
					"If this is the historical Workflows: write restriction, "
					"keep the original candidate and its source SHA. "
					"If v<version> is absent, an authorized maintainer may "
					"create and push only that exact tag with an already-"
					"authenticated Git client, then rerun only the failed "
					"Publish release job in this original workflow run. "
					"This reuses the successful producer and image-writer "
					"results; do not dispatch a new run, rerun all jobs, or "
					"repush the image. Stop and ask the owner for manual "
					"recovery if any other job failed, a required producer "
					"or writer artifact or upload log is missing or expired, "
					"the publisher-only rerun is unavailable, or the tag "
					"points elsewhere. Do not change "
					"the source or expand GITHUB_TOKEN permissions."
				) from error
			raise

	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		return record(self._json(path, "PATCH", payload), "updated resource")

	def download(self, path: str) -> bytes:
		limit = JSON_LIMIT if path.endswith("/logs") else MAX_BUNDLE_BYTES
		arguments = self._arguments(path, "GET")
		try:
			return self.command(arguments, limit=limit)
		except EscapeSequenceError:
			if re.fullmatch(r"/actions/jobs/[0-9]+/logs", path) is None:
				raise
			return self.command(
				self._arguments(path, "GET", allow_escape_sequences=True), limit=limit
			)

	def upload(self, release_id: int, name: str, content: bytes) -> dict[str, object]:
		positive(release_id, "release ID")
		endpoint = (
			f"https://uploads.github.com/repos/{self.repository}/releases/{release_id}"
			f"/assets?name={quote(name, safe='')}"
		)
		raw = self.command(
			[
				"gh",
				"api",
				endpoint,
				"--method",
				"POST",
				"--header",
				"Content-Type: application/octet-stream",
			],
			content=content,
			limit=JSON_LIMIT,
		)
		return json_record(raw)

	def image_digest(self, reference: str) -> str:
		if re.fullmatch(r"[a-z0-9][a-z0-9._:/-]+@sha256:[0-9a-f]{64}", reference) is None:
			raise PublicationError("image reference must be a registry path pinned by SHA-256")
		content = self.image_command(
			["docker", "buildx", "imagetools", "inspect", reference, "--raw"], limit=JSON_LIMIT
		)
		return hashlib.sha256(content).hexdigest()

	def preflight_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> None:
		"""Check one configured image tag before any candidate image tag is pushed."""
		if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", producer) is None:
			raise PublicationError("invalid image producer name")
		package = image_repository(repository)
		preflight_image_archive(
			content,
			source,
			f"{package}:v{version}",
			self.image_command,
			expected_digest=expected_digest,
		)

	def publish_image_archive(
		self,
		content: bytes,
		source: str,
		repository: str,
		version: str,
		producer: str,
		*,
		expected_digest: str | None = None,
	) -> dict[str, str]:
		"""Push one source-verified image archive and return its immutable GHCR identity."""
		if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", producer) is None:
			raise PublicationError("invalid image producer name")
		package = image_repository(repository)
		target = f"{package}:v{version}"
		registry_digest = push_image_archive(
			content,
			source,
			target,
			self.image_command,
			expected_digest=expected_digest,
		)
		return {
			"reference": f"{package}@sha256:{registry_digest}",
			"sha256": registry_digest,
			"tag": target,
		}
