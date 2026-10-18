"""Version 1 bounded, source-free working-tree Ruff protocol."""

from __future__ import annotations

import json
import time
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
DELIVERY_RESERVE_SECONDS = 0.05
SUPPORTED_POLICIES = frozenset({"v2.4.2", "v2.5.0", "v2.6.0", "v2.6.1"})
SAFE_FIXES = ("I001",)
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
}


class EditError(Exception):
	"""A protocol failure identified by a fixed, source-free reason code."""

	def __init__(self, reason: str) -> None:
		self.reason = reason
		self.stage = "request"
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


def decode_request(payload: bytes) -> dict[str, Any]:
	"""Validate the envelope before selecting files or prerequisites."""
	try:
		request = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
	except (UnicodeError, ValueError, RecursionError) as error:
		raise EditError("invalid_request") from error
	if len(payload) > MAX_REQUEST_BYTES or not isinstance(request, dict):
		raise EditError("invalid_request")
	allowed = {"schema", "root", "files", "deadline_unix_ms", "baseline", "cache_dir"}
	if set(request) - allowed or type(request.get("schema")) is not int or request["schema"] != 1:
		raise EditError("invalid_request")
	deadline = request.get("deadline_unix_ms")
	if type(deadline) is not int:
		raise EditError("invalid_request")
	remaining_ms = deadline - int(time.time() * 1000)
	if remaining_ms <= DELIVERY_RESERVE_SECONDS * 1000:
		raise EditError("deadline")
	if remaining_ms > MAX_DEADLINE_MS:
		raise EditError("invalid_request")
	if not isinstance(request.get("root"), str) or not isinstance(request.get("files"), list):
		raise EditError("invalid_request")
	cache_dir = request.get("cache_dir")
	if "cache_dir" in request and (
		not isinstance(cache_dir, str) or not Path(cache_dir).is_absolute()
	):
		raise EditError("invalid_request")
	if "baseline" in request:
		_validate_baseline(request["baseline"])
	return request


def _validate_baseline(baseline: object) -> None:
	if (
		not isinstance(baseline, dict)
		or set(baseline) != {"policy_release", "python", "python_version"}
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
