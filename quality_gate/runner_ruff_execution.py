"""Execute and parse bounded Ruff file-local verification."""

from __future__ import annotations

from pathlib import Path

from . import runner as _runner
from .runner_ruff_reports import _ruff_report_findings, _ruff_unformatted


def _run_with_status(
	command: list[str], root: Path, environment: dict[str, str], *, timeout: float
) -> tuple[int, str]:
	"""Use the operation-owned subprocess path and retain the machine-report exit code."""
	try:
		return _runner._run_bounded_subprocess(command, root, environment, timeout)
	except OSError as error:
		raise _runner.QualityGateError(
			f"Ruff could not be executed: {error}",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
		) from error
	except (_runner.subprocess.TimeoutExpired, _runner.OutputReadError) as error:
		raise _runner.QualityGateError(
			f"Ruff verification could not complete: {error}",
			check_id="runtime.timeout",
			exit_code=_runner.EXIT_UNCHECKED,
		) from error


def _eligible_files(
	tool_command: list[str],
	common: list[str],
	arguments: list[str],
	root: Path,
	environment: dict[str, str],
	deadline: float,
	gate_timeout: int,
) -> list[str]:
	show_output = _runner.run(
		[*tool_command, "check", *common, "--show-files", "--", *arguments],
		root,
		_runner._ruff_environment(environment),
		timeout=_runner._remaining(deadline, gate_timeout),
	)
	eligible: list[str] = []
	for line in show_output.splitlines():
		candidate = Path(line)
		candidate = candidate if candidate.is_absolute() else root / candidate
		path = candidate.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()
		if not path.startswith(arguments[0].rstrip("/") + "/") or Path(path).suffix not in {
			".py",
			".pyi",
		}:
			raise ValueError("Ruff selected a path outside the declared Python component")
		eligible.append(path)
	return sorted(set(eligible))


def _check_import_rules(
	tool_command: list[str],
	common: list[str],
	arguments: list[str],
	root: Path,
	environment: dict[str, str],
	deadline: float,
	gate_timeout: int,
	eligible: list[str],
) -> list[dict[str, object]]:
	fresh_import_code, fresh_import_output = _run_with_status(
		[
			*tool_command,
			"check",
			*common,
			"--select",
			"I",
			"--no-fix",
			"--output-format",
			"json",
			"--",
			*arguments,
		],
		root,
		_runner._ruff_environment(environment),
		timeout=_runner._remaining(deadline, gate_timeout),
	)
	all_paths = set(eligible)
	import_findings = _ruff_report_findings(fresh_import_code, fresh_import_output, root, all_paths)
	if any(
		finding["rule"] != "invalid-syntax" and not str(finding["rule"]).startswith("I")
		for finding in import_findings
	):
		raise ValueError("Ruff import-rule pass returned a non-import finding")

	return import_findings


def _local_lint(
	tool_command: list[str],
	common: list[str],
	targets: list[str],
	root: Path,
	environment: dict[str, str],
	deadline: float,
	timeout: int,
) -> tuple[dict[str, list[dict[str, object]]], set[str]]:
	if not targets:
		return {}, set()
	code, output = _run_with_status(
		[
			*tool_command,
			"check",
			*common,
			"--no-fix",
			"--extend-ignore",
			"I",
			"--output-format",
			"json",
			"--",
			*targets,
		],
		root,
		_runner._ruff_environment(environment),
		timeout=_runner._remaining(deadline, timeout),
	)
	findings = _ruff_report_findings(code, output, root, set(targets))
	if any(str(finding["rule"]).startswith("I") for finding in findings):
		raise ValueError("Ruff local pass returned an import-dependent finding")
	by_path: dict[str, list[dict[str, object]]] = {}
	for finding in findings:
		by_path.setdefault(str(finding["path"]), []).append(finding)
	return by_path, {
		str(finding["path"]) for finding in findings if finding["rule"] == "invalid-syntax"
	}


def _local_format(
	tool_command: list[str],
	common: list[str],
	targets: list[str],
	root: Path,
	environment: dict[str, str],
	deadline: float,
	timeout: int,
	syntax_paths: set[str],
) -> set[str]:
	if not targets:
		return set()
	code, output = _run_with_status(
		[*tool_command, "format", *common, "--check", "--output-format", "json", "--", *targets],
		root,
		_runner._ruff_environment(environment),
		timeout=_runner._remaining(deadline, timeout),
	)
	return _ruff_unformatted(code, output, root, set(targets), syntax_paths)
