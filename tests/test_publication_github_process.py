"""Related behavior tests."""

from __future__ import annotations

import ctypes
import io
import subprocess
import sys
from pathlib import Path

import pytest
from test_publication_github import (
	WINDOWS_MODULE_PATH_BUFFER_CHARS,
)

from quality_gate import publication_command, publication_github, publication_process
from quality_gate.publication import PublicationError
from quality_gate.publication_github import (
	BoundedCommand,
	EscapeSequenceError,
	MissingResourceError,
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object containment")
def test_windows_job_assignment_failure_does_not_launch_target(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	marker = tmp_path / "uncontained-command-ran"
	program = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"

	def fail_assignment(_job: object, _process_id: int) -> None:
		raise OSError("simulated Job Object assignment failure")

	monkeypatch.setattr(publication_process._WindowsJob, "assign", fail_assignment)
	with pytest.raises(PublicationError):
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)

	assert not marker.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object containment")
def test_windows_job_contains_the_helper_that_launches_the_target(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	assigned_process_ids: list[int] = []
	original_assign = publication_process._WindowsJob.assign

	def record_assignment(job: object, process_id: int) -> None:
		original_assign(job, process_id)  # type: ignore[arg-type]
		assigned_process_ids.append(process_id)

	monkeypatch.setattr(publication_process._WindowsJob, "assign", record_assignment)
	from ctypes import wintypes

	kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
	get_module_file_name = kernel32.GetModuleFileNameW
	get_module_file_name.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
	get_module_file_name.restype = wintypes.DWORD
	executable_buffer = ctypes.create_unicode_buffer(WINDOWS_MODULE_PATH_BUFFER_CHARS)
	path_length = get_module_file_name(None, executable_buffer, len(executable_buffer))
	if path_length == 0:
		error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
		raise OSError(error_code, "GetModuleFileNameW failed")
	if path_length >= len(executable_buffer):
		raise OSError("running Python executable path exceeds the Windows path limit")
	current_executable = Path(executable_buffer.value)
	output = BoundedCommand(5)(
		[str(current_executable), "-B", "-c", "import os; print(os.getppid())"], limit=64
	)

	assert len(assigned_process_ids) == 1
	assert output.splitlines() == [str(assigned_process_ids[0]).encode("ascii")]


def test_timeout_stays_primary_when_reader_fails_during_cleanup(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	output = publication_process._CommandOutput()

	class FakeControl:
		@staticmethod
		def terminate() -> None:
			pass

	class FakeProcess:
		pid = 1
		stdin = None

	control = FakeControl()
	session = publication_process._CommandSession(  # type: ignore[arg-type]
		FakeProcess(), control
	)

	def fail_reader_during_cleanup(
		_session: publication_process._CommandSession, _threads: list[object]
	) -> None:
		output.fail("external command stdout stream could not be closed", control)  # type: ignore[arg-type]
		return None

	monkeypatch.setattr(
		BoundedCommand,
		"_cleanup_session",
		staticmethod(fail_reader_during_cleanup),
	)
	error = BoundedCommand(0.5)._cleanup_after_failure(
		subprocess.TimeoutExpired("command", 0.5),
		"external command",
		None,
		session,
		[],
		output,
	)

	assert error is not None
	assert "external command: timed out after 0.5 seconds" in str(error)
	assert "secondary output reader failure" in str(error)


def test_target_exit_125_is_not_reported_as_helper_failure() -> None:
	with pytest.raises(PublicationError, match=r"command failed \(exit 125\)"):
		BoundedCommand(5)([sys.executable, "-B", "-c", "raise SystemExit(125)"], limit=64)


def test_posix_process_group_kill_failure_is_not_ignored_after_leader_exit(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class ExitedProcess:
		pid = 17
		stdin = None

		@staticmethod
		def poll() -> int:
			return 0

	def fail_killpg(_pid: int, _signal: int) -> None:
		raise PermissionError("simulated process-group kill failure")

	monkeypatch.setattr(publication_github.os, "name", "posix")
	monkeypatch.setattr(publication_github.os, "killpg", fail_killpg, raising=False)
	monkeypatch.setattr(publication_process.signal, "SIGKILL", 9, raising=False)
	control = publication_process._CommandControl(ExitedProcess())  # type: ignore[arg-type]

	with pytest.raises(PermissionError, match="process-group kill failure"):
		control.close()


def test_incomplete_process_cleanup_is_reported(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class UnreapedProcess:
		pid = 18
		stdin = None

		def __init__(self) -> None:
			self.stdout = io.BytesIO()
			self.stderr = io.BytesIO()

		@staticmethod
		def wait(*, timeout: float) -> int:
			raise subprocess.TimeoutExpired("command", timeout)

		@staticmethod
		def poll() -> None:
			return None

		@staticmethod
		def kill() -> None:
			pass

	class FailedControl:
		@staticmethod
		def assign() -> None:
			pass

		@staticmethod
		def release() -> None:
			pass

		@staticmethod
		def close() -> None:
			raise OSError("simulated process-tree cleanup failure")

		@staticmethod
		def terminate() -> None:
			raise OSError("simulated process-tree cleanup failure")

	monkeypatch.setattr(
		publication_command,
		"_spawn_bounded_command",
		lambda _arguments, _environment: publication_process._CommandSession(
			UnreapedProcess(),
			FailedControl(),  # type: ignore[arg-type]
		),
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(0.5)(["command"], limit=64)
	assert "timed out after 0.5 seconds" in str(error.value)
	assert "command cleanup did not complete" in str(error.value)


def test_output_limit_error_survives_cleanup_failure(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class CompletedProcess:
		pid = 19
		stdin = None

		def __init__(self) -> None:
			self.stdout = io.BytesIO(b"x" * 65)
			self.stderr = io.BytesIO()

		@staticmethod
		def wait(*, timeout: float) -> int:
			return 0

		@staticmethod
		def poll() -> int:
			return 0

		@staticmethod
		def kill() -> None:
			pass

	class FailingControl:
		@staticmethod
		def assign() -> None:
			pass

		@staticmethod
		def release() -> None:
			pass

		@staticmethod
		def close() -> None:
			raise OSError("simulated process-tree cleanup failure")

		@staticmethod
		def terminate() -> None:
			pass

	monkeypatch.setattr(
		publication_command,
		"_spawn_bounded_command",
		lambda _arguments, _environment: publication_process._CommandSession(
			CompletedProcess(),
			FailingControl(),  # type: ignore[arg-type]
		),
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)(["command"], limit=64)
	assert str(error.value).startswith("external command response exceeds its size limit")
	assert "command cleanup did not complete" in str(error.value)


def test_command_reader_errors_trigger_teardown() -> None:
	class BrokenStream:
		@staticmethod
		def read(_size: int) -> bytes:
			raise OSError("simulated output read failure")

		@staticmethod
		def close() -> None:
			pass

	class Control:
		terminated = False

		def terminate(self) -> None:
			self.terminated = True

	output = publication_process._CommandOutput()
	control = Control()
	BoundedCommand._capture_stream(
		BrokenStream(),
		bytearray(),
		64,
		"size limit",
		output,
		control,
		"stdout",
		b"",  # type: ignore[arg-type]
	)

	assert control.terminated
	assert output.failure() == "external command stdout could not be read"


def test_external_command_diagnostics_expose_stage_status_without_stderr() -> None:
	program = (
		"import sys; sys.stderr.write('Authorization: bearer secret-token HTTP 403\\n'); "
		"sys.exit(7)"
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	message = str(error.value)
	assert message == "external command: command failed (exit 7, HTTP 403)"
	assert "secret-token" not in message
	assert "Authorization" not in message


def test_external_command_404_remains_optional_without_stderr() -> None:
	program = "import sys; sys.stderr.write('HTTP 404 secret-token\\n'); sys.exit(1)"
	with pytest.raises(MissingResourceError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	message = str(error.value)
	assert message == "external command: resource is absent (HTTP 404)"
	assert "secret-token" not in message


def test_registry_missing_tag_status_is_distinguished_without_diagnostics() -> None:
	program = (
		"import sys; sys.stderr.write("
		"'response status code 404: Not Found secret-token\\n'); sys.exit(1)"
	)
	with pytest.raises(MissingResourceError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	assert "secret-token" not in str(error.value)


def test_external_command_marks_gh_escape_guard_without_stderr() -> None:
	program = (
		"import sys; sys.stderr.write("
		"'the response contains terminal escape sequences; pass --allow-escape-sequences "
		"secret-token\\n'); sys.exit(1)"
	)
	with pytest.raises(EscapeSequenceError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	message = str(error.value)
	assert message == "external command: raw response contains terminal escape sequences"
	assert "secret-token" not in message


@pytest.mark.parametrize(
	"stderr",
	[
		"HTTP 403; pass --allow-escape-sequences secret-token\\n",
		"unknown flag: --allow-escape-sequences\\n",
	],
)
def test_external_command_does_not_trust_an_unrelated_escape_flag_hint(stderr: str) -> None:
	program = f"import sys; sys.stderr.write({stderr!r}); sys.exit(1)"
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	assert not isinstance(error.value, EscapeSequenceError)


def test_external_command_preserves_http_status_with_escape_text() -> None:
	program = (
		"import sys; sys.stderr.write('HTTP 403: the response contains terminal escape sequences; "
		"pass --allow-escape-sequences secret-token\\n'); sys.exit(1)"
	)
	with pytest.raises(PublicationError) as error:
		BoundedCommand(5)([sys.executable, "-B", "-c", program], limit=64)
	assert not isinstance(error.value, EscapeSequenceError)
	assert str(error.value) == "external command: command failed (exit 1, HTTP 403)"


def test_external_command_timeout_is_distinguished_from_exit_failure() -> None:
	with pytest.raises(PublicationError, match=r"^external command: timed out after"):
		BoundedCommand(0.2)([sys.executable, "-B", "-c", "import time; time.sleep(30)"], limit=64)


def test_missing_executable_is_distinguished_from_timeout(tmp_path: Path) -> None:
	missing = tmp_path / "missing-command"
	with pytest.raises(PublicationError, match=r"^external command: command unavailable$") as error:
		BoundedCommand(5)([str(missing)], limit=64)
	assert str(missing) not in str(error.value)
