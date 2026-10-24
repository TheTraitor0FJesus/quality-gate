"""Behavioral tests for concise and verbose quality reports."""

from __future__ import annotations

import pytest

from quality_gate.contracts import (
	MAX_FINDINGS_PER_CHECK,
	MAX_FINDINGS_TOTAL,
	CheckResult,
	Finding,
	Status,
	Verdict,
)
from quality_gate.integrity import git_conflict_markers
from quality_gate.reporting import render

UPSTREAM_MARKER_LIMIT = 1
SINGLE_FINDING_COUNT = 1
PER_CHECK_OMISSION = MAX_FINDINGS_PER_CHECK // 4
TOTAL_OMISSION = MAX_FINDINGS_PER_CHECK // 2
EXPECTED_OMITTING_CHECK_COUNT = 2
EXPECTED_UNCHECKED_EXIT = 2
KNOWN_UPSTREAM_OMISSION = MAX_FINDINGS_PER_CHECK // 4
EXPECTED_DISPLAYED_FINDINGS = MAX_FINDINGS_TOTAL
EXPECTED_AVAILABLE_FINDINGS = MAX_FINDINGS_PER_CHECK + SINGLE_FINDING_COUNT


def _failed_result(
	check_id: str,
	finding_count: int,
	*,
	omitted_findings: int = 0,
) -> CheckResult:
	return CheckResult(
		check_id=check_id,
		status=Status.FAILED,
		summary=f"summary for {check_id}",
		findings=tuple(
			Finding(message=f"detail-{check_id}-{index}") for index in range(finding_count)
		),
		recovery_action=f"recover {check_id}",
		omitted_findings=omitted_findings,
	)


def _result_with_upstream_omissions(omitted_findings: int | None) -> CheckResult:
	return CheckResult(
		check_id="runtime.verify",
		status=Status.UNCHECKED,
		summary="verification could not inspect every finding",
		findings=tuple(
			Finding(path=f"check-{index:02d}.py", message="available detail")
			for index in range(EXPECTED_AVAILABLE_FINDINGS)
		),
		recovery_action="restore complete verification",
		omitted_findings=omitted_findings,
	)


def test_default_report_summarizes_observed_outcomes_and_retains_report_only_info() -> None:
	results = Verdict(
		(
			CheckResult("core.pass", Status.PASSED, "check passed"),
			CheckResult(
				"python.coverage_report",
				Status.PASSED,
				"coverage report: TOTAL 10 1 90%",
			),
			CheckResult(
				"python.deptry",
				Status.WAIVED,
				"typed waiver applies",
				waiver_target="pyproject.toml::DEP002::plugin-package",
			),
			CheckResult(
				"python.tests",
				Status.NOT_APPLICABLE,
				"no test paths are configured",
				recovery_action="declare test paths when tests apply",
			),
		)
	)

	output = render(results)
	verbose = render(results, verbose=True)

	assert output.splitlines()[0] == "Quality Gate: 0"
	assert "Observed results: 1 passed, 1 waived, 1 not applicable, 1 report-only result" in output
	assert "core.pass: passed" not in output
	assert "python.coverage_report: passed" not in output
	assert "python.deptry: waived" not in output
	assert "python.tests: not_applicable" not in output
	assert "Report-only: python.coverage_report - coverage report: TOTAL 10 1 90%" in output
	assert "core.pass: passed" in verbose
	assert "python.coverage_report: passed" in verbose
	assert "python.deptry: waived" in verbose
	assert "python.tests: not_applicable" in verbose


