"""Run external publisher commands within time and output bounds."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from typing import BinaryIO, NoReturn

from .publication import (
	MAX_VERSION_LENGTH,
	VERSION,
	MissingResourceError,
	PublicationError,
	image_repository,
)
from .publication_command_policy import _http_status, _operation
from .publication_process import (
	_CommandControl,
	_CommandOutput,
	_CommandSession,
	_spawn_bounded_command,
)

MAX_TIMEOUT_SECONDS = 300
MAX_IMAGE_WRITER_TIMEOUT_SECONDS = 25 * 60
MAX_COMMAND_STDERR_BYTES = 4096
COMMAND_CLEANUP_GRACE_SECONDS = 2
WINDOWS_HELPER_PID_BYTES = 4
WINDOWS_HELPER_UNAVAILABLE = b"\x00quality-gate-command-unavailable-v1\n"
GH_ESCAPE_SEQUENCE_ERROR = (
	b"the response contains terminal escape sequences; pass --allow-escape-sequences"
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
		return self._call(arguments, content=content, limit=limit)

	def inspect_image_manifest(self, target: str, *, allow_missing: bool, limit: int) -> bytes:
		"""Inspect one version tag, optionally identifying its exact absence at preflight."""
		arguments = ["docker", "buildx", "imagetools", "inspect", target, "--raw"]
		expected_missing_target = target if allow_missing else None
		return self._call(
			arguments,
			content=None,
			limit=limit,
			expected_missing_target=expected_missing_target,
		)

	def _call(
		self,
		arguments: Sequence[str],
		*,
		content: bytes | None,
		limit: int,
		expected_missing_target: str | None = None,
	) -> bytes:
		deadline = self._begin_aggregate_budget()
		with tempfile.TemporaryDirectory(prefix="publisher-") as directory:
			command = list(arguments)
			if content is not None:
				input_file = Path(directory) / "input"
				input_file.write_bytes(content)
				command.extend(["--input", str(input_file)])
			return self._execute(command, limit, deadline, expected_missing_target)

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

	def _execute(
		self,
		command: list[str],
		limit: int,
		deadline: float | None,
		expected_missing_target: str | None,
	) -> bytes:
		operation = _operation(command)
		if expected_missing_target is not None:
			_validate_expected_missing_target(command, expected_missing_target)
		environment = os.environ.copy()
		environment.update(self.environment)
		try:
			self._remaining_timeout(operation, deadline)
			return self._run_bounded(
				command,
				limit,
				deadline,
				operation,
				environment,
				expected_missing_target,
			)
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
		expected_missing_target: str | None,
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
		return self._finish_command(
			session, threads, output, code, operation, command, expected_missing_target
		)

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
		command: Sequence[str],
		expected_missing_target: str | None,
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
			self._raise_command_error(
				bytes(output.stderr), operation, code, command, expected_missing_target
			)
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
	def _raise_command_error(
		detail: bytes,
		operation: str,
		code: int,
		command: Sequence[str],
		expected_missing_target: str | None,
	) -> None:
		status = _http_status(detail)
		is_docker = bool(command and command[0] == "docker")
		if (
			is_docker
			and code == 1
			and expected_missing_target is not None
			and _exact_missing_manifest_response(detail, expected_missing_target)
		):
			raise MissingResourceError(f"{operation}: image target is absent")
		if not is_docker and status == "404":
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


def _validate_expected_missing_target(command: Sequence[str], target: str) -> None:
	if command != ["docker", "buildx", "imagetools", "inspect", target, "--raw"]:
		raise PublicationError("expected missing target is not an exact GHCR manifest inspection")
	repository, separator, tag = target.rpartition(":")
	try:
		image_repository(repository)
	except PublicationError as error:
		raise PublicationError(
			"expected missing target is not a version-tagged GHCR reference"
		) from error
	if (
		not separator
		or not tag.startswith("v")
		or len(tag[1:]) > MAX_VERSION_LENGTH
		or VERSION.fullmatch(tag[1:]) is None
	):
		raise PublicationError("expected missing target is not a version-tagged GHCR reference")


def _exact_missing_manifest_response(detail: bytes, target: str) -> bool:
	try:
		text = detail.decode("ascii")
	except UnicodeDecodeError:
		return False
	if text.endswith("\r\n"):
		text = text[:-2]
	elif text.endswith("\n"):
		text = text[:-1]
	if "\r" in text or "\n" in text:
		return False
	if text.startswith("ERROR: "):
		text = text.removeprefix("ERROR: ")
	return text in {
		f"{target}: manifest unknown",
		f"{target}: manifest_unknown",
		f"{target}: no such manifest",
		f"no such manifest: {target}",
		f"{target}: not found",
	}
