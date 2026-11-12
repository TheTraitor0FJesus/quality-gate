"""Construct Python component commands and their verdicts."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import CheckResult, Finding, Manifest, Status, redact
from .launcher import PreparedEnvironment
from .runner_dependency import _tool_command
from .runner_errors import QualityGateError

if TYPE_CHECKING:
	from .runner_context import PythonComponent

EXIT_UNCHECKED = 2


def _has_pinned_coverage(prepared: PreparedEnvironment) -> bool:
	"""Return whether the selected policy release pins a coverage provider."""
	release_manifest = getattr(prepared, "release_manifest", None)
	return release_manifest is not None and any(
		tool.name.casefold() == "coverage" for tool in release_manifest.tools
	)


def _component_commands(
	actual_root: Path,
	manifest: Manifest,
	component: PythonComponent,
	prepared: PreparedEnvironment,
	component_index: int,
	python_executable: Path,
) -> list[tuple[str, list[str], int]]:
	prefix = f"python.component_{component_index}"
	arguments = [str(component.path.relative_to(actual_root))]
	commands: list[tuple[str, list[str], int]] = [
		(
			f"{prefix}.ruff",
			[
				*_tool_command(prepared, python_executable, "ruff"),
				"check",
				"--config",
				str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
				*arguments,
			],
			manifest.repository.command_timeout_seconds,
		),
		(
			f"{prefix}.format",
			[
				*_tool_command(prepared, python_executable, "ruff"),
				"format",
				"--check",
				"--config",
				str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
				*arguments,
			],
			manifest.repository.command_timeout_seconds,
		),
	]
	if component.typecheck:
		commands.append(
			(
				f"{prefix}.mypy",
				[
					*_tool_command(prepared, python_executable, "mypy"),
					"--config-file",
					str(prepared.policy_root / "quality_gate" / "policy" / "mypy.ini"),
					*arguments,
				],
				manifest.repository.command_timeout_seconds,
			)
		)
	if component.test_paths:
		test_arguments = [
			*(str(test_path.relative_to(actual_root)) for test_path in component.test_paths),
			"-q",
			"-p",
			"no:cacheprovider",
		]
		commands.append(
			(
				f"{prefix}.pytest",
				[
					*_tool_command(prepared, python_executable, "pytest"),
					*test_arguments,
				],
				manifest.repository.test_timeout_seconds,
			)
		)
		if _has_pinned_coverage(prepared):
			commands.extend(
				(
					(
						f"{prefix}.coverage",
						[
							*_tool_command(prepared, python_executable, "coverage"),
							"run",
							"-m",
							"pytest",
							*test_arguments,
						],
						manifest.repository.test_timeout_seconds,
					),
					(
						f"{prefix}.coverage_report",
						[
							*_tool_command(prepared, python_executable, "coverage"),
							"report",
							"--show-missing",
						],
						manifest.repository.command_timeout_seconds,
					),
				)
			)
	return commands


def _command_results(
	executed: list[str],
	run_errors: list[QualityGateError],
	run_outputs: dict[str, str],
) -> list[CheckResult]:
	results: list[CheckResult] = []
	for check_id in executed:
		errors = [error for error in run_errors if error.check_id == check_id]
		is_coverage_collection = check_id.endswith(".coverage")
		is_coverage_report = check_id.endswith(".coverage_report")
		is_coverage = is_coverage_collection or is_coverage_report
		status = Status.PASSED
		if errors and not is_coverage:
			status = (
				Status.UNCHECKED
				if any(error.exit_code == EXIT_UNCHECKED for error in errors)
				else Status.FAILED
			)
		if is_coverage:
			status = Status.PASSED
		coverage_output = run_outputs.get(check_id, "")
		summary = "check passed" if status is Status.PASSED else "check requires attention"
		recovery_action = "restore verification and fix the reported finding, then retry"
		if is_coverage_collection:
			summary = "coverage collection completed"
		if is_coverage_report:
			report_lines = [line.strip() for line in coverage_output.splitlines() if line.strip()]
			report_line = next(
				(line for line in reversed(report_lines) if line.startswith("TOTAL")),
				report_lines[-1] if report_lines else "coverage report generated",
			)
			summary = f"coverage report: {redact(report_line)}"
			recovery_action = "coverage is report-only and does not affect the quality verdict"
		if errors and is_coverage:
			summary = "coverage report unavailable; report-only: " + redact(str(errors[0]))
		results.append(
			CheckResult(
				check_id=check_id,
				status=status,
				summary=summary,
				findings=()
				if is_coverage
				else tuple(Finding(message=redact(str(error))) for error in errors),
				recovery_action=None if is_coverage else recovery_action if errors else None,
			)
		)
	return results
