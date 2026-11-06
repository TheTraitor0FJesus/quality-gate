"""Dependency metadata and bounded deptry report handling."""

from __future__ import annotations

import io
import json
import re
import subprocess
import tokenize
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .contracts import CheckResult, Finding, Manifest, Status, redact
from .launcher import PreparedEnvironment
from .runner_context import _remaining, _safe_environment
from .runner_errors import QualityGateError
from .runner_subprocess import OutputReadError

if TYPE_CHECKING:
	from .runner_context import PythonComponent

EXIT_UNCHECKED = 2
MAX_DEPTRY_REPORT_BYTES = 1024 * 1024
MAX_DEPTRY_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DEPTRY_TEXT_CHARS = 1000
_DEPTRY_MODULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_DEPTRY_INLINE_IGNORE = re.compile(r"#\s*deptry:\s*ignore(?:\s*\[[A-Z0-9,\s]+\])?(?=\s|$)")
_DEPTRY_SUPPRESSION_KEYS = {
	"exclude",
	"extend_exclude",
	"ignore",
	"ignore_notebooks",
	"per_rule_ignores",
}


def _tool_command(prepared: PreparedEnvironment, python: Path, name: str) -> list[str]:
	release_manifest = getattr(prepared, "release_manifest", None)
	if release_manifest is not None:
		for tool in release_manifest.tools:
			if tool.name == name:
				if tool.path.lower().endswith(".whl"):
					return [str(python), "-m", name]
				return [str(prepared.policy_root / tool.path)]
	return [str(python), "-m", name]


