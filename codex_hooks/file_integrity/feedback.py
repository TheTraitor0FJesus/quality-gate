"""Integrity presentation independent of the pending touched-file provider.

The caller establishes selected policy, final-content freshness, coverage and
waiver validity before projecting a result. This boundary never scans content.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from _shared.quality_events import MAX_FILES
from _shared.quality_feedback import MAX_INTEGRITY_FINDINGS, feedback, safe_relative

CHECK_ID = "repository.git.conflict_markers"
MAX_REPORT_BYTES = 256 * 1024
MAX_LINE_NUMBER = 2**31 - 1
REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def integrity_projection(output: str, *, touched_paths: set[str]) -> dict[str, Any]:
	"""Validate a projection against independently attributed canonical paths.

	Projection: schema 1, check_id repository.git.conflict_markers, status, and
	findings containing path and line. Raw messages and unknown fields are discarded.
	The allowlist comes from attribution, separately from the scanner result.
	"""
	report: dict[str, Any] = {"check": "integrity", "status": "unchecked"}
	try:
		if (
			not isinstance(output, str)
			or len(output) > MAX_REPORT_BYTES
			or len(output.encode("utf-8")) > MAX_REPORT_BYTES
			or len(touched_paths) > MAX_FILES
			or any(
				not isinstance(path, str) or path == "." or safe_relative(path) != path
				for path in touched_paths
			)
		):
			raise ValueError("invalid_report")
		data = json.loads(output)
		if (
			not isinstance(data, dict)
			or type(data.get("schema")) is not int
			or data["schema"] != 1
			or data.get("check_id") != CHECK_ID
			or not isinstance(data.get("status"), str)
			or data["status"] not in {"passed", "failed", "waived", "not_applicable", "unchecked"}
		):
			raise ValueError("invalid_report")
		findings = data.get("findings")
		if (
			not isinstance(findings, list)
			or len(findings) > MAX_INTEGRITY_FINDINGS
			or (data["status"] == "failed") != bool(findings)
		):
			raise ValueError("invalid_report")
		reason = data.get("reason")
		if reason is not None and (
			data["status"] != "unchecked"
			or not isinstance(reason, str)
			or REASON.fullmatch(reason) is None
		):
			raise ValueError("invalid_report")
		clean = []
		for finding in findings:
			if (
				not isinstance(finding, dict)
				or not isinstance(finding.get("path"), str)
				or finding["path"] not in touched_paths
				or type(finding.get("line")) is not int
				or not 0 < finding["line"] <= MAX_LINE_NUMBER
			):
				raise ValueError("invalid_report")
			clean.append({"path": finding["path"], "line": finding["line"]})
		report.update(status=data["status"], findings=clean)
		if reason is not None:
			report["reason"] = reason
	except (ValueError, TypeError, UnicodeError, RecursionError):
		pass
	return report


def integrity_feedback(
	output: str, *, touched_paths: set[str], evidence_dir: Path | None = None
) -> dict[str, Any] | None:
	"""Render a validated host projection through shared model feedback."""
	return feedback(
		"PostToolUse",
		[integrity_projection(output, touched_paths=touched_paths)],
		evidence_dir=evidence_dir,
	)
