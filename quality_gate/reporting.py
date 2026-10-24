"""Compact deterministic English reporting for agents."""

from __future__ import annotations

from .contracts import (
	MAX_FINDINGS_PER_CHECK,
	MAX_FINDINGS_TOTAL,
	CheckResult,
	Status,
	Verdict,
	redact,
)

SINGLE_FINDING_COUNT = 1


def _finding_count(count: int) -> str:
	unit = "finding" if count == SINGLE_FINDING_COUNT else "findings"
	return f"{count} {unit}"


def _render_result_details(
	result: CheckResult, *, verbose: bool, total_budget: int
) -> tuple[list[str], int, int]:
	available_findings = result.ordered_findings
	findings = available_findings if verbose else available_findings[:MAX_FINDINGS_PER_CHECK]
	if not verbose:
		findings = findings[: max(0, total_budget)]

	lines = []
	for finding in findings:
		location = (
			f"{finding.path}:{finding.line}" if finding.path and finding.line else finding.path
		)
		location = redact(location, result._secrets)
		detail = f"{location}: " if location else ""
		detail += redact(finding.message, result._secrets)
		if finding.action:
			detail += f"; action: {redact(finding.action, result._secrets)}"
		lines.append(f"  - {detail}")

	report_omissions = len(available_findings) - len(findings)
	if report_omissions:
		lines.append(
			f"  - {_finding_count(report_omissions)} omitted by report limits; "
			"use --verbose for all available details"
		)
	if result.omitted_findings is None:
		lines.append("  - Additional findings were omitted while checking; exact count unavailable")
	elif result.omitted_findings:
		lines.append(f"  - {_finding_count(result.omitted_findings)} omitted upstream")
	if result.recovery_action:
		lines.append(f"  recovery: {redact(result.recovery_action, result._secrets)}")
	known_omissions = report_omissions + (result.omitted_findings or 0)
	return lines, len(findings), known_omissions


def render(verdict: Verdict, *, verbose: bool = False) -> str:
	lines = [f"Quality Gate: {verdict.exit_code}"]
	total_displayed = 0
	known_omissions = 0
	checks_with_known_omissions = 0
	for result in sorted(verdict.results, key=lambda item: item.check_id):
		lines.append(
			f"{result.check_id}: {result.status.value} - {redact(result.summary, result._secrets)}"
		)
		if result.status is Status.PASSED and not verbose:
			continue
		result_lines, displayed, check_known_omissions = _render_result_details(
			result,
			verbose=verbose,
			total_budget=MAX_FINDINGS_TOTAL - total_displayed,
		)
		lines.extend(result_lines)
		total_displayed += displayed
		if check_known_omissions:
			known_omissions += check_known_omissions
			checks_with_known_omissions += 1
	if known_omissions:
		lines.append(
			f"  - {known_omissions} known findings omitted across "
			f"{checks_with_known_omissions} checks; "
			"use --verbose for all findings collected by checks"
		)
	return "\n".join(lines)