def _dependency_configuration(pyprojects: list[Path]) -> dict[str, object]:
	if len(pyprojects) > 1:
		raise QualityGateError(
			"deptry accepts at most one pyproject.toml for a Python component",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare one authoritative pyproject.toml per Python component",
		)
	if not pyprojects:
		return {}
	try:
		if pyprojects[0].stat().st_size > MAX_DEPTRY_REPORT_BYTES:
			raise QualityGateError(
				"dependency metadata exceeds the size limit",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="reduce the dependency metadata size and retry",
			)
		content = pyprojects[0].read_bytes()
		if len(content) > MAX_DEPTRY_REPORT_BYTES:
			raise QualityGateError(
				"dependency metadata exceeds the size limit",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="reduce the dependency metadata size and retry",
			)
		metadata = tomllib.loads(content.decode("utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise QualityGateError(
			"deptry configuration metadata is unavailable or invalid",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore valid dependency metadata and retry",
		) from error
	tool = metadata.get("tool", {})
	candidate = tool.get("deptry", {}) if isinstance(tool, dict) else {}
	configuration = candidate if isinstance(candidate, dict) else {}
	for key in sorted(_DEPTRY_SUPPRESSION_KEYS.intersection(configuration)):
		raise QualityGateError(
			f"tool.deptry.{key} bypasses the typed waiver contract",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action=f"remove tool.deptry.{key} and use one exact current manifest waiver",
		)
	return configuration


def _dependency_requirement_groups(
	actual_root: Path,
	component: PythonComponent,
	pyprojects: list[Path],
	configuration: dict[str, object],
) -> tuple[list[Path], list[Path]]:
	runtime_names = configuration.get("requirements_files", ["requirements.txt"])
	development_names = configuration.get(
		"requirements_files_dev", ["dev-requirements.txt", "requirements-dev.txt"]
	)
	if not isinstance(runtime_names, list) or not all(
		isinstance(value, str) for value in runtime_names
	):
		raise QualityGateError(
			"tool.deptry.requirements_files must classify dependency input paths",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare valid runtime requirement paths in tool.deptry",
		)
	if not isinstance(development_names, list) or not all(
		isinstance(value, str) for value in development_names
	):
		raise QualityGateError(
			"tool.deptry.requirements_files_dev must classify dependency input paths",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare valid development requirement paths in tool.deptry",
		)
	runtime_set = {value.replace("\\", "/") for value in runtime_names}
	development_set = {value.replace("\\", "/") for value in development_names}
	runtime_requirements: list[Path] = []
	development_requirements: list[Path] = []
	for path in component.dependency_inputs:
		if path in pyprojects:
			continue
		relative = str(path.relative_to(actual_root)).replace("\\", "/")
		if relative in development_set or path.name in development_set:
			development_requirements.append(path)
		elif relative in runtime_set or path.name in runtime_set:
			runtime_requirements.append(path)
		else:
			raise QualityGateError(
				f"dependency input is not classified as runtime or development: {relative}",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action=(
					"classify every requirements input in tool.deptry requirements_files or "
					"requirements_files_dev"
				),
			)
	return runtime_requirements, development_requirements


def _dependency_command(
	actual_root: Path,
	component: PythonComponent,
	prepared: PreparedEnvironment,
	python_executable: Path,
	report_path: Path,
) -> list[str]:
	pyprojects = [path for path in component.dependency_inputs if path.name == "pyproject.toml"]
	configuration = _dependency_configuration(pyprojects)
	runtime_requirements, development_requirements = _dependency_requirement_groups(
		actual_root, component, pyprojects, configuration
	)
	if pyprojects and (runtime_requirements or development_requirements):
		raise QualityGateError(
			"deptry cannot analyze mixed pyproject.toml and requirements dependency inputs",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action=(
				"declare one authoritative dependency metadata format per Python component"
			),
		)
	command = [
		*_tool_command(prepared, python_executable, "deptry"),
		str(component.path.relative_to(actual_root)),
		"--json-output",
		str(report_path),
		"--no-ansi",
		"--ignore-notebooks",
		"--exclude",
		r"(?:.*/)?venv(?:/|$)",
		"--exclude",
		r"(?:.*/)?\.venv(?:/|$)",
		"--exclude",
		r"(?:.*/)?\.direnv(?:/|$)",
		"--exclude",
		r"(?:.*/)?tests(?:/|$)",
		"--exclude",
		r"(?:.*/)?\.git(?:/|$)",
		"--exclude",
		r"(?:.*/)?setup\.py$",
	]
	if pyprojects:
		command.extend(("--config", str(pyprojects[0].relative_to(actual_root))))
	else:
		controlled_config = report_path.with_name("deptry-config.toml")
		controlled_config.write_text("", encoding="utf-8")
		command.extend(("--config", str(controlled_config)))
	for test_path in component.test_paths:
		relative_test = str(test_path.relative_to(actual_root)).replace("\\", "/")
		command.extend(("--exclude", rf"{re.escape(relative_test)}(?:/|$)"))
	if runtime_requirements:
		command.extend(
			(
				"--requirements-files",
				",".join(str(path.relative_to(actual_root)) for path in runtime_requirements),
			)
		)
	if development_requirements:
		command.extend(
			(
				"--requirements-files-dev",
				",".join(str(path.relative_to(actual_root)) for path in development_requirements),
			)
		)
	return command


def _dependency_issue(value: object) -> tuple[Finding, str]:
	if not isinstance(value, dict):
		raise ValueError("deptry issue is not an object")
	error = value.get("error")
	location = value.get("location")
	module = value.get("module")
	if not isinstance(error, dict) or not isinstance(location, dict) or not isinstance(module, str):
		raise ValueError("deptry issue fields are invalid")
	code = error.get("code")
	message = error.get("message")
	path = location.get("file")
	line = location.get("line")
	if (
		code not in {"DEP001", "DEP002", "DEP003", "DEP004"}
		or not isinstance(message, str)
		or not isinstance(path, str)
		or not _DEPTRY_MODULE.fullmatch(module)
		or len(message) > MAX_DEPTRY_TEXT_CHARS
		or not message.isprintable()
		or (line is not None and (not isinstance(line, int) or line < 1))
	):
		raise ValueError("deptry issue contains an unsupported value")
	normalized_path = path.replace("\\", "/")
	parsed_path = PurePosixPath(normalized_path)
	if (
		len(normalized_path) > MAX_DEPTRY_TEXT_CHARS
		or not normalized_path
		or parsed_path.is_absolute()
		or normalized_path == "."
		or ".." in parsed_path.parts
		or not normalized_path.isprintable()
		or ":" in parsed_path.parts[0]
	):
		raise ValueError("deptry issue path is not repository-relative")
	target = f"{normalized_path}::{code}::{module}"
	return (
		Finding(
			path=normalized_path,
			line=line,
			message=f"{code} {message}",
			action=f"fix the dependency declaration or add a reviewed waiver for {target}",
		),
		target,
	)


def _dependency_inline_suppressions(
	actual_root: Path, component: PythonComponent
) -> tuple[Finding, ...]:
	findings = []
	for path in sorted(component.path.rglob("*.py")):
		if any(
			path == test_path or test_path in path.parents for test_path in component.test_paths
		):
			continue
		try:
			if path.stat().st_size > MAX_DEPTRY_SOURCE_BYTES:
				raise QualityGateError(
					"Python source exceeds the dependency-analysis size limit",
					check_id="runtime.command",
					exit_code=EXIT_UNCHECKED,
					recovery_action="split the oversized Python source and retry",
				)
			content = path.read_bytes()
		except OSError as error:
			raise QualityGateError(
				"Python source is unreadable during dependency suppression validation",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="restore readable Python source and retry",
			) from error
		if len(content) > MAX_DEPTRY_SOURCE_BYTES:
			raise QualityGateError(
				"Python source exceeds the dependency-analysis size limit",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="split the oversized Python source and retry",
			)
		try:
			comments = tuple(
				token
				for token in tokenize.tokenize(io.BytesIO(content).readline)
				if token.type == tokenize.COMMENT
			)
		except (SyntaxError, tokenize.TokenError) as error:
			raise QualityGateError(
				"Python source cannot be tokenized for dependency suppression validation",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="restore parseable Python source and retry",
			) from error
		for comment in comments:
			if not _DEPTRY_INLINE_IGNORE.search(comment.string):
				continue
			findings.append(
				Finding(
					path=str(path.relative_to(actual_root)).replace("\\", "/"),
					line=comment.start[0],
					message="inline deptry suppression bypasses the typed waiver contract",
					action="remove the inline suppression and use an exact manifest waiver",
				)
			)
	return tuple(findings)


def _read_dependency_report(report_path: Path) -> list[object]:
	try:
		if report_path.stat().st_size > MAX_DEPTRY_REPORT_BYTES:
			raise ValueError("deptry report exceeds the size limit")
		content = report_path.read_bytes()
	except OSError as error:
		raise ValueError("deptry report is unavailable") from error
	if len(content) > MAX_DEPTRY_REPORT_BYTES:
		raise ValueError("deptry report exceeds the size limit")
	try:
		value = json.loads(content.decode("utf-8"))
	except (UnicodeError, json.JSONDecodeError) as error:
		raise ValueError("deptry report is not valid UTF-8 JSON") from error
	if not isinstance(value, list):
		raise ValueError("deptry report is not a list")
	return value


def _dependency_hygiene_result(
	actual_root: Path,
	manifest: Manifest,
	component: PythonComponent,
	prepared: PreparedEnvironment,
	component_index: int,
	python_executable: Path | None,
	report_path: Path,
	deadline: float,
	run_bounded: Callable[[list[str], Path, dict[str, str], float | None], tuple[int, str]],
) -> CheckResult:
	"""Run deptry and return a result honoring exact current dependency waivers.

	Use the one-based component_index in the check ID and write JSON to report_path.
	The absolute monotonic deadline bounds the command timeout. Missing runtime or
	metadata, caught execution errors, and invalid reports become UNCHECKED;
	components with no dependency inputs are NOT_APPLICABLE."""
	check_id = f"python.component_{component_index}.deptry"
	if not component.dependency_inputs:
		return CheckResult(
			check_id,
			Status.NOT_APPLICABLE,
			"dependency hygiene is not applicable without dependency inputs",
			recovery_action="declare dependency inputs when the component has dependencies",
		)
	missing_inputs = [path for path in component.dependency_inputs if not path.is_file()]
	if python_executable is None or missing_inputs:
		reason = (
			"the component runtime is unavailable"
			if python_executable is None
			else "dependency metadata is unavailable"
		)
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			reason,
			findings=tuple(
				Finding(
					path=str(path.relative_to(actual_root)).replace("\\", "/"),
					message="declared dependency input does not exist",
					action="restore the declared dependency input",
				)
				for path in missing_inputs
			),
			recovery_action="restore the component runtime and dependency metadata, then retry",
		)
	try:
		inline_suppressions = _dependency_inline_suppressions(actual_root, component)
		if inline_suppressions:
			return CheckResult(
				check_id,
				Status.FAILED,
				"inline deptry suppressions bypass the typed waiver contract",
				findings=inline_suppressions,
				recovery_action="remove every inline deptry suppression and retry",
			)
		command = _dependency_command(
			actual_root, component, prepared, python_executable, report_path
		)
		returncode, output = run_bounded(
			command,
			actual_root,
			_safe_environment(str(report_path.parent)),
			_remaining(deadline, manifest.repository.command_timeout_seconds),
		)
	except (OSError, subprocess.TimeoutExpired, OutputReadError, QualityGateError) as error:
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			"dependency analysis could not run",
			findings=(Finding(message=redact(str(error))),),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	try:
		raw_issues = _read_dependency_report(report_path)
		issues = tuple(_dependency_issue(value) for value in raw_issues)
	except ValueError as error:
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			"dependency analysis report is unavailable or invalid",
			findings=(Finding(message=redact(output.strip() or str(error))),),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	if returncode not in {0, 1} or (returncode == 0 and issues) or (returncode == 1 and not issues):
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			"dependency analysis returned an unusable result",
			findings=(
				Finding(message=redact(output.strip() or f"deptry exited with {returncode}")),
			),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	if not issues:
		return CheckResult(check_id, Status.PASSED, "dependency declarations match imports")
	unwaived = tuple(
		finding for finding, target in issues if manifest.resolve_waiver(check_id, target) is None
	)
	if not unwaived:
		return CheckResult(
			check_id,
			Status.WAIVED,
			"dependency findings have exact current waivers",
			findings=tuple(finding for finding, _target in issues),
			waiver_target=issues[0][1],
		)
	return CheckResult(
		check_id,
		Status.FAILED,
		"dependency declarations do not match imports",
		findings=unwaived,
		recovery_action="fix each dependency finding or add one exact current human waiver",
	)
