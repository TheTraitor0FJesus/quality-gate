"""Public JSON transport for bounded working-tree edit commands."""

from __future__ import annotations

import json
import os
import queue
import re
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


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _is_digest(value: object) -> bool:
	return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _validate_read_only_phase(result: dict[str, Any], status: str) -> None:
	if result.get("phase") != "validate" or result.get("writes_may_have_occurred") is not False:
		raise EditError("tool_output")
	if status != "unchecked" and any(
		not isinstance(item, dict)
		or item.get("changed") is not False
		or item.get("sha256") != item.get("input_sha256")
		for item in result["files"]
	):
		raise EditError("tool_output")


def _validate_write_file(item: object) -> None:
	if not isinstance(item, dict):
		raise EditError("tool_output")
	if (
		item.get("status") not in {"passed", "not_applicable"}
		or (item.get("status") == "not_applicable" and item.get("changed") is not False)
		or item.get("findings") != []
		or item.get("format") not in {"passed", "not_applicable"}
	):
		raise EditError("tool_output")
	if (
		type(item.get("changed")) is not bool
		or not _is_digest(item.get("input_sha256"))
		or not _is_digest(item.get("sha256"))
		or item["changed"] != (item["input_sha256"] != item["sha256"])
	):
		raise EditError("tool_output")


def _validate_write_files(result: dict[str, Any]) -> None:
	for item in result["files"]:
		_validate_write_file(item)
	if result["writes_may_have_occurred"] != any(item["changed"] for item in result["files"]):
		raise EditError("tool_output")


def _validate_write_phase(result: dict[str, Any], status: str) -> None:
	if (
		result.get("phase") != "write"
		or type(result.get("writes_may_have_occurred")) is not bool
		or status == "failed"
	):
		raise EditError("tool_output")
	if status != "unchecked":
		_validate_write_files(result)


def _failure(
	reason: str, *, secrets: bool, types: bool = False, integrity: bool = False
) -> dict[str, Any]:
	if integrity:
		from .edit_integrity import integrity_unchecked

		return integrity_unchecked(reason)
	if types:
		from .edit_types import typing_unchecked

		return typing_unchecked(reason)
	if secrets:
		from .edit_secrets import secret_unchecked

		return secret_unchecked(reason)
	return unchecked(reason)


def _apply_phase_metadata(result: dict[str, Any], request: dict[str, Any] | None) -> None:
	if request is None:
		return
	phase = request.get("phase")
	if phase == "validate":
		result.update(phase="validate", writes_may_have_occurred=False)
	elif phase == "write":
		result["phase"] = "write"


def _validate_result(
	output: bytes,
	stderr: bytes,
	code: int,
	*,
	secrets: bool,
	schema: int = 1,
	integrity: bool = False,
	phase: str | None = None,
) -> dict[str, Any]:
	try:
		result = json.loads(output)
		statuses = {"passed": 0, "failed": 1, "not_applicable": 0, "unchecked": 2}
		if secrets or integrity:
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
		if phase == "validate":
			_validate_read_only_phase(result, status)
		elif phase == "write":
			_validate_write_phase(result, status)
		elif "phase" in result:
			raise EditError("tool_output")
		return result
	except (ValueError, TypeError) as error:
		raise EditError("tool_output") from error


def main(*, secrets: bool = False, types: bool = False, integrity: bool = False) -> int:
	"""Read one bounded request and emit exactly one source-free JSON result."""
	started = time.monotonic()
	delivery_deadline = started + MAX_DEADLINE_MS / 1000
	request = None
	background: dict[str, Any] = {}
	try:
		payload = _request()
		request = decode_request(payload, secrets=secrets, types=types, integrity=integrity)
		delivery_deadline = time.monotonic() + request["deadline_unix_ms"] / 1000 - time.time()
		deadline = (
			time.monotonic()
			+ request["deadline_unix_ms"] / 1000
			- time.time()
			- DELIVERY_RESERVE_SECONDS
		)
		result = _run_worker(
			request, payload, deadline, secrets=secrets, types=types, integrity=integrity
		)
	except EditError as error:
		result = _failure(error.reason, secrets=secrets, types=types, integrity=integrity)
		background = error.background
		if not types:
			result["stage"] = error.stage
	except OSError:
		result = _failure("io", secrets=secrets, types=types, integrity=integrity)
	if types:
		result.update(background_identity(request) | background)
	_apply_phase_metadata(result, request)
	result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
	try:
		output = encode(result)
	except EditError:
		result = _failure("limits", secrets=secrets, types=types, integrity=integrity)
		if types:
			result.update(background_identity(request) | background)
		_apply_phase_metadata(result, request)
		result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
		output = encode(result)
	_deliver(output, delivery_deadline)
	if result.get("stage") == "intake":
		os._exit(EXIT_UNCHECKED)
	return {"unchecked": 2, "failed": 1}.get(result["status"], 0)


def _run_worker(
	request: dict[str, Any],
	payload: bytes,
	deadline: float,
	*,
	secrets: bool,
	types: bool,
	integrity: bool = False,
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
		or getattr(root.lstat(), "st_file_attributes", 0)
		& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
		or root.is_symlink()
	):
		raise EditError("unsafe_path")
	cancellation = None
	if types and request["schema"] == BACKGROUND_SCHEMA:
		from .edit_type_inputs import CancellationSignal

		cancellation = CancellationSignal(request)
	workspace_parent = (
		None if request.get("phase") == "validate" and not (secrets or types or integrity) else root
	)
	with tempfile.TemporaryDirectory(
		prefix=".quality-gate-edit-", dir=workspace_parent
	) as directory:
		entry = (
			f"import sys; sys.path.insert(0, {str(package_root)!r}); "
			"from pathlib import Path; from quality_gate.edit_worker import main; "
			f"main(Path({str(Path(directory).resolve())!r}), "
			f"secrets={secrets!r}, types={types!r}, integrity={integrity!r})"
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
	result = _validate_result(
		output,
		stderr,
		code,
		secrets=secrets,
		schema=request["schema"],
		integrity=integrity,
		phase=request.get("phase"),
	)
	if cancellation is not None and cancellation():
		raise EditError("cancelled")
	if time.monotonic() >= deadline:
		raise EditError("deadline")
	return result


if __name__ == "__main__":
	raise SystemExit(main())
