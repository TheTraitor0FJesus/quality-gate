"""Bounded, redacted diagnostics for failed release verification."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .output_redaction import redact

MAX_PYTEST_FAILURE_DIAGNOSTIC_BYTES = 12 * 1024


def _tail_bytes(value: str, limit: int) -> str:
	return value.encode("utf-8")[-limit:].decode("utf-8", errors="replace")


def _read_result(evidence: Path) -> dict[str, Any] | None:
	try:
		with (evidence / "result.json").open("rb") as stream:
			data = stream.read(MAX_PYTEST_FAILURE_DIAGNOSTIC_BYTES + 1)
		if len(data) > MAX_PYTEST_FAILURE_DIAGNOSTIC_BYTES:
			return None
		result = json.loads(data)
	except (OSError, UnicodeError, ValueError, TypeError):
		return None
	return result if isinstance(result, dict) else None


def _read_component_log(evidence: Path, name: object, limit: int) -> str:
	if not isinstance(name, str) or re.fullmatch(r"component-[0-9]+\.log", name) is None:
		return ""
	path = evidence / name
	try:
		if path.is_symlink() or not path.resolve().is_relative_to(evidence.resolve()):
			return ""
		with path.open("rb") as stream:
			stream.seek(0, 2)
			size = stream.tell()
			stream.seek(max(0, size - limit))
			data = stream.read(limit)
	except OSError:
		return ""
	truncated = size > limit
	content = redact(data.decode("utf-8", errors="replace"))
	content = _tail_bytes(content, limit)
	if truncated:
		content += "\n  pytest log excerpt truncated"
	return content


def _component_diagnostic(evidence: Path, component: dict[str, Any], limit: int) -> list[str]:
	lines: list[str] = []
	name = component.get("name")
	status = component.get("status")
	if isinstance(name, str) and isinstance(status, str):
		lines.append(f"component {redact(name)}: {redact(status)}")
	reason = component.get("reason")
	if isinstance(reason, str):
		lines.append(f"  reason: {redact(reason)}")
	totals = component.get("totals")
	if isinstance(totals, dict):
		values = ", ".join(
			f"{key}={totals[key]}"
			for key in ("tests", "failures", "errors", "skipped")
			if isinstance(totals.get(key), int)
		)
		if values:
			lines.append(f"  totals: {values}")
	used = len("\n".join(lines).encode("utf-8")) + len(lines)
	remaining = max(0, limit - used)
	if not isinstance(status, str) or status not in {"failed", "error"}:
		return lines
	marker_size = len(b"\n  pytest log excerpt truncated")
	log = _read_component_log(evidence, component.get("log"), max(0, remaining - marker_size))
	if log:
		lines.extend(("  pytest log:", log))
	return lines


def pytest_failure_diagnostic(evidence: Path | None, stdout: str, stderr: str) -> str:
	"""Keep a useful explanation after the temporary release workspace is removed."""
	lines: list[str] = []
	if evidence is not None:
		result = _read_result(evidence)
		if result is not None:
			status = result.get("status")
			if isinstance(status, str):
				lines.append(f"pytest status: {redact(status)}")
			reason = result.get("reason")
			if isinstance(reason, str):
				lines.append(f"pytest reason: {redact(reason)}")
			components = result.get("components")
			if isinstance(components, list):
				for component in components:
					if isinstance(component, dict):
						remaining = max(
							0,
							MAX_PYTEST_FAILURE_DIAGNOSTIC_BYTES
							- len("\n".join(lines).encode("utf-8")),
						)
						component_lines = _component_diagnostic(evidence, component, remaining)
						lines.extend(component_lines)
	if not any("pytest log:" in line for line in lines):
		captured = redact((stdout + "\n" + stderr).strip())
		if captured:
			lines.append(_tail_bytes(captured, MAX_PYTEST_FAILURE_DIAGNOSTIC_BYTES))
	return _tail_bytes("\n".join(lines), MAX_PYTEST_FAILURE_DIAGNOSTIC_BYTES)
