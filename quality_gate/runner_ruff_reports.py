"""Strict parsing and errors for Ruff verification reports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from . import runner as _runner
from .contracts import MAX_FINDINGS_PER_CHECK, CheckResult, Finding, Status

_MAX_REPORT_FINDINGS = 4096


def _ruff_report_findings(
	code: int,
	output: str,
	root: Path,
	included: set[str],
) -> list[dict[str, object]]:
	try:
		report = json.loads(output)
	except json.JSONDecodeError as error:
		raise ValueError("Ruff returned invalid JSON") from error
	if (
		not isinstance(report, list)
		or code not in {0, 1, _runner.EXIT_UNCHECKED}
		or (code == 0 and report)
		or (code in {1, _runner.EXIT_UNCHECKED} and not report)
		or len(report) > _MAX_REPORT_FINDINGS
	):
		raise ValueError("Ruff returned an incomplete report")
	root = root.resolve(strict=True)
	findings: list[dict[str, object]] = []
	for item in report:
		if not isinstance(item, dict):
			raise ValueError("Ruff returned an invalid diagnostic")
		filename = item.get("filename")
		code_value = item.get("code")
		location = item.get("location")
		if (
			not isinstance(filename, str)
			or not isinstance(code_value, str)
			or not isinstance(location, dict)
		):
			raise ValueError("Ruff returned an invalid diagnostic")
		try:
			candidate = Path(filename)
			candidate = candidate if candidate.is_absolute() else root / candidate
			path = candidate.resolve(strict=True).relative_to(root).as_posix()
		except (OSError, ValueError) as error:
			raise ValueError("Ruff reported a path outside the candidate") from error
		line, column = location.get("row"), location.get("column")
		if (
			path not in included
			or not re.fullmatch(r"(?:[A-Z]+[0-9]+|invalid-syntax)", code_value)
			or type(line) is not int
			or type(column) is not int
			or min(line, column) < 1
		):
			raise ValueError("Ruff returned an invalid diagnostic")
		findings.append({"path": path, "rule": code_value, "line": line, "column": column})
	if code == _runner.EXIT_UNCHECKED and not any(
		finding["rule"] == "invalid-syntax" for finding in findings
	):
		raise ValueError("Ruff exited without a complete diagnostic")
	return findings


def _ruff_unformatted(
	code: int,
	output: str,
	root: Path,
	included: set[str],
) -> set[str]:
	if code == 0 and not output.strip():
		return set()
	try:
		report = json.loads(output)
	except json.JSONDecodeError as error:
		raise ValueError("Ruff format returned invalid JSON") from error
	if not isinstance(report, list) or code not in {0, 1}:
		raise ValueError("Ruff format returned an incomplete report")
	if (code == 0 and report) or (code == 1 and not report):
		raise ValueError("Ruff format status does not match its report")
	root = root.resolve(strict=True)
	paths: set[str] = set()
	for item in report:
		if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
			raise ValueError("Ruff format returned an invalid path")
		try:
			candidate = Path(item["filename"])
			candidate = candidate if candidate.is_absolute() else root / candidate
			path = candidate.resolve(strict=True).relative_to(root).as_posix()
		except (OSError, ValueError) as error:
			raise ValueError("Ruff format reported a path outside the candidate") from error
		if path not in included:
			raise ValueError("Ruff format reported a path outside the checked scope")
		paths.add(path)
	return paths


def _build_ruff_results(
	outcomes: dict[str, dict[str, Any]],
	import_findings: list[dict[str, object]],
	cached: dict[str, dict[str, Any]],
	eligible: list[str],
	check_id: str,
	format_id: str,
) -> tuple[CheckResult, CheckResult]:
	local_findings = [finding for outcome in outcomes.values() for finding in outcome["findings"]]
	ruff_findings = [*local_findings, *import_findings]
	format_findings = [
		{"path": path} for path, outcome in outcomes.items() if outcome["format_status"] == "failed"
	]
	reused_count = sum(path in cached for path in eligible)
	executed_count = len(eligible) - reused_count
	ruff_failed = any(outcome["lint_status"] == "failed" for outcome in outcomes.values()) or bool(
		import_findings
	)
	format_failed = bool(format_findings)
	ruff_summary = (
		f"Ruff check {'requires attention' if ruff_failed else 'passed'}; "
		f"{reused_count} per-file results reused, {executed_count} executed; "
		f"import rules rechecked across {len(eligible)} eligible files"
	)
	format_summary = (
		f"format check {'requires attention' if format_failed else 'passed'}; "
		f"{reused_count} per-file results reused, {executed_count} executed"
	)
	return (
		CheckResult(
			check_id,
			Status.FAILED if ruff_failed else Status.PASSED,
			ruff_summary,
			findings=tuple(
				Finding(
					path=str(item["path"]),
					line=cast(int, item["line"]),
					message=f"Ruff rule {item['rule']}",
					action=f"Resolve Ruff {item['rule']} in this file.",
				)
				for item in ruff_findings[:MAX_FINDINGS_PER_CHECK]
			),
			recovery_action=(
				"fix the reported Ruff findings and rerun the quality gate" if ruff_failed else None
			),
			omitted_findings=max(0, len(ruff_findings) - MAX_FINDINGS_PER_CHECK),
		),
		CheckResult(
			format_id,
			Status.FAILED if format_failed else Status.PASSED,
			format_summary,
			findings=tuple(
				Finding(
					path=str(item["path"]),
					message="file is not formatted",
					action="run quality-gate format for this file",
				)
				for item in format_findings[:MAX_FINDINGS_PER_CHECK]
			),
			recovery_action=(
				"format the reported files and rerun the quality gate" if format_failed else None
			),
			omitted_findings=max(0, len(format_findings) - MAX_FINDINGS_PER_CHECK),
		),
	)
