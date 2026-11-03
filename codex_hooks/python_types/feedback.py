"""Present validated typing results; selection, analysis and freshness are upstream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _shared.quality_feedback import MAX_TYPE_FINDINGS, MYPY_RULE, feedback, safe_relative

MAX_REPORT_BYTES = 256 * 1024
MAX_ANALYSIS_PATHS = 1024
MAX_LOCATION = 2**31 - 1


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("ambiguous_report")
		result[key] = value
	return result


def typing_feedback(
	output: str, *, analysis_paths: set[str], evidence_dir: Path | None = None
) -> dict[str, Any] | None:
	"""Render a source-free host projection against an independent analysis inventory.

	The caller first verifies the selected policy/environment, complete analysis and
	content/dependency freshness. The inventory includes unchanged dependents; the
	editing event's touched paths alone cannot establish component-wide coverage.
	This internal projection is not a public provider protocol or a type checker.
	"""
	report: dict[str, Any] = {"check": "types", "status": "unchecked"}
	try:
		if (
			not isinstance(output, str)
			or len(output) > MAX_REPORT_BYTES
			or len(output.encode("utf-8")) > MAX_REPORT_BYTES
			or not isinstance(analysis_paths, set)
			or len(analysis_paths) > MAX_ANALYSIS_PATHS
			or any(
				not isinstance(path, str) or path == "." or safe_relative(path) != path
				for path in analysis_paths
			)
		):
			raise ValueError("invalid_report")
		data = json.loads(output, object_pairs_hook=unique_keys)
		if (
			not isinstance(data, dict)
			or type(data.get("schema")) is not int
			or data["schema"] != 1
			or data.get("check_id") != "mypy"
			or not isinstance(data.get("status"), str)
			or data["status"] not in {"passed", "failed", "not_applicable", "unchecked"}
		):
			raise ValueError("invalid_report")
		findings = data.get("findings")
		if (
			not isinstance(findings, list)
			or len(findings) > MAX_TYPE_FINDINGS
			or (data["status"] == "failed") != bool(findings)
		):
			raise ValueError("invalid_report")
		clean = []
		for finding in findings:
			if (
				not isinstance(finding, dict)
				or not isinstance(finding.get("path"), str)
				or finding["path"] not in analysis_paths
				or any(
					type(finding.get(name)) is not int or not 0 < finding[name] <= MAX_LOCATION
					for name in ("line", "column")
				)
				or not isinstance(finding.get("rule"), str)
				or MYPY_RULE.fullmatch(finding["rule"]) is None
			):
				raise ValueError("invalid_report")
			clean.append({name: finding[name] for name in ("path", "line", "column", "rule")})
		report.update(status=data["status"], findings=clean)
	except (ValueError, TypeError, UnicodeError, RecursionError):
		pass
	return feedback("PostToolUse", [report], evidence_dir=evidence_dir)
