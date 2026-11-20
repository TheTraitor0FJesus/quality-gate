"""Execute and parse bounded Ruff file-local verification."""

from __future__ import annotations

from pathlib import Path

from . import runner as _runner
from .evidence_identity import RUFF_LOCAL_FORMAT, RUFF_LOCAL_LINT
from .runner_ruff_reports import _ruff_report_findings, _ruff_unformatted


def _run_with_status(
	command: list[str], root: Path, environment: dict[str, str], *, timeout: float
) -> tuple[int, str]:
	"""Use the operation-owned subprocess path and retain the machine-report exit code."""
	operation = _runner.current_operation()
	if operation is None:
		raise ValueError("Ruff evidence requires an active operation")
	code, output, _diagnostics = operation.run_process(
		command, root=root, environment=environment, timeout_seconds=timeout
	)
	return code, output.decode("utf-8", errors="strict")


def _eligible_files(
	tool_command: list[str],
	common: list[str],
	arguments: list[str],
	root: Path,
	environment: dict[str, str],
	deadline: float,
	gate_timeout: int,
) -> list[str]:
	code, show_output = _run_with_status(
		[*tool_command, "check", *common, "--show-files", "--", *arguments],
		root,
		_runner._ruff_environment(environment),
		timeout=_runner._remaining(deadline, gate_timeout),
	)
	if code != 0:
		raise ValueError("Ruff could not enumerate the selected files")
	resolved_root = root.resolve(strict=True)
	eligible: list[str] = []
	for line in show_output.split("\n"):
		if not line:
			continue
		line = line.removesuffix("\r")
		candidate = Path(line)
		candidate = candidate if candidate.is_absolute() else root / candidate
		path = candidate.resolve(strict=True).relative_to(resolved_root).as_posix()
		if not Path(path).is_relative_to(arguments[0]) or Path(path).suffix not in {
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
		[*tool_command, "check", *common, *RUFF_LOCAL_LINT, "--", *targets],
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
) -> set[str]:
	if not targets:
		return set()
	code, output = _run_with_status(
		[*tool_command, "format", *common, *RUFF_LOCAL_FORMAT, "--", *targets],
		root,
		_runner._ruff_environment(environment),
		timeout=_runner._remaining(deadline, timeout),
	)
	return _ruff_unformatted(code, output, root, set(targets))
