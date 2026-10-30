"""Version 1 bounded, source-free working-tree edit protocols."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_TOOL_BYTES = 512 * 1024
MAX_FILES = 10
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_CONFIG_TOTAL_BYTES = 1024 * 1024
MAX_FINDINGS = 200
MAX_PATH_CHARS = 1024
FIRST_PRINTABLE_CHARACTER = 32
EXIT_UNCHECKED = 2
MAX_DEADLINE_MS = 10_000
MAX_BACKGROUND_DEADLINE_MS = 30_000
BACKGROUND_SCHEMA = 2
DELIVERY_RESERVE_SECONDS = 0.05
SUPPORTED_POLICIES = frozenset(
	{
		"v2.4.2",
		"v2.5.0",
		"v2.6.0",
		"v2.6.1",
		"v2.6.2",
		"v3.0.0",
		"v3.1.0",
		"v3.2.0",
		"v4.0.0",
		"v4.1.0",
		"v4.2.0",
		"v4.2.1",
		"v4.3.0",
		"v4.4.0",
		"v4.5.0",
		"v4.6.0",
	}
)
SAFE_FIXES = ("I001", "F401")
RECOVERY = {
	"invalid_request": "Send a schema 1 request with explicit files and current SHA-256 values.",
	"deadline": "Retry fresh content within the shared edit deadline; inspect slow prerequisites.",
	"unsafe_path": "Use regular project-owned files inside the explicit root, without links.",
	"limits": "Split the edit into bounded batches and retry every unchecked path.",
	"stale_input": "Capture current file/configuration identities and retry the complete batch.",
	"manifest": "Correct the working-tree manifest and run quality-gate validate.",
	"unsupported_policy": "Follow the explicit consumer release update in docs/agent-reference.md.",
	"release": "Run quality-gate sync for the exact immutable policy release, then edit-setup.",
	"runtime": (
		"Run quality-gate setup for managed components; restore the selected baseline Python."
	),
	"tool": "Run edit-setup for the exact release; restore the verified native Ruff executable.",
	"configuration": (
		"Restore readable valid working-tree configuration/dependency inputs and setup."
	),
	"tool_output": (
		"Restore the selected Ruff tool and retry; raw tool output is intentionally omitted."
	),
	"io": "Restore readable/writable regular files and retry with fresh identities.",
	"cancelled": (
		"Reap cancelled typing work and submit the current event if checking is still required."
	),
}


class EditError(Exception):
	"""A protocol failure identified by a fixed, source-free reason code."""

	def __init__(self, reason: str) -> None:
		self.reason = reason
		self.stage = "request"
		self.background: dict[str, Any] = {}
		super().__init__(reason)


def unchecked(reason: str) -> dict[str, Any]:
	"""Return a fail-closed response without echoing exception or tool text."""
	return {
		"schema": 1,
		"status": "unchecked",
		"reason": reason,
		"action": RECOVERY[reason],
		"files": [],
		"writes_may_have_occurred": True,
	}


def encode(result: dict[str, Any]) -> bytes:
	"""Encode one bounded UTF-8 JSON response."""
	encoded = (json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
	if len(encoded) > MAX_RESULT_BYTES:
		raise EditError("limits")
	return encoded


def decode_request(
	payload: bytes, *, secrets: bool = False, types: bool = False, integrity: bool = False
) -> dict[str, Any]:
	"""Validate the envelope before selecting files or prerequisites."""
	read_only = _read_only_reader(secrets, integrity, types)
	try:
		request = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
	except (UnicodeError, ValueError, RecursionError) as error:
		raise EditError("invalid_request") from error
	if len(payload) > MAX_REQUEST_BYTES or not isinstance(request, dict):
		raise EditError("invalid_request")
	background = _validate_envelope(request, secrets=secrets, types=types, integrity=integrity)
	deadline = request.get("deadline_unix_ms")
	if type(deadline) is not int:
		raise EditError("invalid_request")
	if not isinstance(request.get("root"), str) or not isinstance(request.get("files"), list):
		raise EditError("invalid_request")
	cache_dir = request.get("cache_dir")
	if "cache_dir" in request and (
		not isinstance(cache_dir, str) or not Path(cache_dir).is_absolute()
	):
		raise EditError("invalid_request")
	if "baseline" in request:
		_validate_baseline(request["baseline"], read_only=read_only)
	remaining_ms = deadline - int(time.time() * 1000)
	if remaining_ms <= DELIVERY_RESERVE_SECONDS * 1000:
		deadline_error = EditError("deadline")
		if background:
			deadline_error.background = background_identity(request)
		raise deadline_error
	read_only_ruff = not read_only and request.get("phase") == "validate"
	maximum_deadline_ms = (
		MAX_BACKGROUND_DEADLINE_MS if background or read_only_ruff else MAX_DEADLINE_MS
	)
	if remaining_ms > maximum_deadline_ms:
		raise EditError("invalid_request")
	return request


def _validate_envelope(
	request: dict[str, Any], *, secrets: bool, types: bool, integrity: bool = False
) -> bool:
	read_only = _read_only_reader(secrets, integrity, types)
	allowed = {"schema", "root", "files", "deadline_unix_ms", "baseline", "cache_dir"}
	background = types and request.get("schema") == BACKGROUND_SCHEMA
	if not read_only:
		allowed.add("phase")
	if "phase" in request and (read_only or request.get("phase") not in {"write", "validate"}):
		raise EditError("invalid_request")
	if background:
		allowed |= {"mode", "event_id", "cancel_path"}
		_validate_background(request)
	if (
		set(request) - allowed
		or type(request.get("schema")) is not int
		or request["schema"] != (BACKGROUND_SCHEMA if background else 1)
	):
		raise EditError("invalid_request")
	return background


def _validate_background(request: dict[str, Any]) -> None:
	event_id, cancel_path = request.get("event_id"), request.get("cancel_path")
	if (
		request.get("mode") != "background"
		or not isinstance(event_id, str)
		or not isinstance(cancel_path, str)
		or not Path(cancel_path).is_absolute()
	):
		raise EditError("invalid_request")
	try:
		if str(uuid.UUID(event_id)) != event_id:
			raise ValueError("noncanonical event identity")
	except ValueError as error:
		raise EditError("invalid_request") from error


def background_identity(request: dict[str, Any] | None) -> dict[str, Any]:
	"""Bind validated background completions, including unavailable results, to an event."""
	if request is not None and request["schema"] == BACKGROUND_SCHEMA:
		return {"schema": BACKGROUND_SCHEMA, "mode": "background", "event_id": request["event_id"]}
	return {}


def _read_only_reader(secrets: bool, integrity: bool, types: bool) -> bool:
	if sum((secrets, integrity, types)) > 1:
		raise EditError("invalid_request")
	return secrets or integrity or types


def _validate_baseline(baseline: object, *, read_only: bool = False) -> None:
	fields = {"policy_release"} if read_only else {"policy_release", "python", "python_version"}
	if (
		not isinstance(baseline, dict)
		or set(baseline) != fields
		or not all(isinstance(value, str) and value for value in baseline.values())
	):
		raise EditError("invalid_request")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		if key in result:
			raise EditError("invalid_request")
		result[key] = value
	return result
