"""Source-free diagnostic vocabulary shared by producers and presentation.

This host envelope is independent of the immutable provider wire protocols.
Unknown strings never become diagnostic text or authority for writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from _shared.quality_storage import atomic_json, directory_transaction

KNOWN_REASONS = frozenset(
	"""
abandoned_shell_call ambiguous_report background_busy background_state_links
background_unavailable background_state_busy background_state_io batch_size_limit
cancelled coverage codex_root_unavailable concurrent_edit concurrent_observation configuration
configuration_limit deadline dependencies duplicate_pre_event event_size_limit
file_count_limit file_size_limit findings_limit incomplete_installation input_limit
installation_not_prepared installation_unavailable invalid_background_state
invalid_baseline_runtime invalid_checks invalid_configuration invalid_edit_budget
invalid_event invalid_install_lock_wait invalid_json invalid_mechanism_selection invalid_output
invalid_output_limit invalid_patch invalid_provider_result invalid_release_identity
invalid_report invalid_request invalid_secret_finding invalid_settings
invalid_settings_json invalid_snapshot io limits manifest mechanism_unavailable
missing_event_identity mutation_failed mutation_not_confirmed no_changes pre_edit
previous_settings_missing process_cleanup process_io_unavailable process_unavailable
registration_conflict release release_selection_required repeated_findings
request_limit runtime runtime_directory_unavailable security_unavailable
settings_busy settings_changed settings_io_unavailable settings_missing
settings_size_limit snapshot_limit snapshot_missing snapshot_not_configured
snapshot_root_changed snapshot_state_busy source_bundle_missing stale_input
stale_result state_busy state_inside_project tool tool_output
unmanaged_install_receipt unmanaged_registration unmanaged_settings
unreadable_event_or_file unreadable_file unsafe_host_root unsafe_path
unsafe_settings_path unsupported_event unsupported_file unsupported_policy
unsupported_settings_operation unsupported_settings_schema unsupported_tool
windows_handle_unavailable windows_job_assignment_failed windows_job_cleanup_deadline
windows_job_close_failed windows_job_query_failed windows_job_termination_failed
windows_job_unavailable windows_jobs_unavailable windows_resume_failed
windows_thread_unavailable
""".split()
)
OPERATIONS = frozenset(
	"""
