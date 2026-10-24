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
	assert f"{PER_CHECK_OMISSION} findings omitted" in output
	assert f"{TOTAL_OMISSION} findings omitted" in output
	assert f"{KNOWN_UPSTREAM_OMISSION} findings omitted upstream" in output
	assert (
		f"{PER_CHECK_OMISSION + TOTAL_OMISSION + KNOWN_UPSTREAM_OMISSION} "
		"known findings omitted across "
		f"{EXPECTED_OMITTING_CHECK_COUNT} checks" in output
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


def test_report_preserves_nonblocking_statuses_and_exit_precedence() -> None:
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
			"status.report_only",
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

	assert verdict.exit_code == EXPECTED_UNCHECKED_EXIT
	assert "coverage report: TOTAL 10 1 90%" in output
	for status in Status:
		assert f"status.{status.value}: {status.value} -" in output
