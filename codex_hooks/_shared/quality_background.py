"""Root-owned bounded jobs, silent pending, and active-turn result delivery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep, time
from typing import Any

from python_types.freshness import digest, file_identity, plain_path
from python_types.inputs import input_identities, snapshot_inputs
from python_types.results import REASONS as TYPE_REASONS
from python_types.results import STATUS_EXIT, unchecked, validate_result
from python_types.selections import gate_cache_root, release_inventory

from . import quality_background_state as state
from .quality_diagnostics import failure, record, safe_diagnostic, scope
from .quality_settings import load_settings
from .quality_storage import StorageBusyError, directory_transaction

_children: list[subprocess.Popen[bytes]] = []
FILE_FINDING_CHECKS = frozenset({"ruff", "secrets", "builtin_secrets", "legacy_security"})


@dataclass(frozen=True)
class BackgroundSession:
	"""Bind per-session identity to the settings and state that own its jobs."""

	settings_path: Path
	state_dir: Path
	session_id: object
	origin_event: object = None


def pending(checks: list[str] | None = None) -> dict[str, Any]:
	return {
		"check": "types" if checks is None or checks == ["types"] else "background",
		"status": "pending",
	}


def check_enabled(settings: dict[str, Any], name: str) -> bool:
	from .quality_legacy_security import heavy_security_enabled

	return name in settings["enabled"] or (
		name == "legacy_security"
		and "secrets" not in settings["enabled"]
		and heavy_security_enabled()
	)


def selected_checks(settings: dict[str, Any], checks: object) -> list[str]:
	if checks is None:
		return ["types"] if "types" in settings["enabled"] else []
	if (
		not isinstance(checks, list)
		or not checks
		or any(not isinstance(name, str) or name not in state.BACKGROUND_CHECKS for name in checks)
		or len(set(checks)) != len(checks)
		or any(not check_enabled(settings, name) for name in checks)
	):
		raise ValueError("invalid_checks")
	return [name for name in state.BACKGROUND_CHECK_ORDER if name in checks]


def current_inputs(root: Path, paths: list[str], deadline: float) -> list[dict[str, Any]]:
	files = []
	for name in sorted(paths):
		path = root / name
		plain_path(path)
		sha256 = file_identity(path, deadline, 128 * 1024)[0] if path.exists() else None
		files.append({"path": name, "sha256": sha256})
	return snapshot_inputs(root, files, deadline)


def launch(directory: Path, event_id: str) -> None:
	"""Start an intentionally asynchronous one-shot supervisor without inherited pipes."""
	_children[:] = [child for child in _children if child.poll() is None]
	child = subprocess.Popen(
		[
			sys.executable,
			"-B",
			str(Path(__file__).with_name("quality_background_worker.py")),
			str(directory),
			event_id,
		],
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		close_fds=True,
		start_new_session=os.name != "nt",
		creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
	)
	_children.append(child)


def queue_document(
	root: Path,
	inputs: list[dict[str, Any]],
	previous: dict[str, Any] | None,
	settings: dict[str, Any],
	settings_path: Path,
	session_id: str,
	retry: int,
	checks: list[str],
) -> dict[str, Any]:
	audience = state.identity(session_id)
	waiting = (
		[name for name in previous["audiences"] if name not in previous["acknowledged"]]
		if previous
		else []
	)
	audiences = list(dict.fromkeys([*waiting, audience]))
	if len(audiences) > state.MAX_AUDIENCES:
		raise ValueError("limits")
	if previous:
		if previous["status"] == "pending":
			state.signal_cancel(Path(previous["request"]["cancel_path"]))
	event_id = str(uuid.uuid4())
	cancel_path = state.cancellation_path(event_id)
	if cancel_path.is_relative_to(root):
		raise ValueError("unsafe_path")
	plain_path(cancel_path)
	request = {
		"schema": state.BACKGROUND_SCHEMA,
		"mode": "background",
		"event_id": event_id,
		"root": str(root),
		"files": inputs,
		"cancel_path": str(cancel_path),
		"deadline_unix_ms": int((time() + state.WORK_SECONDS) * 1000),
		"baseline": {"policy_release": settings["baseline_release"]},
	}
	return {
		"schema": 1,
		"checks": checks,
		"request": request,
		"settings_path": str(settings_path.absolute()),
		"settings": settings,
		"session_id": session_id,
		"audiences": audiences,
		"acknowledged": [],
		"retry": retry,
		"status": "pending",
		"wire": None,
		"report": None,
		"last_findings": _prior_signatures(previous, audiences) if previous else {},
	}


def _prior_signatures(
	previous: dict[str, Any] | None, audiences: list[str]
) -> dict[str, dict[str, Any]]:
	if previous is None:
		return {}
	result: dict[str, dict[str, Any]] = {}
	for audience in audiences:
		clean = _finding_state(previous.get("last_findings", {}).get(audience))
		if clean:
			result[audience] = clean
	return result


def _finding_state(value: object) -> dict[str, Any]:
	if isinstance(value, str):
		return {"types": [value]}
	if not isinstance(value, dict):
		return {}
	result: dict[str, Any] = {}
	for check, signatures in value.items():
		if check == "types":
			clean = _signature_list(signatures)
			if clean:
				result[check] = clean
		elif check in FILE_FINDING_CHECKS:
			paths = _path_signatures(signatures)
			if paths:
				result[check] = paths
	return result


def _signature_list(value: object) -> list[str]:
	if isinstance(value, str):
		return [value]
	if isinstance(value, list) and all(isinstance(item, str) for item in value):
		return value
	return []


def _path_signatures(value: object) -> dict[str, list[str]]:
	if not isinstance(value, dict):
		return {}
	result = {}
	for path, signatures in value.items():
		if not isinstance(path, str):
			continue
		clean = _signature_list(signatures)
		if clean:
			result[path] = clean
	return result


def schedule(
	root: Path,
	files: object,
	*,
	session: BackgroundSession,
	deadline: float,
	retry: int = 0,
	checks: object = None,
) -> dict[str, Any]:
	"""Retain unverified paths; a lease admits the replacement only after cleanup."""
	operation = "background_identity"
	try:
		audience = state.identity(session.session_id)
		operation = "background_settings"
		settings = load_settings(session.settings_path)["current"]
		selected = selected_checks(settings, checks)
		if not selected:
			return {"check": "types", "status": "not_applicable"}
		operation = "background_inputs"
		root = root.resolve(strict=True)
		inputs = snapshot_inputs(root, files, deadline)
		operation = "background_state"
		directory = state.owned_directory(session.state_dir, root)
		state.subscribe(session.state_dir, audience, root, deadline=deadline)
		document = prepare_job(
			root, inputs, directory, settings, session, retry, deadline, selected
		)
		operation = "background_launch"
		launch(directory, document["request"]["event_id"])
		return pending(document["checks"])
	except (OSError, ValueError, TypeError, KeyError) as error:
		return {
			"check": "types" if checks is None else "background",
			**failure(error, operation, "background_unavailable"),
			"root": str(root),
		}


def prepare_job(
	root: Path,
	inputs: list[dict[str, Any]],
	directory: Path,
	settings: dict[str, Any],
	session: BackgroundSession,
	retry: int,
	deadline: float,
	checks: list[str],
) -> dict[str, Any]:
	"""Hash outside the metadata lock, then compare the generation before publishing."""
	from python_types.freshness import check_deadline

	path = directory / "latest.json"
	while True:
		check_deadline(deadline)
		with directory_transaction(directory):
			previous = state.read(path, deadline=deadline) if path.exists() else None
		paths = set(input_identities(inputs))
		job_checks = list(checks)
		if previous and set(previous["audiences"]) - set(previous["acknowledged"]):
			paths.update(item["path"] for item in previous["request"]["files"])
			previous_checks = previous.get("checks", ["types"])
			selected_checks = set(job_checks)
			selected_checks.update(
				name for name in previous_checks if check_enabled(settings, name)
			)
			job_checks = [name for name in state.BACKGROUND_CHECK_ORDER if name in selected_checks]
		current = current_inputs(root, sorted(paths), deadline)
		with directory_transaction(directory):
			latest = state.read(path, deadline=deadline) if path.exists() else None
			if latest != previous:
				continue
			document = queue_document(
				root,
				current,
				previous,
				settings,
				session.settings_path,
				str(session.session_id),
				retry,
				job_checks,
			)
			document["origin_event"] = scope(session.origin_event).get("event", {})
			state.write(path, document)
			return document


def acknowledge(
	directory: Path,
	event_id: str,
	audience: str,
	signatures: dict[str, dict[str, list[str]] | list[str] | None],
	*,
	deadline: float | None = None,
) -> bool:
	with directory_transaction(directory):
		document = state.read(directory / "latest.json", deadline=deadline)
		if document["request"]["event_id"] != event_id:
			return False
		if audience not in document["acknowledged"]:
			document["acknowledged"].append(audience)
		last = _finding_state(document["last_findings"].get(audience))
		for check, value in signatures.items():
			if value is None:
				last.pop(check, None)
			elif check in FILE_FINDING_CHECKS:
				if not isinstance(value, dict):
					last.pop(check, None)
					continue
				paths = _path_signatures(last.get(check))
				for path, current in value.items():
					if current:
						paths[path] = current
					else:
						paths.pop(path, None)
				if paths:
					last[check] = paths
				else:
					last.pop(check, None)
			else:
				last[check] = _signature_list(value)
		document["last_findings"][audience] = last
		state.write(directory / "latest.json", document)
		return True


def _finding_key(check: str, finding: object) -> str:
	return state.key(
		json.dumps({"check": check, "finding": finding}, sort_keys=True, ensure_ascii=True)
	)


def _ruff_finding_identities(report: dict[str, Any]) -> list[object]:
	identities: list[object] = []
	for item in report.get("files", []):
		if not isinstance(item, dict) or item.get("status") != "failed":
			continue
		path = item.get("path")
		if item.get("format") == "failed":
			identities.append({"path": path, "kind": "format"})
		identities.extend(
			{"path": path, "kind": "lint", "finding": finding}
			for finding in item.get("findings", [])
			if isinstance(finding, dict)
		)
	return identities


def _secret_finding_identities(report: dict[str, Any]) -> list[object]:
	return [
		{"path": item.get("path"), "kind": "secret", "finding": finding}
		for item in report.get("files", [])
		if isinstance(item, dict)
		for finding in item.get("findings", [])
		if isinstance(finding, dict) and finding.get("status") == "failed"
	]


def _finding_signature(
	check: str, report: dict[str, Any], input_paths: list[str] | None = None
) -> dict[str, list[str]] | list[str] | None:
	if check in FILE_FINDING_CHECKS:
		paths = set(input_paths or [])
		paths.update(
			item["path"]
			for item in report.get("files", [])
			if isinstance(item, dict) and isinstance(item.get("path"), str)
		)
		findings_by_path: dict[str, list[str]] = {path: [] for path in paths}
		if report.get("status") == "failed":
			for finding in _reader_finding_identities(check, report):
				path = finding.get("path") if isinstance(finding, dict) else None
				if isinstance(path, str):
					findings_by_path.setdefault(path, []).append(_finding_key(check, finding))
		return {path: sorted(set(values)) for path, values in findings_by_path.items()}
	if report.get("status") != "failed":
		return None
	# Preserve the existing aggregate behavior for the shared Types check.
	return [_finding_key(check, {"findings": report.get("findings", [])})]


def _reader_finding_identities(check: str, report: dict[str, Any]) -> list[object]:
	if check == "ruff":
		return _ruff_finding_identities(report)
	if check == "secrets":
		return _secret_finding_identities(report)
	return [item for item in report.get("files", []) if isinstance(item, dict)]


def _new_ruff_file(
	item: dict[str, Any], new_signatures: dict[str, set[str]]
) -> dict[str, Any] | None:
	path = item.get("path")
	path_signatures = new_signatures.get(path, set()) if isinstance(path, str) else set()
	findings = []
	for finding in item.get("findings", []):
		if not isinstance(finding, dict):
			continue
		identity = {"path": path, "kind": "lint", "finding": finding}
		if _finding_key("ruff", identity) in path_signatures:
			findings.append(finding)
	new_format = (
		item.get("format") == "failed"
		and _finding_key("ruff", {"path": path, "kind": "format"}) in path_signatures
	)
	if not findings and not new_format:
		return None
	filtered = dict(item)
	filtered["findings"] = findings
	if not new_format and filtered.get("format") == "failed":
		filtered.pop("format")
	return filtered


def _new_ruff_report(report: dict[str, Any], new_signatures: dict[str, set[str]]) -> dict[str, Any]:
	visible = dict(report)
	visible["files"] = [
		filtered
		for item in report.get("files", [])
		if isinstance(item, dict)
		if (filtered := _new_ruff_file(item, new_signatures)) is not None
	]
	return visible


def _new_secret_file(
	item: dict[str, Any], new_signatures: dict[str, set[str]]
) -> dict[str, Any] | None:
	path = item.get("path")
	path_signatures = new_signatures.get(path, set()) if isinstance(path, str) else set()
	findings = []
	for finding in item.get("findings", []):
		if not isinstance(finding, dict) or finding.get("status") != "failed":
			continue
		identity = {"path": path, "kind": "secret", "finding": finding}
		if _finding_key("secrets", identity) in path_signatures:
			findings.append(finding)
	if not findings:
		return None
	filtered = dict(item)
	filtered["findings"] = findings
	return filtered


def _new_secret_report(
	report: dict[str, Any], new_signatures: dict[str, set[str]]
) -> dict[str, Any]:
	visible = dict(report)
	visible["files"] = [
		filtered
		for item in report.get("files", [])
		if isinstance(item, dict)
		if (filtered := _new_secret_file(item, new_signatures)) is not None
	]
	return visible


def _new_builtin_report(
	report: dict[str, Any], new_signatures: dict[str, set[str]]
) -> dict[str, Any]:
	visible = dict(report)
	visible["files"] = [
		item
		for item in report.get("files", [])
		if isinstance(item, dict)
		and _finding_key(report["check"], item) in new_signatures.get(item.get("path", ""), set())
	]
	return visible


def _new_findings_report(
	check: str, report: dict[str, Any], new_signatures: dict[str, set[str]]
) -> dict[str, Any]:
	if check == "ruff":
		return _new_ruff_report(report, new_signatures)
	if check == "secrets":
		return _new_secret_report(report, new_signatures)
	if check in {"builtin_secrets", "legacy_security"}:
		return _new_builtin_report(report, new_signatures)
	return dict(report)


def _visible_report(
	report: dict[str, Any],
	signature: dict[str, list[str]] | list[str] | None,
	previous: object,
	include_clean: bool,
) -> dict[str, Any] | None:
	name = report["check"]
	prior = _signature_list(previous)
	repeated = isinstance(signature, list) and signature == prior
	if report["status"] == "failed" and not include_clean:
		if name in FILE_FINDING_CHECKS:
			if not isinstance(signature, dict):
				return report
			previous_by_path = _path_signatures(previous)
			new_signatures = {
				path: set(current) - set(previous_by_path.get(path, []))
				for path, current in signature.items()
			}
			new_signatures = {path: values for path, values in new_signatures.items() if values}
			if not new_signatures:
				return None
			return _new_findings_report(name, report, new_signatures)
		if repeated and name == "types":
			return {
				"check": "types",
				"status": "failed",
				"reason": "repeated_findings",
				"findings": [],
			}
		if repeated:
			return None
	if report["status"] in {"passed", "not_applicable"} and not include_clean:
		return None
	return report


def _fresh_inputs(request: dict[str, Any], deadline: float) -> None:
	root = Path(request["root"])
	paths = [item["path"] for item in request["files"]]
	current = current_inputs(root, paths, deadline)
	if input_identities(current) != input_identities(request["files"]):
		raise ValueError("stale_result")


def _fresh_validation_inputs(
	check: str, report: dict[str, Any], request: dict[str, Any], deadline: float
) -> None:
	inputs = report.get("inputs")
	if inputs is None:
		if report.get("status") in {"passed", "failed", "waived"}:
			raise ValueError("invalid_provider_result")
		return
	root = Path(request["root"])
	if check == "ruff":
		from python_quality.check import validated_inputs

		current = validated_inputs(inputs, root, deadline)
	elif check == "secrets":
		from secret_scan.results import configuration_inputs

		current = configuration_inputs(inputs, root, deadline)
	else:
		return
	if current != inputs:
		raise ValueError("stale_result")


def _validation_error_reason(
	error: OSError | ValueError | TypeError | KeyError | RuntimeError,
) -> str:
	if isinstance(error, OSError):
		return "stale_result" if isinstance(error, FileNotFoundError) else "io"
	reason = str(error)
	if reason not in {
		"cancelled",
		"deadline",
		"invalid_provider_result",
		"io",
		"limits",
		"stale_input",
		"stale_result",
		"unsafe_path",
	}:
		return "stale_result"
	return reason


def _fresh_completed_report(
	check: str, report: dict[str, Any], request: dict[str, Any], deadline: float
) -> dict[str, Any]:
	if report.get("status") not in {"passed", "failed", "not_applicable", "waived"}:
		return report
	try:
		_fresh_inputs(request, deadline)
		_fresh_validation_inputs(check, report, request, deadline)
	except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
		return {
			"check": check,
			"status": "unchecked",
			"reason": _validation_error_reason(error),
		}
	return report


def _validate_builtin(report: object, request: dict[str, Any], deadline: float) -> dict[str, Any]:
	from .quality_legacy_security import MAX_BUILTIN_FINDINGS

	if (
		not isinstance(report, dict)
		or report.get("check") != "builtin_secrets"
		or set(report) not in ({"check", "status", "files"}, {"check", "status", "reason", "files"})
	):
		return {
			"check": "builtin_secrets",
			"status": "unchecked",
			"reason": "invalid_provider_result",
		}
	if report.get("status") == "unchecked":
		if report.get("reason") == "security_unavailable":
			return {
				"check": "builtin_secrets",
				"status": "unchecked",
				"reason": "security_unavailable",
			}
		return {
			"check": "builtin_secrets",
			"status": "unchecked",
			"reason": "invalid_provider_result",
		}
	try:
		_fresh_inputs(request, deadline)
		expected = {item["path"] for item in request["files"] if item["sha256"] is not None}
		files = report.get("files")
		if (
			report.get("status") not in {"passed", "failed"}
			or not isinstance(files, list)
			or len(files) > MAX_BUILTIN_FINDINGS
			or any(
				not isinstance(item, dict)
				or set(item) != {"path", "line", "check_id"}
				or item.get("path") not in expected
				or type(item.get("line")) is not int
				or not 0 < item["line"] <= 2**31 - 1
				or item.get("check_id") != "secrets.builtin"
				for item in files
			)
			or (report["status"] == "failed") != bool(files)
		):
			raise ValueError("invalid_provider_result")
		require_unique = {(item["path"], item["line"]) for item in files}
		if len(require_unique) != len(files):
			raise ValueError("invalid_provider_result")
		return {"check": "builtin_secrets", "status": report["status"], "files": files}
	except (OSError, ValueError, TypeError, KeyError) as error:
		return {
			"check": "builtin_secrets",
			"status": "unchecked",
			"reason": _validation_error_reason(error),
		}


def _not_applicable(report: object, check: str, has_files: bool) -> dict[str, Any] | None:
	if not isinstance(report, dict) or report.get("status") != "not_applicable":
		return None
	valid = report.get("schema") == 1 and report.get("files") == []
	if check == "ruff":
		valid = (
			valid
			and set(report)
			== {"schema", "status", "reason", "files", "phase", "writes_may_have_occurred"}
			and not has_files
			and report.get("phase") == "validate"
			and report.get("writes_may_have_occurred") is False
			and report.get("reason") == "no_python_files"
		)
	else:
		valid = (
			valid
			and set(report)
			== {
				"schema",
				"check",
				"check_id",
				"status",
				"reason",
				"files",
				"selections",
				"inputs",
				"writes_may_have_occurred",
			}
			and not has_files
			and report.get("check") == "secrets"
			and report.get("check_id") == "secrets.candidate"
			and report.get("writes_may_have_occurred") is False
			and report.get("reason") == "no_files"
		)
	return {"check": check, "status": "not_applicable"} if valid else None


def _fresh_not_applicable(
	report: object,
	check: str,
	has_files: bool,
	request: dict[str, Any],
	deadline: float,
) -> dict[str, Any] | None:
	not_applicable = _not_applicable(report, check, has_files)
	if not_applicable is None:
		return None
	return _fresh_completed_report(check, not_applicable, request, deadline)


def _unchecked_reports(checks: list[str], reason: str, root: str) -> list[dict[str, Any]]:
	names = [*checks, *(["builtin_secrets"] if "secrets" in checks else [])]
	reports = []
	for name in names:
		if name == "types":
			report = unchecked(reason)
			report["root"] = root
			report["diagnostic"] = {"operation": "background_collect", "phase": "background"}
			reports.append(report)
		else:
			reports.append(
				{
					"check": name,
					"status": "unchecked",
					"reason": reason,
					"root": root,
					"phase": "validate",
					"writes_may_have_occurred": False,
					"diagnostic": {"operation": "background_collect", "phase": "background"},
				}
			)
	return reports


def _composite_reports(
	checks: list[str],
	wire: object,
	request: dict[str, Any],
	settings: dict[str, Any],
	deadline: float,
	inventory: dict[str, Any],
) -> list[dict[str, Any]]:
	root = request["root"]
	results = wire.get("results") if isinstance(wire, dict) and wire.get("schema") == 1 else None
	if not isinstance(results, dict):
		results = {"types": {"wire": wire}}
	release_inventories = {settings["provider_release"]: inventory}
	reports = []
	for name in checks:
		report = _validate_check(
			name, results.get(name), request, settings, deadline, release_inventories
		)
		report.update(check=name, root=root)
		reports.append(report)
		if name == "secrets":
			entry = results.get("builtin_secrets")
			builtin = _validate_builtin(
				entry.get("report") if isinstance(entry, dict) else None,
				request,
				deadline,
			)
			builtin["root"] = root
			reports.append(builtin)
	return reports


def _completed_reports(
	document: dict[str, Any], settings: dict[str, Any], deadline: float
) -> list[dict[str, Any]]:
	request = document["request"]
	checks = document.get("checks", ["types"])
	root = request["root"]
	if document["settings"] != settings:
		state.signal_cancel(Path(request["cancel_path"]))
		active_checks = [name for name in checks if check_enabled(settings, name)]
		return _unchecked_reports(active_checks, "settings_changed", root)
	if document["wire"] is None:
		failure = document["report"] or unchecked("background_unavailable")
		reports = _unchecked_reports(checks, failure.get("reason", "background_unavailable"), root)
		for report in reports:
			report["diagnostic"] = {
				**safe_diagnostic(failure.get("diagnostic")),
				**safe_diagnostic(
					{
						"operation": "background_worker",
						"phase": "background",
						"errno": document.get("failure_errno"),
						"winerror": document.get("failure_winerror"),
					}
				),
			}
			report["event"] = document.get("origin_event", {})
			report["background_event_id"] = request.get("event_id")
			report["input_paths"] = [
				item["path"]
				for item in request.get("inputs", [])
				if isinstance(item, dict) and "path" in item
			]
		return reports
	try:
		inventory = release_inventory(settings["provider_release"], request, deadline)
		if digest(inventory) != document.get("engine_identity"):
			return _unchecked_reports(checks, "release", root)
	except (OSError, ValueError, KeyError):
		return _unchecked_reports(checks, "release", root)
	return _composite_reports(checks, document["wire"], request, settings, deadline, inventory)


def _retry_if_stale(
	reports: list[dict[str, Any]],
	document: dict[str, Any],
	session: BackgroundSession,
	deadline: float,
) -> dict[str, Any] | None:
	if document["retry"] != 0 or not any(
		report.get("reason") in {"stale_result", "stale_input"} for report in reports
	):
		return None
	request = document["request"]
	root = Path(request["root"])
	return schedule(
		root,
		current_inputs(root, [item["path"] for item in request["files"]], deadline),
		session=BackgroundSession(session.settings_path, session.state_dir, document["session_id"]),
		deadline=deadline,
		retry=1,
		checks=document.get("checks", ["types"]),
	)


def _visible_reports(
	reports: list[dict[str, Any]],
	document: dict[str, Any],
	directory: Path,
	audience: str,
	include_clean: bool,
	*,
	deadline: float | None = None,
) -> list[dict[str, Any]] | None:
	previous = _finding_state(document.get("last_findings", {}).get(audience))
	checks = document.get("checks", ["types"])
	tracked_checks = [*checks, *(["builtin_secrets"] if "secrets" in checks else [])]
	input_paths = [item["path"] for item in document["request"].get("files", [])]
	signatures: dict[str, dict[str, list[str]] | list[str] | None] = {
		report["check"]: _finding_signature(report["check"], report, input_paths)
		for report in reports
		if report.get("status") in {"passed", "failed", "not_applicable", "waived"}
	}
	for name in set(tracked_checks) - {report["check"] for report in reports}:
		signatures[name] = None
	if not acknowledge(
		directory,
		document["request"]["event_id"],
		audience,
		signatures,
		deadline=deadline,
	):
		return [pending(document.get("checks", ["types"]))]
	visible = []
	for report in reports:
		name = report["check"]
		selected = _visible_report(report, signatures.get(name), previous.get(name), include_clean)
		if selected is not None:
			visible.append(selected)
	return visible or (reports if include_clean else None)


def deliver(
	directory: Path,
	document: dict[str, Any],
	audience: str,
	session: BackgroundSession,
	settings: dict[str, Any],
	deadline: float,
	include_clean: bool = False,
) -> list[dict[str, Any]] | None:
	reports = _completed_reports(document, settings, deadline)
	retry = _retry_if_stale(reports, document, session, deadline)
	if retry is not None:
		return [retry]
	for report in reports:
		if report.get("status") == "unchecked":
			report["input_paths"] = [item["path"] for item in document["request"]["files"]]
			report.setdefault("event", document.get("origin_event", {}))
			report.setdefault("background_event_id", document["request"]["event_id"])
			report.setdefault("phase", "validate")
			report["writes_may_have_occurred"] = False
			report.setdefault(
				"diagnostic", {"operation": "provider_validate", "phase": "background"}
			)
	return _visible_reports(
		reports, document, directory, audience, include_clean, deadline=deadline
	)


def _validate_check(
	name: str,
	entry: object,
	request: dict[str, Any],
	settings: dict[str, Any],
	deadline: float,
	release_inventories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	if not isinstance(entry, dict):
		return {"check": name, "status": "unchecked", "reason": "invalid_provider_result"}
	report, wire = entry.get("report"), entry.get("wire")
	if isinstance(report, dict) and report.get("status") == "unchecked" and "diagnostic" in report:
		clean = record({**report, "check": name})
		clean["writes_may_have_occurred"] = False
		return clean
	if name == "types":
		return _validate_types(report, wire, request, settings, deadline)
	if not isinstance(report, dict):
		return {"check": name, "status": "unchecked", "reason": "invalid_provider_result"}
	if name == "ruff":
		return _validate_ruff(report, request, settings, deadline, release_inventories)
	if name == "secrets":
		return _validate_secrets(report, request, settings, deadline, release_inventories)
	if name == "legacy_security":
		return _validate_legacy_security(report, request, deadline)
	return {"check": name, "status": "unchecked", "reason": "invalid_provider_result"}


def _validate_legacy_security(
	report: dict[str, Any], request: dict[str, Any], deadline: float
) -> dict[str, Any]:
	"""Accept only fresh, source-free heavy-scanner findings for requested files."""
	failure = {
		"check": "legacy_security",
		"status": "unchecked",
		"reason": "invalid_provider_result",
	}
	if report.get("check") != "legacy_security" or set(report) not in (
		{"check", "status", "files"},
		{"check", "status", "files", "reason"},
	):
		return failure
	if report.get("status") == "unchecked":
		if report.get("reason") == "security_unavailable" and report.get("files") == []:
			return {**failure, "reason": "security_unavailable"}
		return failure
	try:
		_fresh_inputs(request, deadline)
		expected = {item["path"] for item in request["files"] if item["sha256"] is not None}
		files = report.get("files")
		if (
			report.get("status") not in {"passed", "failed"}
			or not isinstance(files, list)
			or len(files) > len(expected)
			or any(
				not isinstance(item, dict)
				or set(item) != {"path"}
				or item.get("path") not in expected
				for item in files
			)
			or len({item["path"] for item in files}) != len(files)
			or (report["status"] == "failed") != bool(files)
		):
			return failure
		return {"check": "legacy_security", "status": report["status"], "files": files}
	except (OSError, ValueError, TypeError, KeyError) as error:
		return {**failure, "reason": _validation_error_reason(error)}


def _validate_types(
	report: object,
	wire: object,
	request: dict[str, Any],
	settings: dict[str, Any],
	deadline: float,
) -> dict[str, Any]:
	if wire is None:
		if (
			isinstance(report, dict)
			and set(report)
			== {
				"schema",
				"check",
				"status",
				"reason",
				"files",
				"findings",
				"selections",
				"writes_may_have_occurred",
			}
			and report.get("check") == "types"
			and report.get("status") == "unchecked"
			and report.get("reason") in TYPE_REASONS | {"stale_result"}
			and report.get("files") == []
			and report.get("findings") == []
			and report.get("selections") == []
			and report.get("writes_may_have_occurred") is False
		):
			return report
		return {"check": "types", "status": "unchecked", "reason": "invalid_provider_result"}
	return validate_result(
		json.dumps(wire),
		STATUS_EXIT.get(wire.get("status", ""), 2) if isinstance(wire, dict) else 2,
		request,
		settings["provider_release"],
		deadline,
	)


def _validate_ruff(
	report: dict[str, Any],
	request: dict[str, Any],
	settings: dict[str, Any],
	deadline: float,
	release_inventories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	from python_quality.check import REASONS as RUFF_REASONS
	from python_quality.check import STATUS_EXIT as RUFF_STATUS_EXIT
	from python_quality.check import unchecked as unchecked_ruff
	from python_quality.check import validate_result as validate_ruff

	reason = report.get("reason")
	if (
		report.get("status") == "unchecked"
		and isinstance(reason, str)
		and reason in RUFF_REASONS | {"stale_result"}
		and report == unchecked_ruff(reason, phase="validate")
	):
		return {"check": "ruff", "status": "unchecked", "reason": reason}
	files = [
		item
		for item in request["files"]
		if item["sha256"] is not None and Path(item["path"]).suffix.lower() in {".py", ".pyi"}
	]
	not_applicable = _fresh_not_applicable(report, "ruff", bool(files), request, deadline)
	if not_applicable is not None:
		return not_applicable
	validation_request = {
		"schema": 1,
		"root": request["root"],
		"files": files,
		"phase": "validate",
	}
	validated = validate_ruff(
		json.dumps(report),
		RUFF_STATUS_EXIT.get(report.get("status", ""), 2),
		validation_request,
		settings["provider_release"],
		phase="validate",
	)
	bound = _release_bound(validated, request, deadline, "ruff", release_inventories)
	if bound.get("reason") is not None:
		return bound
	try:
		for selection in bound.get("selections", []):
			python = Path(selection["python"])
			if not _ruff_python_path_is_current(selection, python, request, settings):
				return {"status": "unchecked", "reason": "runtime"}
			plain_path(python.parent)
			actual, _ = file_identity(python.resolve(strict=True), deadline, 64 * 1024 * 1024)
			if actual != selection["python_sha256"]:
				return {"status": "unchecked", "reason": "runtime"}
	except (OSError, ValueError, KeyError):
		return {"status": "unchecked", "reason": "runtime"}
	return _fresh_completed_report("ruff", bound, request, deadline)


def _ruff_python_path_is_current(
	selection: dict[str, Any], python: Path, request: dict[str, Any], settings: dict[str, Any]
) -> bool:
	if not python.is_absolute() or ".." in python.parts:
		return False
	baseline = settings.get("baseline_python")
	if isinstance(baseline, str) and python == Path(baseline):
		return True
	root = Path(request["root"])
	root_key = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:32]
	runtime = gate_cache_root(request) / "runtimes" / root_key / selection["runtime_fingerprint"]
	executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
	return python == runtime / executable


def _validate_secrets(
	report: dict[str, Any],
	request: dict[str, Any],
	settings: dict[str, Any],
	deadline: float,
	release_inventories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	from secret_scan.results import REASONS as SECRET_REASONS
	from secret_scan.results import STATUS_EXIT as SECRET_STATUS_EXIT
	from secret_scan.results import unchecked as unchecked_secrets
	from secret_scan.results import validate_result as validate_secrets

	reason = report.get("reason")
	if (
		report.get("status") == "unchecked"
		and isinstance(reason, str)
		and reason in SECRET_REASONS | {"stale_result"}
		and report == unchecked_secrets(reason)
	):
		return {"check": "secrets", "status": "unchecked", "reason": reason}
	validation_request = {
		"schema": 1,
		"root": request["root"],
		"files": [item for item in request["files"] if item["sha256"] is not None],
		"baseline": {"policy_release": settings["baseline_release"]},
	}
	not_applicable = _fresh_not_applicable(
		report, "secrets", bool(validation_request["files"]), request, deadline
	)
	if not_applicable is not None:
		return not_applicable
	validated = validate_secrets(
		json.dumps(report),
		SECRET_STATUS_EXIT.get(report.get("status", ""), 2),
		validation_request,
		settings["provider_release"],
		deadline,
	)
	bound = _release_bound(validated, request, deadline, "secrets", release_inventories)
	return _fresh_completed_report("secrets", bound, request, deadline)


def _release_bound(
	report: dict[str, Any],
	request: dict[str, Any],
	deadline: float,
	check: str,
	release_inventories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	if report.get("reason") is not None:
		return report
	selections = report.get("selections")
	if not isinstance(selections, list):
		return {"status": "unchecked", "reason": "invalid_provider_result"}
	try:
		for selection in selections:
			version = selection["policy_release"]
			inventory = release_inventories.get(version)
			if inventory is None:
				inventory = release_inventory(
					version,
					request,
					deadline,
					expected_manifest=selection["release_sha256"],
				)
				release_inventories[version] = inventory
			if inventory["sha256"] != selection["release_sha256"]:
				return {"status": "unchecked", "reason": "release"}
			if check == "ruff":
				tool_name, version_key, digest_key = "ruff", "ruff_version", "ruff_sha256"
				if (
					inventory["files"].get("quality_gate/policy/ruff.toml")
					!= selection["policy_sha256"]
				):
					return {"status": "unchecked", "reason": "release"}
			else:
				tool_name, version_key, digest_key = "gitleaks", "scanner_version", "scanner_sha256"
			if not _release_tool_matches(
				inventory, selection, check, tool_name, version_key, digest_key
			):
				return {"status": "unchecked", "reason": "release"}
	except (OSError, ValueError, TypeError, KeyError, RuntimeError):
		return {"status": "unchecked", "reason": "release"}
	return report


def _release_tool_matches(
	inventory: dict[str, Any],
	selection: dict[str, Any],
	check: str,
	tool_name: str,
	version_key: str,
	digest_key: str,
) -> bool:
	tools = [item for item in inventory.get("tools", []) if item.get("name") == tool_name]
	if len(tools) != 1:
		return False
	tool = tools[0]
	tool_path = tool.get("path")
	tool_sha256 = tool.get("sha256")
	if (
		tool.get("version") != selection[version_key]
		or not isinstance(tool_path, str)
		or inventory["files"].get(tool_path) != tool_sha256
	):
		return False
	# Ruff reports the extracted binary digest; its release inventory binds the wheel.
	return check == "ruff" or tool_sha256 == selection[digest_key]


def collect_root(
	directory: Path,
	audience: str,
	session: BackgroundSession,
	settings: dict[str, Any],
	deadline: float,
	drain: bool,
	include_clean: bool = False,
) -> list[dict[str, Any]] | None:
	while True:
		document = state.read(directory / "latest.json", deadline=deadline)
		if audience not in document["audiences"] or audience in document["acknowledged"]:
			return None
		if document["settings"] != settings:
			state.signal_cancel(Path(document["request"]["cancel_path"]))
		if document["status"] == "pending":
			if not drain:
				return None
			if (
				monotonic() >= deadline
				or time() * 1000
				> document["request"]["deadline_unix_ms"] + state.CLEANUP_SECONDS * 1000
			):
				state.signal_cancel(Path(document["request"]["cancel_path"]))
				return [
					{
						"check": "background",
						"status": "unchecked",
						"reason": "deadline",
						"diagnostic": {"operation": "background_collect", "phase": "background"},
					}
				]
			sleep(state.POLL_SECONDS)
			continue
		reports = deliver(
			directory,
			document,
			audience,
			session,
			settings,
			min(deadline, monotonic() + state.DELIVERY_SECONDS),
			include_clean,
		)
		if reports is not None and any(report["status"] == "pending" for report in reports):
			if drain:
				continue
			return None
		return reports


def _collection_failure(error: OSError | ValueError | TypeError | KeyError) -> dict[str, Any]:
	if isinstance(error, state.DeadlineError):
		reason = "deadline"
	elif isinstance(error, StorageBusyError):
		reason = "background_state_busy"
	elif isinstance(error, OSError):
		reason = "background_state_io"
	else:
		reason = (
			"background_state_links"
			if str(error) == "background_state_links"
			else "invalid_background_state"
		)
	reason = failure(error, "background_collect", reason)["reason"]
	result = unchecked(reason)
	if isinstance(error, state.DeadlineError):
		result["check"] = "background"
	result["diagnostic"] = {
		**failure(error, "background_collect", reason)["diagnostic"],
		"phase": "background",
	}
	return result


def _cancellation_failure(error: OSError | ValueError | TypeError | KeyError) -> dict[str, Any]:
	if isinstance(error, ValueError) and str(error) == "process_cleanup":
		return unchecked("process_cleanup")
	return _collection_failure(error)


def collect(
	*,
	session: BackgroundSession,
	deadline: float,
	drain: bool = False,
	include_clean: bool = False,
) -> list[dict[str, Any]]:
	"""Deliver only at an active boundary, after independent current-file validation."""
	reports: list[dict[str, Any]] = []
	try:
		audience = state.identity(session.session_id)
		settings = load_settings(session.settings_path)["current"]
		roots = state.roots_for(session.state_dir, audience, deadline=deadline)
	except (OSError, ValueError, TypeError, KeyError) as error:
		return [_collection_failure(error)]
	for root in roots:
		if monotonic() >= deadline:
			reports.append(
				{
					"check": "background",
					"status": "unchecked",
					"reason": "deadline",
					"diagnostic": {"operation": "background_collect", "phase": "background"},
				}
			)
			break
		try:
			directory = state.owned_directory(session.state_dir, root)
			report = collect_root(
				directory, audience, session, settings, deadline, drain, include_clean
			)
		except (OSError, ValueError, TypeError, KeyError) as error:
			reports.append(_collection_failure(error))
			if isinstance(error, state.DeadlineError):
				break
			continue
		if report is not None:
			reports.extend(report)
	return reports


def cancel_root(directory: Path, deadline: float, *, event_id: str | None = None) -> None:
	"""Retire metadata after the root's supervised analysis has released ownership."""
	document = state.read(directory / "latest.json", deadline=deadline)
	if event_id is not None and document["request"]["event_id"] != event_id:
		return
	state.signal_cancel(Path(document["request"]["cancel_path"]))
	with state.reader_lease(directory, deadline) as acquired:
		if not acquired:
			raise ValueError("process_cleanup")
		with directory_transaction(directory):
			latest = state.read(directory / "latest.json", deadline=deadline)
			if latest["request"]["event_id"] != document["request"]["event_id"]:
				raise ValueError("concurrent_edit")
			latest.update(
				status="retired",
				wire=None,
				report=unchecked("cancelled"),
				acknowledged=list(latest["audiences"]),
			)
			state.write(directory / "latest.json", latest)
		Path(document["request"]["cancel_path"]).unlink(missing_ok=True)