def test_default_report_lists_every_blocking_check_after_detail_budget() -> None:
	results = [
		_failed_result(f"blocker.{index}", MAX_FINDINGS_PER_CHECK)
		for index in range(MAX_FINDINGS_TOTAL // MAX_FINDINGS_PER_CHECK)
	]
	results.extend(
		(
			_failed_result("z.failed", SINGLE_FINDING_COUNT),
			CheckResult(
				check_id="z.unchecked",
				status=Status.UNCHECKED,
				summary="runtime unavailable",
				recovery_action="restore the runtime",
			),
		)
	)

	output = render(Verdict(tuple(reversed(results))))

	assert output.splitlines()[0] == "Quality Gate: 2"
	assert "z.failed: failed - summary for z.failed" in output
	assert "recovery: recover z.failed" in output
	assert "z.unchecked: unchecked - runtime unavailable" in output
	assert "recovery: restore the runtime" in output
	assert output.index("z.failed:") < output.index("z.unchecked:")
	assert output == render(Verdict(tuple(results)))


def test_default_report_keeps_later_blockers_visible_after_finding_budget() -> None:
	early_blockers = tuple(
		CheckResult(
			f"check.{index:03d}",
			Status.FAILED,
			f"early failure {index}",
			findings=tuple(
				Finding(path=f"early-{index}-{finding}.py", message="early detail")
				for finding in range(22 if index == 0 else MAX_FINDINGS_PER_CHECK)
			),
			recovery_action=f"repair early failure {index}",
		)
		for index in range(5)
	)
	late_failed = CheckResult(
		"late.failed",
		Status.FAILED,
		"late failure",
		findings=(Finding(path="late-failed.py", message="late failed detail"),),
		recovery_action="repair the late failure",
	)
	late_unchecked = CheckResult(
		"late.unchecked",
		Status.UNCHECKED,
		"late verification unavailable",
		findings=(Finding(path="late-unchecked.py", message="late unchecked detail"),),
		recovery_action="restore the missing runtime",
	)
	verdict = Verdict((*early_blockers, late_failed, late_unchecked))

	output = render(verdict)
	verbose = render(verdict, verbose=True)

	assert verdict.exit_code == EXPECTED_UNCHECKED_EXIT
	assert output.splitlines()[0] == "Quality Gate: 2"
	assert "Observed results: 6 failed, 1 unchecked" in output
	assert "late.failed: failed - late failure" in output
	assert "late.unchecked: unchecked - late verification unavailable" in output
	assert "recovery: repair the late failure" in output
	assert "recovery: restore the missing runtime" in output
	assert "check.000: 2 findings omitted by report limits" in output
	assert "late.failed: 1 finding omitted by report limits" in output
	assert "late.unchecked: 1 finding omitted by report limits" in output
	assert "4 known findings omitted across 3 checks" in output
	assert output.count("early detail") == EXPECTED_DISPLAYED_FINDINGS
	assert "late failed detail" not in output
	assert "late unchecked detail" not in output
	assert "late.failed: failed" in verbose
	assert "late.unchecked: unchecked" in verbose
	assert "late failed detail" in verbose
	assert "late unchecked detail" in verbose


def test_default_report_counts_per_check_and_total_omissions_once() -> None:
	results = [
		_failed_result(
			"a.dense",
			MAX_FINDINGS_PER_CHECK + PER_CHECK_OMISSION,
			omitted_findings=KNOWN_UPSTREAM_OMISSION,
		)
	]
	results.extend(
		_failed_result(f"b.layer{index}", MAX_FINDINGS_PER_CHECK)
		for index in range(MAX_FINDINGS_TOTAL // MAX_FINDINGS_PER_CHECK - UPSTREAM_MARKER_LIMIT)
	)
	results.append(_failed_result("z.tail", TOTAL_OMISSION))

	output = render(Verdict(tuple(results)))
	detail_lines = [line for line in output.splitlines() if "detail-" in line]

	assert len(detail_lines) == MAX_FINDINGS_TOTAL
	assert f"{PER_CHECK_OMISSION} findings omitted by report limits" in output
	assert f"{TOTAL_OMISSION} findings omitted by report limits" in output
	assert f"{KNOWN_UPSTREAM_OMISSION} findings omitted upstream" in output
	assert (
		f"{PER_CHECK_OMISSION + TOTAL_OMISSION + KNOWN_UPSTREAM_OMISSION} known findings "
		f"omitted across {EXPECTED_OMITTING_CHECK_COUNT} checks" in output
	)
	assert "detail-z.tail-0" not in output
	assert "z.tail: failed - summary for z.tail" in output
	assert "recovery: recover z.tail" in output


@pytest.mark.parametrize(
	("check_count", "expected_detail_count"),
	(
		(SINGLE_FINDING_COUNT, MAX_FINDINGS_PER_CHECK),
		(MAX_FINDINGS_TOTAL // MAX_FINDINGS_PER_CHECK, MAX_FINDINGS_TOTAL),
	),
)
def test_default_report_does_not_announce_omissions_at_exact_budgets(
	check_count: int, expected_detail_count: int
) -> None:
	results = tuple(
		_failed_result(f"check.{index}", MAX_FINDINGS_PER_CHECK) for index in range(check_count)
	)

	output = render(Verdict(results))
	detail_lines = [line for line in output.splitlines() if "detail-" in line]

	assert len(detail_lines) == expected_detail_count
	assert "omitted" not in output


def test_verbose_report_lists_every_available_finding() -> None:
	result = _failed_result("check.details", MAX_FINDINGS_TOTAL + SINGLE_FINDING_COUNT)

	default_output = render(Verdict((result,)))
	verbose_output = render(Verdict((result,)), verbose=True)

	assert sum("detail-" in line for line in default_output.splitlines()) == MAX_FINDINGS_PER_CHECK
	assert (
		sum("detail-" in line for line in verbose_output.splitlines())
		== MAX_FINDINGS_TOTAL + SINGLE_FINDING_COUNT
	)
	assert "known findings omitted" not in verbose_output


def test_report_marks_upstream_truncation_with_unknown_count() -> None:
	result = git_conflict_markers(
		None,
		[("conflict.txt", b"<<<<<<< HEAD\n=======\n")],
		max_findings=UPSTREAM_MARKER_LIMIT,
	)

	assert result.status is Status.UNCHECKED
	assert result.omitted_findings is None
	for verbose in (False, True):
		output = render(Verdict((result,)), verbose=verbose)
		assert "additional findings were omitted while checking" in output.casefold()
		assert "exact count unavailable" in output.casefold()
		assert "known findings omitted" not in output


def test_default_and_verbose_reports_preserve_upstream_omissions() -> None:
	known_omissions = Verdict((_result_with_upstream_omissions(KNOWN_UPSTREAM_OMISSION),))
	unknown_omissions = Verdict((_result_with_upstream_omissions(None),))

	default = render(known_omissions)
	verbose = render(known_omissions, verbose=True)
	unknown_default = render(unknown_omissions)
	unknown_verbose = render(unknown_omissions, verbose=True)

	assert "runtime.verify: unchecked - verification could not inspect every finding" in default
	assert "runtime.verify: 1 finding omitted by report limits" in default
	assert "runtime.verify: 5 findings omitted upstream" in default
	assert "6 known findings omitted across 1 check" in default
	assert "recovery: restore complete verification" in default
	assert "runtime.verify: 5 findings omitted upstream" in verbose
	assert verbose.count("available detail") == EXPECTED_AVAILABLE_FINDINGS
	assert "omitted by report limits" not in verbose
	assert (
		"runtime.verify: additional findings were omitted while checking; exact count unavailable"
		in unknown_default
	)
	assert (
		"runtime.verify: additional findings were omitted while checking; exact count unavailable"
		in unknown_verbose
	)
	assert "1 known finding omitted across 1 check" in unknown_default
	assert "recovery: restore complete verification" in unknown_verbose


def test_report_redacts_all_finding_fields_and_recovery_in_both_modes() -> None:
	secret = "ghp_" + "a" * 24
	result = CheckResult(
		check_id="security.credentials",
		status=Status.UNCHECKED,
		summary=f"token={secret}",
		findings=(
			Finding(
				path=f"secrets/{secret}.txt",
				message=f"found {secret}",
				action=f"rotate {secret}",
			),
		)
		* (MAX_FINDINGS_PER_CHECK + SINGLE_FINDING_COUNT),
		recovery_action=f"rotate {secret}",
	)

	for verbose in (False, True):
		output = render(Verdict((result,)), verbose=verbose)
		assert secret not in output
		assert "<REDACTED>" in output


def test_report_preserves_status_counts_and_exit_precedence() -> None:
	results = (
		CheckResult("status.passed", Status.PASSED, "check passed"),
		CheckResult(
			"status.not_applicable",
			Status.NOT_APPLICABLE,
			"check does not apply",
			recovery_action="declare applicability when required",
		),
		CheckResult(
			"status.waived",
			Status.WAIVED,
			"exact waiver applies",
			waiver_target="config.env",
		),
		CheckResult(
			"status.coverage_report",
			Status.PASSED,
			"coverage report: TOTAL 10 1 90%",
		),
		_failed_result("status.failed", SINGLE_FINDING_COUNT),
		CheckResult(
			"status.unchecked",
			Status.UNCHECKED,
			"verification unavailable",
			recovery_action="restore verification",
		),
	)

	verdict = Verdict(results)
	output = render(verdict)
	verbose = render(verdict, verbose=True)

	assert verdict.exit_code == EXPECTED_UNCHECKED_EXIT
	assert output.splitlines()[0] == "Quality Gate: 2"
	assert (
		"Observed results: 1 passed, 1 failed, 1 unchecked, 1 waived, "
		"1 not applicable, 1 report-only result" in output
	)
	assert "status.failed: failed - summary for status.failed" in output
	assert "status.unchecked: unchecked - verification unavailable" in output
	assert "recovery: recover status.failed" in output
	assert "recovery: restore verification" in output
	assert "Report-only: status.coverage_report - coverage report: TOTAL 10 1 90%" in output
	assert "status.passed: passed" not in output
	assert "status.not_applicable: not_applicable" not in output
	assert "status.waived: waived" not in output
	for check_id, status in (
		("status.passed", Status.PASSED),
		("status.failed", Status.FAILED),
		("status.unchecked", Status.UNCHECKED),
		("status.not_applicable", Status.NOT_APPLICABLE),
		("status.waived", Status.WAIVED),
		("status.coverage_report", Status.PASSED),
	):
		assert f"{check_id}: {status.value} -" in verbose


def test_default_report_preserves_upstream_omissions_for_nonblocking_results() -> None:
	waived = CheckResult(
		"status.waived_omissions",
		Status.WAIVED,
		"exact waiver applies",
		waiver_target="config.env",
		omitted_findings=3,
	)

	output = render(Verdict((waived,)))

	assert "Observed results: 1 waived" in output
	assert "status.waived_omissions: 3 findings omitted upstream" in output
	assert "3 known findings omitted across 1 check" in output


def test_empty_report_does_not_claim_successful_checks() -> None:
	verdict = Verdict(())
	output = render(verdict)
	verbose = render(verdict, verbose=True)

	assert output.splitlines() == ["Quality Gate: 0", "No check results were reported."]
	assert verbose == output
	assert "passed" not in output
