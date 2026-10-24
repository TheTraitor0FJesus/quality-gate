"""Deterministic rendering for concise and verbose quality reports."""

from __future__ import annotations

from .contracts import (
	MAX_FINDINGS_PER_CHECK,
	MAX_FINDINGS_TOTAL,
	CheckResult,
	Finding,
	Status,
	Verdict,
	redact,
)

SINGLE_FINDING_COUNT = 1


def _finding_count(count: int) -> str:
	unit = "finding" if count == SINGLE_FINDING_COUNT else "findings"
	return f"{count} {unit}"


def render(verdict: Verdict, *, verbose: bool = False) -> str:
	"""Render concise outcomes or the complete deterministic check listing."""

	if verbose:
		return _render_verbose(verdict)
	return _render_compact(verdict)


def _render_compact(verdict: Verdict) -> str:
	results = tuple(sorted(verdict.results, key=lambda item: item.check_id))
	lines = [f"Quality Gate: {verdict.exit_code}"]
	if not results:
		lines.append("No check results were reported.")
		return "\n".join(lines)

	report_only = tuple(result for result in results if _is_report_only(result))
	blockers = tuple(
		result for result in results if result.status in {Status.FAILED, Status.UNCHECKED}
	)
	nonblocking_omissions, nonblocking_known, nonblocking_checks = _render_nonblocking_omissions(
		results
	)
	lines.append(_render_summary(results, report_only))
	lines.extend(_render_blocker_summaries(blockers))
	diagnostics, omitted_total, checks_with_omissions = _render_diagnostics(blockers)
	if diagnostics:
		lines.append("Diagnostic details:")
		lines.extend(diagnostics)
	if nonblocking_omissions:
		lines.append("Upstream omission metadata:")
		lines.extend(nonblocking_omissions)
	omitted_total += nonblocking_known
	checks_with_omissions += nonblocking_checks
	if omitted_total:
		omitted_label = f"{omitted_total} known finding"
		if omitted_total != SINGLE_FINDING_COUNT:
			omitted_label += "s"
		check_label = f"{checks_with_omissions} check"
		if checks_with_omissions != SINGLE_FINDING_COUNT:
			check_label += "s"
		lines.append(
			f"  - {omitted_label} omitted across {check_label}; "
			"use --verbose for all findings collected by checks"
		)
	lines.extend(_render_report_only(report_only))
	return "\n".join(lines)


def _render_summary(results: tuple[CheckResult, ...], report_only: tuple[CheckResult, ...]) -> str:
	counts = {status: 0 for status in Status}
	for result in results:
		if not _is_report_only(result):
			counts[result.status] += 1
	summary = [f"{counts[Status.PASSED]} passed"] if counts[Status.PASSED] else []
	summary.extend(
		f"{counts[status]} {label}"
		for status, label in (
			(Status.FAILED, "failed"),
			(Status.UNCHECKED, "unchecked"),
			(Status.WAIVED, "waived"),
			(Status.NOT_APPLICABLE, "not applicable"),
		)
		if counts[status]
	)
	if report_only:
		label = "result" if len(report_only) == SINGLE_FINDING_COUNT else "results"
		summary.append(f"{len(report_only)} report-only {label}")
	return f"Observed results: {', '.join(summary)}"


def _render_blocker_summaries(blockers: tuple[CheckResult, ...]) -> list[str]:
	lines: list[str] = []
	for result in blockers:
		lines.append(
			f"{result.check_id}: {result.status.value} - {redact(result.summary, result._secrets)}"
		)
		if result.recovery_action:
			lines.append(f"  recovery: {redact(result.recovery_action, result._secrets)}")
	return lines


def _render_diagnostics(blockers: tuple[CheckResult, ...]) -> tuple[list[str], int, int]:
	displayed_total = 0
	known_omissions = 0
	checks_with_omissions = 0
	lines: list[str] = []
	for result in blockers:
		findings = result.ordered_findings
		display_count = min(
			len(findings),
			MAX_FINDINGS_PER_CHECK,
			max(0, MAX_FINDINGS_TOTAL - displayed_total),
		)
		displayed = findings[:display_count]
		report_omissions = len(findings) - display_count
		if displayed:
			lines.append(f"  {result.check_id}:")
			lines.extend(f"    - {_format_finding(result, finding)}" for finding in displayed)
		if report_omissions:
			lines.append(
				f"  {result.check_id}: {_finding_count(report_omissions)} omitted by report limits"
			)
		omitted_upstream = _append_upstream_omissions(result, lines)
		check_omissions = report_omissions + (omitted_upstream or 0)
		if check_omissions:
			known_omissions += check_omissions
			checks_with_omissions += 1
		displayed_total += len(displayed)
	return lines, known_omissions, checks_with_omissions


def _render_nonblocking_omissions(
	results: tuple[CheckResult, ...],
) -> tuple[list[str], int, int]:
	lines: list[str] = []
	known_omissions = 0
	checks_with_omissions = 0
	for result in results:
		if result.status in {Status.FAILED, Status.UNCHECKED}:
			continue
		if result.omitted_findings == 0:
			continue
		omitted = _append_upstream_omissions(result, lines)
		if omitted:
			known_omissions += omitted
			checks_with_omissions += 1
	return lines, known_omissions, checks_with_omissions


def _render_report_only(results: tuple[CheckResult, ...]) -> list[str]:
	lines: list[str] = []
	for result in results:
		lines.append(f"Report-only: {result.check_id} - {redact(result.summary, result._secrets)}")
		if result.recovery_action:
			lines.append(f"  note: {redact(result.recovery_action, result._secrets)}")
	return lines


def _render_verbose(verdict: Verdict) -> str:
	lines = [f"Quality Gate: {verdict.exit_code}"]
	if not verdict.results:
		lines.append("No check results were reported.")
		return "\n".join(lines)
	for result in sorted(verdict.results, key=lambda item: item.check_id):
		lines.append(
			f"{result.check_id}: {result.status.value} - {redact(result.summary, result._secrets)}"
		)
		lines.extend(
			f"  - {_format_finding(result, finding)}" for finding in result.ordered_findings
		)
		_append_upstream_omissions(result, lines)
		if result.recovery_action:
			lines.append(f"  recovery: {redact(result.recovery_action, result._secrets)}")
	return "\n".join(lines)


def _is_report_only(result: CheckResult) -> bool:
	return result.status is Status.PASSED and (
		result.check_id.endswith(".coverage_report")
		or any(
			"report-only" in value.casefold()
			for value in (result.summary, result.recovery_action or "")
		)
	)


def _append_upstream_omissions(result: CheckResult, lines: list[str]) -> int | None:
	if result.omitted_findings is None:
		lines.append(
			f"  {result.check_id}: additional findings were omitted while checking; "
			"exact count unavailable"
		)
		return None
	if result.omitted_findings:
		lines.append(
			f"  {result.check_id}: {_finding_count(result.omitted_findings)} omitted upstream"
		)
	return result.omitted_findings


def _format_finding(result: CheckResult, finding: Finding) -> str:
	location = f"{finding.path}:{finding.line}" if finding.path and finding.line else finding.path
	location = redact(location, result._secrets)
	detail = f"{location}: " if location else ""
	detail += redact(finding.message, result._secrets)
	if finding.action:
		detail += f"; action: {redact(finding.action, result._secrets)}"
	return detail
