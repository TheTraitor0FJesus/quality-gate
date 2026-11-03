"""Preserve legacy safety and expose its bounded built-in-only migration route."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from threading import Event
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_events import MAX_FILE_BYTES, MAX_FILES, safe_path
from _shared.quality_process import ProcessError, execute
from _shared.quality_settings import default_settings_path

MAX_BUILTIN_FINDINGS = 200


def heavy_security_enabled() -> bool:
	"""Select the existing opt-in without importing scanner implementations."""
	return os.environ.get("CODEX_POST_EDIT_HEAVY_SECURITY") == "1"


def run_scanner(
	root: Path,
	files: list[dict[str, Any]],
	deadline: float,
	*,
	builtin_only: bool = False,
	cancel_event: Event | None = None,
	heavy: bool | None = None,
) -> dict[str, Any]:
	"""Run the installed security supplement with bounded, source-free output."""
	check = "builtin_secrets" if builtin_only else "legacy_security"
	request = {
		"schema": 1,
		"root": str(root),
		"files": files,
		"mode": "builtin" if builtin_only else "legacy",
	}
	try:
		process = execute(
			[sys.executable, "-B", str(Path(__file__).resolve())],
			deadline=deadline,
			input_text=json.dumps(request, ensure_ascii=True),
			cancel_event=cancel_event,
			environment=(
				{**os.environ, "CODEX_POST_EDIT_HEAVY_SECURITY": "1" if heavy else "0"}
				if heavy is not None
				else None
			),
		)
		result = json.loads(process.stdout)
		if (
			process.returncode != 0
			or not isinstance(result, dict)
			or result.get("schema") != 1
			or result.get("check") != check
			or result.get("status") not in {"passed", "failed"}
		):
			raise ValueError("security_unavailable")
		expected = {item["path"] for item in files}
		if not isinstance(result.get("files"), list) or any(
			not isinstance(item, dict) or item.get("path") not in expected
			for item in result["files"]
		):
			raise ValueError("security_unavailable")
		findings = [{"path": item["path"]} for item in result["files"]]
		if builtin_only:
			if len(findings) > MAX_BUILTIN_FINDINGS or any(
				type(item.get("line")) is not int
				or not 0 < item["line"] <= 2**31 - 1
				or item.get("check_id") != "secrets.builtin"
				for item in result["files"]
			):
				raise ValueError("security_unavailable")
			findings = [
				{"path": item["path"], "line": item["line"], "check_id": "secrets.builtin"}
				for item in result["files"]
			]
		if (result["status"] == "failed") != bool(findings):
			raise ValueError("security_unavailable")
		return {
			"check": check,
			"status": result["status"],
			"files": findings,
		}
	except (OSError, ValueError, ProcessError):
		return {
			"check": check,
			"status": "unchecked",
			"reason": "security_unavailable",
			"files": [],
		}


def builtin_findings(path: Path, content: bytes) -> list[dict[str, Any]]:
	"""Reuse installed detector definitions on the bounded captured bytes only."""
	from _shared.codex_hook_adapter import BUILTIN_SECRET_PATTERNS, SCANNABLE_EXTENSIONS

	extension = path.suffix.lower() if path.suffix else path.name.lower()
	if extension not in SCANNABLE_EXTENSIONS:
		return []
	findings = []
	for line_number, line in enumerate(content.decode("utf-8", errors="ignore").splitlines(), 1):
		if any(pattern.search(line) for _, pattern in BUILTIN_SECRET_PATTERNS):
			findings.append({"line": line_number, "check_id": "secrets.builtin"})
			if len(findings) > MAX_BUILTIN_FINDINGS:
				raise ValueError("findings_limit")
	return findings


def legacy_blocks(path: Path, *, heavy: bool) -> bool:
	"""Only real scanner findings establish a violation; tool errors stay unchecked."""
	from _shared.codex_hook_adapter import (
		run_builtin_secret_scan,
		run_detect_secrets,
		run_semgrep,
	)

	findings = run_builtin_secret_scan(path)
	if heavy:
		findings = run_semgrep(path) + run_detect_secrets(path) + findings
	if any(item.get("tool") != "HOOK" for item in findings):
		return True
	if findings:
		raise ValueError("security_unavailable")
	return False


def scan(request: dict[str, Any]) -> dict[str, Any]:
	root = Path(request["root"]).resolve()
	files = request["files"]
	if not isinstance(files, list) or len(files) > MAX_FILES:
		raise ValueError("invalid_request")
	mode = request.get("mode", "legacy")
	if mode not in {"legacy", "builtin"}:
		raise ValueError("invalid_request")
	hooks_root = default_settings_path().parents[2] / "hooks"
	sys.path.insert(0, str(hooks_root))
	from _shared.codex_hook_adapter import POST_EDIT_HEAVY_SECURITY

	found = []
	for item in files:
		path, _ = safe_path(item["path"], root)
		with path.open("rb") as source:
			content = source.read(MAX_FILE_BYTES + 1)
		if len(content) > MAX_FILE_BYTES or hashlib.sha256(content).hexdigest() != item["sha256"]:
			raise ValueError("stale_input")
		if mode == "legacy":
			blocking = legacy_blocks(path, heavy=POST_EDIT_HEAVY_SECURITY)
			if blocking:
				found.append({"path": item["path"]})
		else:
			found.extend(
				{"path": item["path"], **finding} for finding in builtin_findings(path, content)
			)
		if len(found) > MAX_BUILTIN_FINDINGS:
			raise ValueError("findings_limit")
		with path.open("rb") as source:
			final_content = source.read(MAX_FILE_BYTES + 1)
		if hashlib.sha256(final_content).hexdigest() != item["sha256"]:
			raise ValueError("stale_input")
	return {
		"schema": 1,
		"check": "builtin_secrets" if mode == "builtin" else "legacy_security",
		"status": "failed" if found else "passed",
		"files": found,
	}


def main() -> int:
	try:
		content = sys.stdin.buffer.read(64 * 1024 + 1)
		if len(content) > 64 * 1024:
			raise ValueError("input_limit")
		request = json.loads(content)
		if not isinstance(request, dict) or request.get("schema") != 1:
			raise ValueError("invalid_request")
		result = scan(request)
	except (OSError, ValueError, KeyError, TypeError, ImportError):
		result = {
			"schema": 1,
			"check": "legacy_security",
			"status": "unchecked",
			"reason": "security_unavailable",
			"files": [],
		}
		code = 2
	else:
		code = 0
	sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
	return code


if __name__ == "__main__":
	raise SystemExit(main())