settings_read receipt_read event_read attribution shell_completion snapshot_pre snapshot_post
snapshot_store
snapshot_read snapshot_cleanup patch_inputs provider_write provider_validate background_identity
background_settings background_inputs background_state background_launch background_worker
background_collect background_cancel coordinate final_inputs evidence_store
""".split()
)
DIMENSIONS = frozenset(
	{
		"file_count",
		"file_bytes",
		"batch_bytes",
		"snapshot_entries",
		"snapshot_bytes",
		"snapshot_seconds",
		"event_bytes",
	}
)
MAX_RECORD_BYTES = 64 * 1024
MAX_NUMBER = 2**63 - 1
FIRST_CONTROL_CODE = 32


def safe_text(value: object, maximum: int = 4096) -> str | None:
	if (
		isinstance(value, str)
		and 0 < len(value) <= maximum
		and all(ord(c) >= FIRST_CONTROL_CODE for c in value)
	):
		return value
	return None


def safe_diagnostic(value: object) -> dict[str, Any]:
	if not isinstance(value, dict):
		return {}
	result: dict[str, Any] = {}
	for name, allowed in (
		("operation", OPERATIONS),
		("limit_dimension", DIMENSIONS),
		("phase", {"write", "validate", "foreground", "background", "pre", "post"}),
		("category", {"io", "permission", "missing", "busy", "invalid", "unknown", "limit"}),
	):
		item = value.get(name)
		if isinstance(item, str) and item in allowed:
			result[name] = item
	for name in ("errno", "winerror", "observed", "allowed"):
		item = value.get(name)
		if (
			isinstance(item, (int, float))
			and not isinstance(item, bool)
			and 0 <= item <= MAX_NUMBER
			and (name not in {"errno", "winerror"} or type(item) is int)
		):
			result[name] = item
	return result


class DiagnosticError(ValueError):
	"""A fixed producer reason with separately validated diagnostic metadata."""

	def __init__(self, reason: str, **details: object) -> None:
		super().__init__(reason)
		self.diagnostic = safe_diagnostic(details)
		self.input_scope = scope({"cwd": details.get("root")})
		self.input_scope.pop("event", None)
		if isinstance(details.get("input_paths"), list):
			self.input_scope["input_paths"] = relative_paths(details["input_paths"])


def failure(error: Exception, operation: str, fallback: str = "unknown") -> dict[str, Any]:
	"""Capture identities before a broad exception boundary erases them."""
	reason = str(error) if not isinstance(error, OSError) else fallback
	if reason not in KNOWN_REASONS:
		reason = fallback if fallback in KNOWN_REASONS else "unknown"
	os_error: BaseException = error
	for _ in range(4):
		if isinstance(os_error, OSError) or os_error.__cause__ is None:
			break
		os_error = os_error.__cause__
	category = (
		"permission"
		if isinstance(os_error, PermissionError)
		else "missing"
		if isinstance(os_error, FileNotFoundError)
		else "io"
		if isinstance(os_error, OSError)
		else "invalid"
	)
	details = {
		**safe_diagnostic(getattr(error, "diagnostic", None)),
		"operation": safe_diagnostic(getattr(error, "diagnostic", None)).get(
			"operation", operation
		),
		"category": safe_diagnostic(getattr(error, "diagnostic", None)).get("category", category),
		**safe_diagnostic(
			{
				"errno": getattr(os_error, "errno", None),
				"winerror": getattr(os_error, "winerror", None),
			}
		),
	}
	phases = {
		"snapshot_pre": "pre",
		"snapshot_post": "post",
		"provider_write": "write",
		"provider_validate": "validate",
	}
	if operation in phases:
		details.setdefault("phase", phases[operation])
	return {
		"status": "unchecked",
		"reason": reason,
		"diagnostic": safe_diagnostic(details),
		**(error.input_scope if isinstance(error, DiagnosticError) else {}),
	}


def scope(payload: object) -> dict[str, Any]:
	"""Only event identities, never command text or tool output."""
	if not isinstance(payload, dict):
		return {}
	result: dict[str, Any] = {
		"event": {
			name: value
			for name in ("session_id", "turn_id", "tool_use_id")
			if (value := safe_text(payload.get(name), 256)) is not None
		}
	}
	cwd = safe_text(payload.get("cwd"))
	if cwd and Path(cwd).is_absolute():
		result["root"] = cwd
	return result


def relative_paths(paths: object) -> list[str]:
	if not isinstance(paths, list):
		return []
	return [
		p
		for p in paths[:10]
		if safe_text(p)
		and not PurePosixPath(p.replace("\\", "/")).is_absolute()
		and ".." not in PurePosixPath(p.replace("\\", "/")).parts
		and ":" not in p
	]


def record(report: dict[str, Any]) -> dict[str, Any]:
	"""Sanitize the complete retained envelope, including untrusted provider fields."""
	reason = report.get("reason")
	result: dict[str, Any] = {
		"schema": 1,
		"status": "unchecked",
		"reason": reason if isinstance(reason, str) and reason in KNOWN_REASONS else "unknown",
		"diagnostic": safe_diagnostic(report.get("diagnostic")),
	}
	check = report.get("check")
	result["check"] = (
		check
		if isinstance(check, str)
		and check
		in {
			"ruff",
			"types",
			"secrets",
			"integrity",
			"settings",
			"attribution",
			"background",
			"legacy_security",
			"builtin_secrets",
		}
		else "quality"
	)
	event = report.get("event")
	result.update(scope({"cwd": report.get("root"), **(event if isinstance(event, dict) else {})}))
	paths = report.get("input_paths")
	if paths is None and isinstance(report.get("files"), list):
		paths = [
			item.get("path")
			for item in report["files"][:10]
			if isinstance(item, dict) and item.get("root", report.get("root")) == report.get("root")
		]
	if isinstance(paths, list):
		result["input_paths"] = relative_paths(paths)
	phase = report.get("phase")
	if isinstance(phase, str) and phase in {"write", "validate"}:
		result["diagnostic"].setdefault("phase", report["phase"])
	if type(report.get("writes_may_have_occurred")) is bool:
		result["writes_may_have_occurred"] = report["writes_may_have_occurred"]
	background_id = safe_text(report.get("background_event_id"), 256)
	if background_id:
		result["background_event_id"] = background_id
	return result


def plain(path: Path) -> None:
	for item in (path, *path.parents):
		if item.is_symlink() or (
			item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400
		):
			raise ValueError("unsafe_path")


def read_record(path: Path, maximum: int = MAX_RECORD_BYTES) -> dict[str, Any]:
	"""Bound parsing and reject replaced files and unsafe traversal."""
	plain(path)
	before = path.stat()
	if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
		raise ValueError("invalid_snapshot")
	with path.open("rb") as stream:
		if (before.st_dev, before.st_ino) != (
			os.fstat(stream.fileno()).st_dev,
			os.fstat(stream.fileno()).st_ino,
		):
			raise ValueError("unsafe_path")
		content = stream.read(maximum + 1)
	plain(path)
	after = path.stat()
	if len(content) > maximum or (before.st_dev, before.st_ino) != (
		after.st_dev,
		after.st_ino,
	):
		raise ValueError("invalid_snapshot")
	if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
		raise ValueError("invalid_snapshot")
	value = json.loads(content)
	if not isinstance(value, dict):
		raise ValueError("invalid_snapshot")
	return value


def retain(report: dict[str, Any], directory: Path) -> tuple[str, str | None]:
	clean = record(report)
	content = json.dumps(clean, sort_keys=True, ensure_ascii=True).encode()
	identity = hashlib.sha256(content).hexdigest()
	path = directory / f"diagnostic-{identity}.json"
	try:
		plain(path)
		plain(directory / ".transaction.lock")
		with directory_transaction(directory):
			plain(path)
			if path.exists():
				if read_record(path) != clean:
					raise ValueError("invalid_snapshot")
			else:
				atomic_json(path, clean)
		return identity, str(path)
	except (OSError, ValueError):
		return identity, None
