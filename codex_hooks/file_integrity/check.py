"""Independent read-only integrity hook using the public edit-integrity CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from time import monotonic, time
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_feedback import RECOVERY
from _shared.quality_process import ProcessError, execute
from _shared.quality_settings import DEFAULT_BUDGET_SECONDS, SettingsError

from file_integrity.feedback import CHECK_ID, integrity_projection
from file_integrity.inputs import IntegrityInputError, snapshot_files
from file_integrity.results import REASONS, STATUS_EXIT, unique_keys, validate_result

MAX_REQUEST_BYTES = 64 * 1024


def _reason(value: object, fallback: str = "tool") -> str:
	if value == "output_limit":
		return "limits"
	if isinstance(value, str) and (value in RECOVERY or value in REASONS):
		return value
	return fallback


def _feedback(
	status: str,
	findings: list[dict[str, Any]],
	paths: set[str],
	reason: str | None = None,
) -> dict[str, Any]:
	projection = {
		"schema": 1,
		"check_id": CHECK_ID,
		"status": status,
		"findings": findings,
	}
	if reason is not None:
		projection["reason"] = reason
	return integrity_projection(json.dumps(projection, ensure_ascii=True), touched_paths=paths)


def check_files(
	root: Path, files: object, settings: dict[str, Any], deadline: float
) -> dict[str, Any]:
	"""Validate explicit post-writer file hashes against the shared event deadline."""
	if "integrity" not in settings["enabled"]:
		return _feedback("not_applicable", [], set())
	try:
		current = snapshot_files(root, files, deadline)
		if not current:
			return _feedback("not_applicable", [], set())
		request = {
			"schema": 1,
			"root": str(root.resolve()),
			"files": current,
			"deadline_unix_ms": int((time() + max(0, deadline - monotonic())) * 1000),
			"baseline": {"policy_release": settings["baseline_release"]},
		}
		payload = json.dumps(request, ensure_ascii=True)
		if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
			return _feedback("unchecked", [], {item["path"] for item in current}, "limits")
		process = execute(
			[
				sys.executable,
				"-B",
				str(Path(__file__).with_name("provider_bridge.py")),
				"--release",
				settings["provider_release"],
			],
			deadline=deadline,
			input_text=payload,
		)
	except IntegrityInputError as error:
		return _feedback("unchecked", [], set(), _reason(error.reason, "invalid_request"))
	except ProcessError as error:
		return _feedback("unchecked", [], set(), _reason(str(error)))
	if not process.stdout.strip() or process.stderr:
		return _feedback("unchecked", [], {item["path"] for item in current}, "tool")
	result = validate_result(
		process.stdout,
		process.returncode,
		request,
		settings["provider_release"],
		deadline,
	)
	return _feedback(
		result["status"],
		result["findings"],
		{item["path"] for item in current},
		_reason(result["reason"]) if result["reason"] is not None else None,
	)


def main() -> int:
	from _shared.quality_settings import default_settings_path, load_settings

	started = monotonic()
	try:
		settings = load_settings(default_settings_path())["current"]
		content = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
		if len(content) > MAX_REQUEST_BYTES:
			raise IntegrityInputError("limits")
		request = json.loads(content, object_pairs_hook=unique_keys)
		if (
			not isinstance(request, dict)
			or set(request) != {"schema", "root", "files"}
			or type(request["schema"]) is not int
			or request["schema"] != 1
			or not isinstance(request["root"], str)
		):
			raise IntegrityInputError("invalid_request")
		root = Path(request["root"])
		result = check_files(
			root,
			request["files"],
			settings,
			started + settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS),
		)
	except IntegrityInputError as error:
		result = _feedback("unchecked", [], set(), _reason(error.reason, "invalid_request"))
	except SettingsError as error:
		result = _feedback("unchecked", [], set(), _reason(str(error), "settings_missing"))
	except OSError:
		result = _feedback("unchecked", [], set(), "io")
	except (ValueError, TypeError, KeyError, RecursionError):
		result = _feedback("unchecked", [], set(), "invalid_request")
	sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
	return STATUS_EXIT.get(result["status"], 2)


if __name__ == "__main__":
	raise SystemExit(main())
