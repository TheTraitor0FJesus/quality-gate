"""Independent read-only typing hook consuming the released edit-types protocol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
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

from python_types.inputs import TypingInputError, snapshot_inputs
from python_types.results import STATUS_EXIT, unchecked, unique_keys, validate_result

MAX_REQUEST_BYTES = 64 * 1024


def not_applicable(reason: str) -> dict[str, Any]:
	return {**unchecked(reason), "status": "not_applicable"}


def check_files(
	root: Path, files: object, settings: dict[str, Any], deadline: float
) -> dict[str, Any]:
	"""Consume final writer identities and the remaining common event deadline."""
	if "types" not in settings["enabled"]:
		return not_applicable("disabled")
	try:
		current = snapshot_inputs(root, files, deadline)
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
		)
	except (TypingInputError, ProcessError) as error:
		return unchecked(str(error))
	if not process.stdout.strip():
		return unchecked("provider_unavailable")
	if process.stderr:
		return unchecked("tool_output")
	return validate_result(
		process.stdout, process.returncode, request, settings["provider_release"], deadline
	)


def ingress(content: bytes) -> dict[str, Any]:
	if len(content) > MAX_REQUEST_BYTES:
		raise TypingInputError("limits")
	request = json.loads(content, object_pairs_hook=unique_keys)
	if (
		not isinstance(request, dict)
		or set(request) != {"schema", "root", "files"}
		or type(request["schema"]) is not int
		or request["schema"] != 1
		or not isinstance(request["root"], str)
	):
		raise TypingInputError("invalid_request")
	return request


def main() -> int:
	started = monotonic()
	parser = argparse.ArgumentParser()
	parser.add_argument("--settings", type=Path)
	parser.add_argument("--state-dir", type=Path)
	parser.add_argument("--session-id")
	actions = parser.add_mutually_exclusive_group()
	actions.add_argument("--background", action="store_true")
	actions.add_argument("--drain", action="store_true")
	actions.add_argument("--cancel", action="store_true")
	arguments = parser.parse_args()
	try:
		settings_path = arguments.settings or default_settings_path()
		settings = load_settings(settings_path)["current"]
		if arguments.background or arguments.drain or arguments.cancel:
			result = background_control(arguments, settings_path, settings, started)
		else:
			request = ingress(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
			result = check_files(
				Path(request["root"]),
				request["files"],
				settings,
				started + settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS),
			)
	except TypingInputError as error:
		result = unchecked(str(error))
	except (SettingsError, OSError, ValueError, TypeError, KeyError, RecursionError):
		result = unchecked("invalid_request_or_settings")
	sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
	if result["status"] == "drained":
		return drain_exit_code(result["reports"])
	return STATUS_EXIT.get(result["status"], 2)


def drain_exit_code(reports: list[dict[str, Any]]) -> int:
	"""Waived secret findings are completed clean work during shared drain."""
	return max(
		(_report_exit_code(report.get("status")) for report in reports),
		default=0,
	)


def _report_exit_code(status: object) -> int:
	if status == "waived":
		return 0
	if not isinstance(status, str):
		return 2
	return STATUS_EXIT.get(status, 2)


def background_control(
	arguments: argparse.Namespace, settings_path: Path, settings: dict[str, Any], started: float
) -> dict[str, Any]:
	from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule
	from _shared.quality_background_state import CLEANUP_SECONDS, MAX_DRAIN_SECONDS
	from _shared.quality_feedback import runtime_directory

	if not arguments.session_id:
		raise TypingInputError("invalid_event")
	state_dir = arguments.state_dir or runtime_directory() / "events"
	session = BackgroundSession(settings_path, state_dir, arguments.session_id)
	if arguments.background:
		request = ingress(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
		return schedule(
			Path(request["root"]),
			request["files"],
			session=session,
			deadline=started + settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS),
		)
	if arguments.cancel:
		reports = cancel_session(session=session, deadline=started + CLEANUP_SECONDS)
	else:
		reports = collect(
			session=session,
			deadline=started + MAX_DRAIN_SECONDS,
			drain=True,
		)
	return {"schema": 1, "status": "drained", "reports": reports}


if __name__ == "__main__":
	raise SystemExit(main())
