"""Public JSON transport for bounded working-tree edit commands."""

from __future__ import annotations

import json
import os
import queue
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .edit_process import run_process
from .edit_protocol import (
	BACKGROUND_SCHEMA,
	DELIVERY_RESERVE_SECONDS,
	EXIT_UNCHECKED,
	MAX_DEADLINE_MS,
	MAX_REQUEST_BYTES,
	MAX_RESULT_BYTES,
	EditError,
	background_identity,
	decode_request,
	encode,
	unchecked,
)


def _request() -> bytes:
	intake: queue.Queue[bytes | Exception] = queue.Queue(maxsize=1)

	def read() -> None:
		try:
			intake.put(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
		except OSError as error:
			intake.put(error)

	threading.Thread(target=read, daemon=True).start()
	try:
		payload = intake.get(timeout=MAX_DEADLINE_MS / 1000)
	except queue.Empty as error:
		failure = EditError("deadline")
		failure.stage = "intake"
		raise failure from error
	if isinstance(payload, Exception):
		raise EditError("invalid_request")
	return payload


def _deliver(output: bytes, deadline: float) -> None:
	completed: list[bool] = []

	def write() -> None:
		try:
			sys.stdout.buffer.write(output)
			sys.stdout.buffer.flush()
			completed.append(True)
		except (OSError, ValueError):
			completed.append(False)

	writer = threading.Thread(target=write, daemon=True)
	writer.start()
	writer.join(timeout=max(0, deadline - time.monotonic()))
	if writer.is_alive() or completed != [True]:
		# The transport cannot accept a complete response. Avoid interpreter flush
		# waiting forever on the same blocked pipe after the bounded worker is gone.
		os._exit(EXIT_UNCHECKED)


def _failure(reason: str, *, secrets: bool, types: bool = False) -> dict[str, Any]:
	if types:
		from .edit_types import typing_unchecked

		return typing_unchecked(reason)
	if secrets:
		from .edit_secrets import secret_unchecked

		return secret_unchecked(reason)
	return unchecked(reason)


def _validate_result(
	output: bytes, stderr: bytes, code: int, *, secrets: bool, schema: int = 1
) -> dict[str, Any]:
	try:
		result = json.loads(output)
		statuses = {"passed": 0, "failed": 1, "not_applicable": 0, "unchecked": 2}
		if secrets:
			statuses["waived"] = 0
		if not isinstance(result, dict):
			raise EditError("tool_output")
		status = result.get("status")
		if (
			stderr
			or not isinstance(status, str)
			or result.get("schema") != schema
			or statuses.get(status) != code
			or not isinstance(result.get("files"), list)
		):
			raise EditError("tool_output")
		return result
	except (ValueError, TypeError) as error:
		raise EditError("tool_output") from error


def main(*, secrets: bool = False, types: bool = False) -> int:
	"""Read one bounded request and emit exactly one source-free JSON result."""
	started = time.monotonic()
	delivery_deadline = started + MAX_DEADLINE_MS / 1000
	request = None
	background: dict[str, Any] = {}
	try:
		payload = _request()
		request = decode_request(payload, secrets=secrets, types=types)
		delivery_deadline = time.monotonic() + request["deadline_unix_ms"] / 1000 - time.time()
		deadline = (
			time.monotonic()
			+ request["deadline_unix_ms"] / 1000
			- time.time()
			- DELIVERY_RESERVE_SECONDS
		)
		result = _run_worker(request, payload, deadline, secrets=secrets, types=types)
	except EditError as error:
		result = _failure(error.reason, secrets=secrets, types=types)
		background = error.background
		if not types:
			result["stage"] = error.stage
	except OSError:
		result = _failure("io", secrets=secrets, types=types)
	if types:
		result.update(background_identity(request) | background)
	result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
	try:
		output = encode(result)
	except EditError:
		result = _failure("limits", secrets=secrets, types=types)
		if types:
			result.update(background_identity(request) | background)
		result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
		output = encode(result)
	_deliver(output, delivery_deadline)
	if result.get("stage") == "intake":
		os._exit(EXIT_UNCHECKED)
	return {"unchecked": 2, "failed": 1}.get(result["status"], 0)


def _run_worker(
	request: dict[str, Any], payload: bytes, deadline: float, *, secrets: bool, types: bool
) -> dict[str, Any]:
	package_root = Path(__file__).resolve().parent.parent
	# CPython's Windows venv redirector can spawn before job assignment. Launch
	# the base interpreter directly; the exact engine package root stays explicit.
	worker_python = (
		Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
	)
	root = Path(request["root"])
	if (
		not root.is_absolute()
		or not root.is_dir()
		or getattr(root.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
		or root.is_symlink()
	):
		raise EditError("unsafe_path")
	cancellation = None
	if types and request["schema"] == BACKGROUND_SCHEMA:
		from .edit_type_inputs import CancellationSignal

		cancellation = CancellationSignal(request)
	with tempfile.TemporaryDirectory(prefix=".quality-gate-edit-", dir=root) as directory:
		entry = (
			f"import sys; sys.path.insert(0, {str(package_root)!r}); "
			"from pathlib import Path; from quality_gate.edit_worker import main; "
			f"main(Path({str(Path(directory).resolve())!r}), "
			f"secrets={secrets!r}, types={types!r})"
		)
		code, output, stderr = run_process(
			[str(worker_python), "-I", "-S", "-B", "-c", entry],
			root=Path(directory).resolve(),
			environment={
				key: value
				for key, value in os.environ.items()
				if key.upper()
				in {
					"PATH",
					"SYSTEMROOT",
					"WINDIR",
					"TEMP",
					"TMP",
					"LOCALAPPDATA",
					"XDG_CACHE_HOME",
					"HOME",
					"USERPROFILE",
				}
			},
			deadline=deadline,
			payload=payload,
			limit=MAX_RESULT_BYTES,
			own_tree=True,
			cancelled=cancellation,
		)
	result = _validate_result(output, stderr, code, secrets=secrets, schema=request["schema"])
	if cancellation is not None and cancellation():
		raise EditError("cancelled")
	if time.monotonic() >= deadline:
		raise EditError("deadline")
	return result


if __name__ == "__main__":
	raise SystemExit(main())
