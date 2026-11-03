"""Independent read-only secret hook using the public released edit-secrets CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from threading import Event
from time import monotonic, time
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_process import ProcessError, execute
from _shared.quality_settings import (
	DEFAULT_BUDGET_SECONDS,
	SettingsError,
	default_settings_path,
	load_settings,
)

from secret_scan.inputs import SecretInputError, snapshot_files
from secret_scan.results import (
	STATUS_EXIT,
	SecretResultError,
	unchecked,
	unique_keys,
	validate_result,
)

MAX_REQUEST_BYTES = 64 * 1024


def not_applicable(reason: str) -> dict[str, Any]:
	return {**unchecked(reason), "status": "not_applicable"}


def check_files(
	root: Path,
	files: object,
	settings: dict[str, Any],
	deadline: float,
	*,
	cancel_event: Event | None = None,
) -> dict[str, Any]:
	"""Consume the coordinator's final hashes and its remaining common deadline."""
	if "secrets" not in settings["enabled"]:
		return not_applicable("disabled")
	try:
		current = snapshot_files(root, files, deadline)
		if not current:
			return not_applicable("no_files")
		request = {
			"schema": 1,
			"root": str(root.resolve()),
			"files": current,
			"deadline_unix_ms": int((time() + max(0, deadline - monotonic())) * 1000),
			"baseline": {"policy_release": settings["baseline_release"]},
		}
		payload = json.dumps(request, ensure_ascii=True)
		if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
			return unchecked("limits")
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
			cancel_event=cancel_event,
		)
	except (SecretInputError, ProcessError) as error:
		return unchecked(str(error))
	if not process.stdout.strip():
		return unchecked("provider_unavailable")
	if process.stderr:
		return unchecked("tool_output")
	return validate_result(
		process.stdout, process.returncode, request, settings["provider_release"], deadline
	)


def main() -> int:
	started = monotonic()
	parser = argparse.ArgumentParser()
	parser.add_argument("--settings", type=Path)
	arguments = parser.parse_args()
	try:
		settings = load_settings(arguments.settings or default_settings_path())["current"]
		content = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
		if len(content) > MAX_REQUEST_BYTES:
			raise SecretInputError("limits")
		request = json.loads(content, object_pairs_hook=unique_keys)
		if (
			not isinstance(request, dict)
			or set(request) != {"schema", "root", "files"}
			or type(request["schema"]) is not int
			or request["schema"] != 1
			or not isinstance(request["root"], str)
		):
			raise SecretInputError("invalid_request")
		root = Path(request["root"])
		result = check_files(
			root,
			request["files"],
			settings,
			started + settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS),
		)
	except SecretInputError as error:
		result = unchecked(str(error))
	except (
		SettingsError,
		SecretResultError,
		OSError,
		ValueError,
		TypeError,
		KeyError,
		RecursionError,
	):
		result = unchecked("invalid_request_or_settings")
	sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
	return STATUS_EXIT[result["status"]]


if __name__ == "__main__":
	raise SystemExit(main())
