"""Shared fixtures and helpers for related tests."""

from __future__ import annotations

import io
import subprocess
import sys
from collections.abc import Sequence

import pytest

from quality_gate import publication_command, publication_process
from quality_gate.publication_github import BoundedCommand

WINDOWS_MODULE_PATH_BUFFER_CHARS = 32768


class Command:
	def __init__(self, responses: list[bytes]) -> None:
		self.responses = responses
		self.calls: list[list[str]] = []

	def __call__(
		self, arguments: Sequence[str], *, content: bytes | None = None, limit: int
	) -> bytes:
		self.calls.append(list(arguments))
		return self.responses.pop(0)


class _FakeControl:
	def assign(self) -> None:
		pass

	def release(self) -> None:
		pass

	def terminate(self) -> None:
		pass

	def close(self) -> None:
		pass


def _clocked_bounded_command(
	monkeypatch: pytest.MonkeyPatch,
	*,
	launch_seconds: list[float],
	wait_seconds: list[float],
	exit_codes: list[int] | None = None,
) -> tuple[BoundedCommand, dict[str, list[object]], list[float]]:
	now = [0.0]
	monitor: dict[str, list[object]] = {
		"calls": [],
		"waits": [],
	}
	codes = exit_codes or [0] * len(launch_seconds)

	class FakeProcess:
		def __init__(self, arguments: list[str]) -> None:
			self.index = len(monitor["calls"])
			self.pid = self.index + 1000
			monitor["calls"].append(arguments)
			now[0] += launch_seconds[self.index]
			self.stdin = None
			self.stdout = io.BytesIO()
			self.stderr = io.BytesIO(b"HTTP 404\n" if codes[self.index] else b"")
			self.code = codes[self.index]
			self.waited = False

		def wait(self, *, timeout: float) -> int:
			if self.waited:
				return self.code
			monitor["waits"].append(timeout)
			if wait_seconds[self.index] > timeout:
				now[0] += timeout
				raise subprocess.TimeoutExpired("command", timeout)
			now[0] += wait_seconds[self.index]
			self.waited = True
			return self.code

		def poll(self) -> int | None:
			return self.code if self.waited else None

		def kill(self) -> None:
			self.waited = True
			self.code = -9

	monkeypatch.setattr(publication_command, "monotonic", lambda: now[0])
	monkeypatch.setattr(
		publication_command,
		"_spawn_bounded_command",
		lambda arguments, _environment: publication_process._CommandSession(
			FakeProcess(arguments),
			_FakeControl(),  # type: ignore[arg-type]
		),
	)
	return BoundedCommand(10, aggregate_timeout_seconds=10), monitor, now


def _controlled_exit_command(
	monkeypatch: pytest.MonkeyPatch, *, detail: bytes, exit_code: int = 1
) -> list[list[str]]:
	original_spawn = publication_command._spawn_bounded_command
	calls: list[list[str]] = []

	def spawn(
		arguments: list[str], environment: dict[str, str]
	) -> publication_process._CommandSession:
		calls.append(arguments)
		program = f"import sys; sys.stderr.buffer.write({detail!r}); sys.exit({exit_code})"
		return original_spawn([sys.executable, "-B", "-c", program], environment)

	monkeypatch.setattr(publication_command, "_spawn_bounded_command", spawn)
	return calls
