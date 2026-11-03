"""Synchronous event routing, writer-before-reader ordering and model delivery."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_background import BackgroundSession
from _shared.quality_background_state import BACKGROUND_CHECK_ORDER
from _shared.quality_diagnostics import failure, safe_diagnostic, scope
from _shared.quality_events import (
	AttributionError,
	attribute,
	content_record,
	discard_shell_snapshot,
	read_event,
	safe_path,
)
from _shared.quality_feedback import deliver_feedback, feedback, runtime_directory
from _shared.quality_legacy_security import heavy_security_enabled, run_scanner
from _shared.quality_settings import (
	DEFAULT_BUDGET_SECONDS,
	SettingsError,
	default_settings_path,
	load_settings,
)

SECURITY_RESERVE_SECONDS = 0.4


def legacy_security(
	root: Path,
	files: list[dict[str, Any]],
	deadline: float,
	*,
	builtin_only: bool = False,
	cancel_event: Event | None = None,
) -> dict[str, Any]:
	return run_scanner(
		root,
		files,
		deadline,
		builtin_only=builtin_only,
		cancel_event=cancel_event,
	)


def refresh_files(root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Readers receive actual post-writer hashes, including after partial failures."""
	refreshed = []
	for item in files:
		if item["operation"] == "deleted":
			continue
		path, _ = safe_path(item["path"], root)
		current = content_record(path, root, item["operation"])
		refreshed.append({"path": current["path"], "sha256": current["sha256"]})
	return refreshed


def read_only_reports(
	root: Path,
	files: list[dict[str, Any]],
	settings: dict[str, Any],
	deadline: float,
) -> list[dict[str, Any]]:
	"""Run the independent synchronous integrity reader after Ruff writes."""
	if "integrity" not in settings["enabled"] or not files:
		return []
	reader_deadline = deadline - min(SECURITY_RESERVE_SECONDS, max(0, deadline - monotonic()))
	from file_integrity.check import check_files as check_integrity

	report = check_integrity(root, files, settings, reader_deadline)
	report.setdefault("root", str(root))
	return [report]


def check_group(
	root: Path,
	files: list[dict[str, Any]],
	settings: dict[str, Any],
	deadline: float,
	*,
	background: BackgroundSession | None = None,
	write_allowed: bool = True,
) -> list[dict[str, Any]]:
	from python_quality.check import check_files

	python_files = [
		{"path": item["path"], "sha256": item["sha256"]}
		for item in files
		if item["operation"] != "deleted" and Path(item["path"]).suffix.lower() in {".py", ".pyi"}
	]
	# Preserve foreground time for the integrity check and legacy security fallback.
	writer_deadline = deadline - min(SECURITY_RESERVE_SECONDS, max(0, deadline - monotonic()) * 0.2)
	writer = (
		check_files(root, python_files, settings, writer_deadline, phase="write")
		if write_allowed
		else {"status": "not_applicable", "reason": "concurrent_observation"}
	)
	reports = [
		{
			"check": "ruff",
			"root": str(root),
			**writer,
			"diagnostic": {
				"operation": "provider_write",
				**safe_diagnostic(writer.get("diagnostic")),
			},
		}
	]
	reports.extend(
		{"check": name, "status": "unchecked", "reason": "mechanism_unavailable"}
		for name in settings["enabled"]
		if name not in {"ruff", "secrets", "integrity", "types"}
	)
	try:
		final_files = refresh_files(root, files)
	except (OSError, ValueError, AttributionError) as error:
		reports.append(
			{
				"check": "attribution",
				"root": str(root),
				"input_paths": [item["path"] for item in files],
				**failure(
					error, "final_inputs", "io" if isinstance(error, OSError) else "stale_result"
				),
			}
		)
		return reports
	typing_inputs = [
		*final_files,
		*[
			{"path": item["path"], "sha256": None}
			for item in files
			if item["operation"] == "deleted"
		],
	]
	reports.extend(read_only_reports(root, final_files, settings, deadline))
	heavy_fallback = (
		bool(final_files) and "secrets" not in settings["enabled"] and heavy_security_enabled()
	)
	if final_files and "secrets" not in settings["enabled"]:
		reports.append(legacy_security(root, final_files, deadline, builtin_only=heavy_fallback))
	background_checks = [name for name in BACKGROUND_CHECK_ORDER if name in settings["enabled"]]
	if heavy_fallback:
		background_checks.append("legacy_security")
	if background_checks:
		from _shared.quality_background import schedule

		if background is None:
			reports.extend(
				{"check": name, "status": "unchecked", "reason": "invalid_event"}
				for name in background_checks
			)
		else:
			reports.append(
				schedule(
					root,
					typing_inputs,
					session=background,
					deadline=deadline,
					checks=background_checks,
				)
			)
	return reports


def release_blocked_pre_capture(
	payload: object,
	status: str,
	result: dict[str, Any] | None,
	state_dir: Path,
) -> dict[str, Any] | None:
	"""Keep only captures whose tool calls are still allowed to produce a Post."""
	if (
		isinstance(payload, dict)
		and status == "pending"
		and result
		and result.get("decision") == "block"
	):
		discard_shell_snapshot(payload, state_dir)
	return result


def attach_call(reports: list[dict[str, Any]], payload: object) -> None:
	for report in reports:
		report.setdefault("event", scope(payload).get("event", {}))


