"""Source-free presentation boundary, independent of a scanner wire protocol.

The provider adapter must establish policy, freshness and waiver validity before
projecting its result here. Rendering is not evidence that a scan ran.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _shared.quality_feedback import feedback, safe_relative, secret_finding_line

MAX_REPORT_BYTES = 256 * 1024
MAX_FINDINGS = 200
MAX_FILES = 10


def secret_feedback(
	output: str, *, touched_paths: set[str], evidence_dir: Path | None = None
) -> dict[str, Any] | None:
	"""Render a host projection bound to independently attributed touched paths.

	Projection: schema 1, check_id secrets.candidate, status, and findings carrying
	path, line and fingerprint. Raw scanner text and unknown fields are discarded.
	The caller owns the touched-path allowlist; it never comes from scanner output.
	Waived findings retain validation before the successful projection becomes silent.
	"""
	report: dict[str, Any] = {"check": "secrets", "status": "unchecked"}
	try:
		if (
			not isinstance(output, str)
			or len(output) > MAX_REPORT_BYTES
			or len(output.encode("utf-8")) > MAX_REPORT_BYTES
			or len(touched_paths) > MAX_FILES
			or any(safe_relative(path) != path for path in touched_paths)
		):
			raise ValueError("invalid_report")
		data = json.loads(output)
		if (
			not isinstance(data, dict)
			or type(data.get("schema")) is not int
			or data["schema"] != 1
			or data.get("check_id") != "secrets.candidate"
			or not isinstance(data.get("status"), str)
			or data["status"] not in {"passed", "failed", "waived", "not_applicable", "unchecked"}
		):
			raise ValueError("invalid_report")
		findings = data.get("findings")
		if (
			not isinstance(findings, list)
			or len(findings) > MAX_FINDINGS
			or (data["status"] == "failed" and not findings)
			or (data["status"] not in {"failed", "waived"} and bool(findings))
		):
			raise ValueError("invalid_report")
		files: dict[str, list[dict[str, Any]]] = {}
		for finding in findings:
			if (
				not isinstance(finding, dict)
				or not isinstance(finding.get("path"), str)
				or finding["path"] not in touched_paths
			):
				raise ValueError("invalid_report")
			projected = {
				"check_id": "secrets.candidate",
				"line": finding.get("line"),
				"fingerprint": finding.get("fingerprint"),
			}
			if data["status"] == "waived":
				secret_finding_line(projected, finding["path"])
			files.setdefault(finding["path"], []).append(projected)
		report.update(
			status="not_applicable" if data["status"] == "waived" else data["status"],
			files=[{"path": path, "findings": items} for path, items in files.items()],
		)
	except (ValueError, TypeError, UnicodeError, RecursionError):
		pass
	return feedback("PostToolUse", [report], evidence_dir=evidence_dir)