def cancel_session(*, session: BackgroundSession, deadline: float) -> list[dict[str, Any]]:
	try:
		audience = state.identity(session.session_id)
		roots = state.roots_for(session.state_dir, audience, deadline=deadline)
	except (OSError, ValueError, TypeError, KeyError) as error:
		return [_collection_failure(error)]
	reports: list[dict[str, Any]] = []
	for root in roots:
		if monotonic() >= deadline:
			reports.append(
				{
					"check": "background",
					"status": "unchecked",
					"reason": "deadline",
					"diagnostic": {"operation": "background_collect", "phase": "background"},
				}
			)
			break
		try:
			directory = state.owned_directory(session.state_dir, root)
			with directory_transaction(directory):
				document = state.read(directory / "latest.json", deadline=deadline)
				if audience in document["audiences"] and audience not in document["acknowledged"]:
					document["acknowledged"].append(audience)
					state.write(directory / "latest.json", document)
				waiting = set(document["audiences"]) - set(document["acknowledged"])
			if not waiting:
				cancel_root(directory, deadline, event_id=document["request"]["event_id"])
		except (OSError, ValueError, TypeError, KeyError) as error:
			reports.append(_cancellation_failure(error))
	return reports


def cancel_all(*, settings_path: Path, state_dir: Path, deadline: float) -> None:
	"""Rollback owns only jobs launched from this exact settings document."""
	root = state_dir / "background" / "roots"
	if not root.exists():
		return
	plain_path(root)
	for path in root.glob("*/latest.json"):
		if monotonic() >= deadline:
			raise state.DeadlineError("deadline")
		document = state.read(path, deadline=deadline)
		if document["settings_path"] == str(settings_path.absolute()):
			cancel_root(path.parent, deadline)
