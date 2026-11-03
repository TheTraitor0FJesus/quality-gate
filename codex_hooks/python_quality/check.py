"""Host Ruff entry point consuming the released public edit-check protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from threading import Event
from time import monotonic, time
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_diagnostics import failure
from _shared.quality_events import MAX_FILE_BYTES, AttributionError, safe_path
from _shared.quality_process import ProcessError, execute
from _shared.quality_settings import (
	DEFAULT_BUDGET_SECONDS,
	RELEASE,
	SettingsError,
	default_settings_path,
	load_settings,
)

DIGEST = re.compile(r"^[0-9a-f]{64}$")
RULE = re.compile(r"^(?:[A-Z][A-Z0-9]{0,15}|invalid-syntax)$")
STATUS_EXIT = {"passed": 0, "failed": 1, "not_applicable": 0, "unchecked": 2}
REASONS = frozenset(
	{
		"invalid_request",
		"deadline",
		"unsafe_path",
		"limits",
		"stale_input",
		"manifest",
		"unsupported_policy",
		"release",
		"runtime",
		"tool",
		"configuration",
		"tool_output",
		"io",
	}
)
MAX_RESULT_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_FINDINGS = 200
MAX_INPUTS = 4096
MAX_INPUT_BYTES = 256 * 1024
MAX_INPUT_TOTAL_BYTES = 1024 * 1024
MAX_FILES = 10


def unchecked(reason: str, *, phase: str | None = None) -> dict[str, Any]:
	result = {
		"schema": 1,
		"status": "unchecked",
		"reason": reason,
		"files": [],
		"selections": [],
		"writes_may_have_occurred": phase != "validate",
	}
	if phase is not None:
		result["phase"] = phase
	return result


def not_applicable(reason: str, *, phase: str | None = None) -> dict[str, Any]:
	result = {"schema": 1, "status": "not_applicable", "reason": reason, "files": []}
	if phase is not None:
		result.update(phase=phase, writes_may_have_occurred=False)
	return result


def is_digest(value: object) -> bool:
	return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def require(condition: bool) -> None:
	if not condition:
		raise ValueError("invalid_provider_result")


def validated_selection(selection: object) -> dict[str, Any]:
	if not isinstance(selection, dict):
		raise ValueError("invalid_provider_result")
	for name in (
		"release_sha256",
		"policy_sha256",
		"ruff_sha256",
		"python_sha256",
		"runtime_fingerprint",
	):
		require(is_digest(selection.get(name)))
	require(
		isinstance(selection.get("policy_release"), str)
		and RELEASE.fullmatch(selection["policy_release"]) is not None
	)
	require(isinstance(selection.get("python"), str) and Path(selection["python"]).is_absolute())
	for name in ("ruff_version", "python_version"):
		require(
			isinstance(selection.get(name), str)
			and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", selection[name]) is not None
		)
	return {
		name: selection[name]
		for name in (
			"policy_release",
			"release_sha256",
			"policy_sha256",
			"ruff_version",
			"ruff_sha256",
			"python",
			"python_version",
			"python_sha256",
			"runtime_fingerprint",
		)
	}


def validated_findings(findings: object) -> list[dict[str, Any]]:
	if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
		raise ValueError("invalid_provider_result")
	clean = []
	for finding in findings:
		require(isinstance(finding, dict) and finding.get("check_id") == "ruff")
		require(
			isinstance(finding.get("rule"), str) and RULE.fullmatch(finding["rule"]) is not None
		)
		for name in ("line", "column"):
			require(type(finding.get(name)) is int and 0 < finding[name] <= 2**31 - 1)
		clean.append({name: finding[name] for name in ("check_id", "rule", "line", "column")})
	return clean


def validated_file(
	item: object,
	expected: dict[str, str],
	root: Path,
	selections: list[dict[str, Any]],
	*,
	phase: str | None = None,
) -> dict[str, Any]:
	if not isinstance(item, dict):
		raise ValueError("invalid_provider_result")
	path = item.get("path")
	if not isinstance(path, str) or path not in expected:
		raise ValueError("invalid_provider_result")
	require(item.get("status") in {"passed", "failed", "not_applicable"})
	require(item.get("input_sha256") == expected[path])
	if item["status"] == "not_applicable":
		require(item.get("sha256") == expected[path] and item.get("changed") is False)
		full, _ = safe_path(path, root)
		with full.open("rb") as stream:
			content = stream.read(MAX_FILE_BYTES + 1)
		require(len(content) <= MAX_FILE_BYTES)
		if hashlib.sha256(content).hexdigest() != expected[path]:
			raise RuntimeError("stale_result")
		return {
			"path": path,
			"status": "not_applicable",
			"input_sha256": expected[path],
			"sha256": expected[path],
			"changed": False,
			"findings": [],
		}
	require(is_digest(item.get("sha256")))
	require(type(item.get("changed")) is bool)
	if phase == "validate":
		require(item["changed"] is False and item["sha256"] == expected[path])
	require(item.get("coverage") in {"managed", "baseline"})
	component = item.get("component")
	require(
		(component is None and item["coverage"] == "baseline")
		or (isinstance(component, str) and bool(component) and item["coverage"] == "managed")
	)
	require(item.get("format") in {"passed", "failed"})
	require(type(item.get("selection")) is int and 0 <= item["selection"] < len(selections))
	full, _ = safe_path(path, root)
	with full.open("rb") as stream:
		content = stream.read(MAX_FILE_BYTES + 1)
	require(len(content) <= MAX_FILE_BYTES)
	if hashlib.sha256(content).hexdigest() != item["sha256"]:
		raise RuntimeError("stale_result")
	findings = validated_findings(item.get("findings"))
	require((item["status"] == "failed") == bool(findings or item["format"] == "failed"))
	return {
		"path": path,
		"status": item["status"],
		"sha256": item["sha256"],
		"input_sha256": item["input_sha256"],
		"changed": item["changed"],
		"coverage": item["coverage"],
		"component": component,
		"selection": item["selection"],
		"format": item["format"],
		"findings": findings,
	}


def validated_inputs(
	inputs: object, root: Path, deadline: float | None = None
) -> list[dict[str, Any]]:
	if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
		raise ValueError("invalid_provider_result")
	clean = []
	total = 0
	for item in inputs:
		if deadline is not None and monotonic() >= deadline:
			raise RuntimeError("deadline")
		require(isinstance(item, dict) and isinstance(item.get("path"), str))
		require(not Path(item["path"]).is_absolute())
		path, _ = safe_path(item["path"], root)
		require(item.get("sha256") is None or is_digest(item.get("sha256")))
		if item["sha256"] is None:
			if path.exists():
				raise RuntimeError("stale_result")
		else:
			with path.open("rb") as stream:
				content = stream.read(MAX_INPUT_BYTES + 1)
			if deadline is not None and monotonic() >= deadline:
				raise RuntimeError("deadline")
			total += len(content)
			if len(content) > MAX_INPUT_BYTES or total > MAX_INPUT_TOTAL_BYTES:
				if deadline is not None:
					raise RuntimeError("stale_result")
				require(False)
			if hashlib.sha256(content).hexdigest() != item["sha256"]:
				raise RuntimeError("stale_result")
		clean.append({"path": item["path"], "sha256": item["sha256"]})
	return clean


def validate_request_phase(request: dict[str, Any], phase: str | None) -> None:
	require(phase in {None, "write", "validate"})
	if phase is None:
		require("phase" not in request)
	else:
		require(request.get("phase") == phase)


def validate_response_phase(result: dict[str, Any], phase: str | None) -> None:
	if phase is None:
		require("phase" not in result)
	elif phase == "validate":
		require(
			result.get("phase") == "validate" and result.get("writes_may_have_occurred") is False
		)
	else:
		require(
			result.get("phase") == "write" and type(result.get("writes_may_have_occurred")) is bool
		)


def validate_write_outcome(
	status: str, provider_report: dict[str, Any], files: list[dict[str, Any]]
) -> None:
	require(status != "failed")
	require(provider_report["writes_may_have_occurred"] == any(item["changed"] for item in files))


def validate_result(
	output: str,
	exit_code: int,
	request: dict[str, Any],
	engine_release: str,
	*,
	phase: str | None = None,
) -> dict[str, Any]:
	"""Accept only the requested final content and source-free provider metadata."""
	try:
		validate_request_phase(request, phase)
		require(len(output.encode("utf-8")) <= MAX_RESULT_BYTES)
		result = json.loads(output)
		require(
			isinstance(result, dict) and type(result.get("schema")) is int and result["schema"] == 1
		)
		validate_response_phase(result, phase)
		status = result.get("status")
		require(isinstance(status, str) and STATUS_EXIT.get(status) == exit_code)
		if status == "unchecked":
			# Transport failures can precede selection and omit success metadata.
			require(result.get("reason") in REASONS)
			return unchecked(result["reason"], phase=phase)
		require(result.get("engine_version") == engine_release.removeprefix("v"))
		root = Path(request["root"]).resolve()
		require(result.get("root") == str(root))
		require(result.get("reason") is None)
		require(isinstance(result.get("files"), list) and len(result["files"]) <= MAX_FILES)
		require(
			isinstance(result.get("selections"), list) and len(result["selections"]) <= MAX_FILES
		)
		selections = [validated_selection(item) for item in result["selections"]]
		expected = {item["path"]: item["sha256"] for item in request["files"]}
		files = [
			validated_file(item, expected, root, selections, phase=phase)
			for item in result["files"]
		]
		require(len(files) == len(expected) and {item["path"] for item in files} == set(expected))
		states = {item["status"] for item in files}
		require((status == "failed") == ("failed" in states))
		require((status == "not_applicable") == (not states or states == {"not_applicable"}))
		inputs = validated_inputs(result.get("inputs"), root)
		for name in ("process_count", "elapsed_ms"):
			require(type(result.get(name)) is int and 0 <= result[name] <= 2**31 - 1)
		require(type(result.get("writes_may_have_occurred")) is bool)
		if phase == "write":
			validate_write_outcome(status, result, files)
		validated = {
			"schema": 1,
			"status": status,
			"reason": None,
			"root": str(root),
			"engine_version": result["engine_version"],
			"files": files,
			"selections": selections,
			"inputs": inputs,
			"process_count": result["process_count"],
			"elapsed_ms": result["elapsed_ms"],
			"writes_may_have_occurred": result["writes_may_have_occurred"],
		}
		if phase is not None:
			validated["phase"] = phase
		return validated
	except RuntimeError:
		return unchecked("stale_result", phase=phase)
	except (OSError, ValueError, TypeError, KeyError, AttributionError):
		return unchecked("invalid_provider_result", phase=phase)


def check_files(
	root: Path,
	files: list[dict[str, Any]],
	settings: dict[str, Any],
	deadline: float,
	*,
	phase: str | None = None,
	cancel_event: Event | None = None,
) -> dict[str, Any]:
	"""Run the writer or final read-only phase through the exact cached engine."""
	if phase is not None and phase not in {"write", "validate"}:
		return unchecked("invalid_request")
	if "ruff" not in settings["enabled"]:
		return not_applicable("disabled", phase=phase)
	if not files:
		return not_applicable("no_python_files", phase=phase)
	request: dict[str, Any] = {
		"schema": 1,
		"root": str(root.resolve()),
		"files": files,
		"deadline_unix_ms": int((time() + max(0, deadline - monotonic())) * 1000),
	}
	if phase is not None:
		request["phase"] = phase
	baseline_python = settings.get("baseline_python")
	baseline_version = settings.get("baseline_python_version")
	if baseline_python and baseline_version:
		request["baseline"] = {
			"policy_release": settings["baseline_release"],
			"python": baseline_python,
			"python_version": baseline_version,
		}
	encoded = json.dumps(request, ensure_ascii=True)
	if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
		return unchecked("request_limit", phase=phase)
	try:
		result = execute(
			[
				sys.executable,
				"-B",
				str(Path(__file__).with_name("provider_bridge.py")),
				"--release",
				settings["provider_release"],
			],
			deadline=deadline,
			input_text=encoded,
			cancel_event=cancel_event,
		)
	except ProcessError as error:
		return {
			**unchecked(str(error), phase=phase),
			"diagnostic": failure(
				error, "provider_validate" if phase == "validate" else "provider_write"
			)["diagnostic"],
		}
	return validate_result(
		result.stdout,
		result.returncode,
		request,
		settings["provider_release"],
		phase=phase,
	)


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--settings", type=Path)
	arguments = parser.parse_args()
	try:
		settings = load_settings(arguments.settings or default_settings_path())["current"]
		content = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
		if len(content) > MAX_REQUEST_BYTES:
			raise ValueError("request_limit")
		request = json.loads(content)
		if (
			not isinstance(request, dict)
			or request.get("schema") != 1
			or ("phase" in request and request.get("phase") not in {"write", "validate"})
		):
			raise ValueError("invalid_request")
		root = Path(request["root"])
		if not root.is_absolute() or not isinstance(request.get("files"), list):
			raise ValueError("invalid_request")
		result = check_files(
			root,
			request["files"],
			settings,
			monotonic() + settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS),
			phase=request.get("phase"),
		)
	except (SettingsError, OSError, ValueError, KeyError, TypeError):
		result = unchecked("invalid_request_or_settings")
	sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
	return STATUS_EXIT.get(result["status"], 2)


if __name__ == "__main__":
	raise SystemExit(main())