def coordinate(
	payload: object,
	*,
	settings_path: Path,
	state_dir: Path,
	evidence_dir: Path | None = None,
	preserve_legacy_security: bool = False,
) -> dict[str, Any] | None:
	started = monotonic()
	event = payload.get("hook_event_name") if isinstance(payload, dict) else "PostToolUse"
	if event not in {
		"PreToolUse",
		"PostToolUse",
		"Stop",
		"SubagentStop",
		"SessionEnd",
		"Interrupt",
	}:
		return None
	if isinstance(payload, dict) and event in {"Stop", "SubagentStop", "SessionEnd", "Interrupt"}:
		return completion_event(payload, event, settings_path, state_dir, evidence_dir, started)
	reports: list[dict[str, Any]] = []
	try:
		settings = load_settings(settings_path)["current"]
	except (SettingsError, OSError) as error:
		reports.append(
			{
				"check": "settings",
				**failure(error, "settings_read", "settings_io_unavailable"),
				**scope(payload),
			}
		)
		if not preserve_legacy_security:
			return feedback(event, reports, evidence_dir=evidence_dir)
		settings = {"enabled": []}
	budget = settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS)
	deadline = started + budget
	if not settings["enabled"] and not preserve_legacy_security:
		return feedback(
			event,
			background_reports(payload, settings_path, state_dir, deadline),
			evidence_dir=evidence_dir,
		)
	attribution = attribute(payload, state_dir=state_dir, deadline=deadline)
	if attribution["status"] in {"pending", "not_applicable"}:
		reports.extend(attribution.get("warnings", []))
		reports.extend(background_reports(payload, settings_path, state_dir, deadline))
		return release_blocked_pre_capture(
			payload,
			attribution["status"],
			feedback(event, reports, evidence_dir=evidence_dir),
			state_dir,
		)
	if attribution["status"] != "attributed":
		return feedback(
			event,
			[
				{"check": "attribution", **attribution},
				*background_reports(payload, settings_path, state_dir, deadline),
			],
			evidence_dir=evidence_dir,
		)
	groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for item in attribution["files"]:
		groups[item["root"]].append(item)
	background = BackgroundSession(
		settings_path, state_dir, attribution["event"].get("session_id"), payload
	)
	for root, files in groups.items():
		reports.extend(
			check_group(
				Path(root),
				files,
				settings,
				deadline,
				background=background,
				write_allowed=attribution.get("write_allowed", True),
			)
		)
	reports.extend(background_reports(payload, settings_path, state_dir, deadline))
	attach_call(reports, payload)
	return deliver_feedback(
		event,
		reports,
		session_id=attribution["event"].get("session_id"),
		settings=settings,
		state_dir=state_dir,
		evidence_dir=evidence_dir,
	)


def background_reports(
	payload: object, settings_path: Path, state_dir: Path, deadline: float, *, drain: bool = False
) -> list[dict[str, Any]]:
	if not (state_dir / "background").exists():
		return []
	from _shared.quality_background import collect

	return collect(
		session=BackgroundSession(
			settings_path,
			state_dir,
			payload.get("session_id") if isinstance(payload, dict) else None,
		),
		deadline=deadline,
		drain=drain,
	)


def completion_event(
	payload: dict[str, Any],
	event: str,
	settings_path: Path,
	state_dir: Path,
	evidence_dir: Path | None,
	started: float,
) -> dict[str, Any] | None:
	from _shared.quality_background import BackgroundSession, cancel_session
	from _shared.quality_background_state import CLEANUP_SECONDS, MAX_DRAIN_SECONDS

	if event == "SessionEnd":
		from _shared.quality_events import clear_session_snapshots

		clear_session_snapshots(state_dir, payload.get("session_id"))
	elif event in {"Stop", "Interrupt"}:
		from _shared.quality_events import clear_turn_snapshots

		clear_turn_snapshots(state_dir, payload.get("session_id"), payload.get("turn_id"))
	if not (state_dir / "background").exists():
		return None
	if event in {"SessionEnd", "Interrupt"}:
		reports = cancel_session(
			session=BackgroundSession(settings_path, state_dir, payload.get("session_id")),
			deadline=started + CLEANUP_SECONDS,
		)
	else:
		reports = background_reports(
			payload, settings_path, state_dir, started + MAX_DRAIN_SECONDS, drain=True
		)
	result = feedback("PostToolUse", reports, evidence_dir=evidence_dir)
	if result is None:
		return None
	text = result["hookSpecificOutput"]["additionalContext"]
	return (
		{"systemMessage": text}
		if event in {"SessionEnd", "Interrupt"}
		else {"decision": "block", "reason": text}
	)


def main(*, preserve_legacy_security: bool = False) -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--settings", type=Path)
	parser.add_argument("--state-dir", type=Path)
	parser.add_argument("--evidence-dir", type=Path)
	parser.add_argument("--registered", action="store_true")
	arguments = parser.parse_args()
	operation = "settings_read"
	try:
		settings_path = arguments.settings or default_settings_path()
		if arguments.registered:
			from _shared.quality_install import load_receipt

			operation = "receipt_read"
			receipt = load_receipt(settings_path.parents[2])
			if receipt.get("paused") is True or receipt.get("state") != "active":
				return 0
		operation = "event_read"
		payload = read_event()
		operation = "coordinate"
		result = coordinate(
			payload,
			settings_path=settings_path,
			state_dir=arguments.state_dir or runtime_directory() / "events",
			evidence_dir=arguments.evidence_dir,
			preserve_legacy_security=preserve_legacy_security,
		)
	except (AttributionError, OSError, ValueError, TypeError, KeyError) as error:
		result = feedback(
			"PostToolUse",
			[
				{
					"check": "settings"
					if operation in {"settings_read", "receipt_read"}
					else "attribution",
					**failure(error, operation),
				}
			],
			evidence_dir=arguments.evidence_dir,
		)
	if result is not None:
		sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
