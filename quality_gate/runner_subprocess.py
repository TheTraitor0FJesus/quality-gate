"""Execute checks with bounded subprocess output."""

from __future__ import annotations

import locale
import subprocess
import threading
from pathlib import Path

from .contracts import redact
from .runner_errors import QualityGateError

EXIT_UNCHECKED = 2
MAX_COMMAND_OUTPUT_CHARS = 16_384
MAX_COMMAND_OUTPUT_BYTES = MAX_COMMAND_OUTPUT_CHARS * 4 + 1
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0


class OutputReadError(RuntimeError):
	"""A subprocess output reader could not finish safely."""


def decode_subprocess_output(output: bytes | str | None) -> str:
	"""Decode subprocess output across UTF-8 and Windows console encodings."""
	if output is None:
		return ""
	if isinstance(output, str):
		return output
	encodings = ["utf-8", locale.getpreferredencoding(False)]
	for encoding in dict.fromkeys(encodings):
		try:
			return output.decode(encoding)
		except UnicodeDecodeError:
			continue
	return output.decode("utf-8", errors="replace")


def _run_bounded_subprocess(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	timeout: float | None,
) -> tuple[int, str]:
	"""Run a subprocess while retaining only a bounded amount of output."""
	process = subprocess.Popen(
		command,
		cwd=root,
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
	)
	retained = bytearray()
	reader_errors: list[Exception] = []
	stdout = process.stdout
	if stdout is None:
		raise OutputReadError("subprocess output pipe is unavailable")

	def drain_output() -> None:
		try:
			while True:
				chunk = stdout.read(4096)
				if not chunk:
					return
				remaining = MAX_COMMAND_OUTPUT_BYTES - len(retained)
				if remaining > 0:
					retained.extend(chunk[:remaining])
		except (OSError, ValueError) as error:
			reader_errors.append(error)

	reader = threading.Thread(target=drain_output, daemon=True)
	reader.start()
	try:
		returncode = process.wait(timeout=timeout)
	except subprocess.TimeoutExpired:
		process.kill()
		process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
		reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
		raise
	reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader.is_alive():
		raise subprocess.TimeoutExpired(command, PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader_errors:
		raise OutputReadError("subprocess output could not be read") from reader_errors[0]
	return returncode, decode_subprocess_output(bytes(retained))


def run(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	*,
	timeout: float | None = None,
) -> str:
	"""Run one quality command and return its decoded standard output."""
	try:
		returncode, output = _run_bounded_subprocess(command, root, environment, timeout)
	except OSError as error:
		raise QualityGateError(
			f"{' '.join(command)} could not be executed: {error}",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the required tool or runtime and retry the quality gate",
		) from error
	except subprocess.TimeoutExpired as error:
		raise QualityGateError(
			f"{' '.join(command)} timed out after {timeout:g} seconds",
			check_id="runtime.timeout",
			exit_code=2,
			recovery_action="inspect the command and retry within the declared time budget",
		) from error
	except OutputReadError as error:
		raise QualityGateError(
			f"{' '.join(command)} output could not be read",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore subprocess output handling and retry the quality gate",
		) from error
	if returncode:
		detail = output.strip()
		detail = _bounded_output(detail)
		if not detail:
			detail = f"Command exited with {returncode}."
		lower_detail = detail.casefold()
		missing_tool = any(
			f"no module named {tool_name}" in lower_detail
			for tool_name in ("ruff", "mypy", "pytest", "biome")
		)
		pytest_collection_error = "pytest" in " ".join(command).casefold() and any(
			marker in lower_detail
			for marker in ("error collecting", "no tests collected", "no tests ran")
		)
		raise QualityGateError(
			f"{' '.join(command)}\n\n{redact(detail)}",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED if missing_tool or pytest_collection_error else 1,
			recovery_action=(
				(
					"restore the required tool in the verification runtime "
					"and retry the quality gate"
					if missing_tool
					else "restore a collectable pytest suite and retry the quality gate"
				)
				if missing_tool or pytest_collection_error
				else "fix the reported quality finding and retry the quality gate"
			),
		)
	return _bounded_output(output).strip()


def _bounded_output(value: str) -> str:
	"""Limit external command output retained by the gate."""
	if len(value) <= MAX_COMMAND_OUTPUT_CHARS:
		return value
	return value[:MAX_COMMAND_OUTPUT_CHARS] + "\n[output truncated]"
