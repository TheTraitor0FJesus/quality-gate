"""Deterministic rendering for concise and verbose quality reports."""

from __future__ import annotations

from .contracts import (
	MAX_FINDINGS_PER_CHECK,
	MAX_FINDINGS_TOTAL,
	MAX_TELEMETRY_TEXT,
	CheckResult,
	Finding,
	StageTiming,
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

	output = _render_verbose(verdict) if verbose else _render_compact(verdict)
	telemetry = _render_telemetry(verdict)
	return "\n".join((output, *telemetry))


def _telemetry_text(value: str, secrets: tuple[str, ...] = ()) -> str:
	return redact(value, secrets).replace("\r", "\\r").replace("\n", "\\n")[:MAX_TELEMETRY_TEXT]


def _timing_line(
	label: str,
	timing: StageTiming,
	secrets: tuple[str, ...] = (),
	*,
	outcome: str | None = None,
	test_totals: str | None = None,
) -> str:
	values = [
		f"elapsed_ms={timing.elapsed_ms}",
		f"disposition={timing.disposition}",
		f"scope={_telemetry_text(timing.scope, secrets)}",
		f"stage={_telemetry_text(timing.stage, secrets)}",
		f"tool_cache={_telemetry_text(timing.tool_cache, secrets)}",
	]
	if outcome is not None:
		values.append(f"outcome={outcome}")
	if test_totals is not None:
		values.append(f"test_totals={_telemetry_text(test_totals, secrets)}")
	for name, value in (
		("lookup_ms", timing.lookup_ms),
		("validation_ms", timing.validation_ms),
		("original_elapsed_ms", timing.original_elapsed_ms),
	):
		if value is not None:
			values.append(f"{name}={value}")
	if timing.original_operation_id is not None:
		values.append(
			f"original_operation={_telemetry_text(timing.original_operation_id, secrets)}"
		)
	if timing.reason:
		values.append(f"reason={_telemetry_text(timing.reason, secrets)}")
	return f"  {_telemetry_text(label, secrets)}: {'; '.join(values)}"


def _render_telemetry(verdict: Verdict) -> list[str]:
	lines: list[str] = []
	operation = verdict.operation
	if operation is not None:
		secrets = tuple(secret for result in verdict.results for secret in result._secrets)
		lines.extend(
			(
				"Operation: "
				+ "; ".join(
					f"{name}={_telemetry_text(value, secrets)}"
					for name, value in (
						("id", operation.operation_id),
						("operation", operation.operation),
						("profile", operation.profile),
						("candidate", operation.candidate),
						("scope", operation.scope),
					)
				),
				f"  elapsed_ms={operation.elapsed_ms}; complete={str(operation.complete).lower()}; "
				f"timed_out={str(operation.timed_out).lower()}; "
				f"current_stage={_telemetry_text(operation.current_stage or 'none', secrets)}; "
				"last_completed_stage="
				f"{_telemetry_text(operation.last_completed_stage or 'none', secrets)}",
			)
		)
		lines.extend(_timing_line(timing.stage, timing, secrets) for timing in operation.stages)
	checks = tuple(result for result in verdict.results if result.telemetry is not None)
	if checks:
		lines.append("Check timings:")
		for result in sorted(checks, key=lambda item: item.check_id):
			if result.telemetry is None:
				continue
			test_totals = None
			if result.check_id.endswith(".pytest") and result.summary.startswith("pytest totals:"):
				test_totals = result.summary.partition(":")[2].strip()
			lines.append(
				_timing_line(
					result.check_id,
					result.telemetry,
					result._secrets,
					outcome=result.status.value,
					test_totals=test_totals,
				)
			)
	return lines


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
