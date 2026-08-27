"""Deterministic quality checks used by the local hook and CI."""

from __future__ import annotations

import json
import locale
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from quality_gate.contracts import (
	CheckResult,
	Finding,
	Manifest,
	Status,
	ValidationError,
	Verdict,
	load_manifest,
	redact,
)
from quality_gate.distribution import DistributionError
from quality_gate.integrity import documentation_results, git_integrity_results, workflow_result
from quality_gate.launcher import PreparedEnvironment, prepare
from quality_gate.lessons import lessons_result
from quality_gate.reporting import render
from quality_gate.runtime import RuntimeUnavailable
from quality_gate.secrets import secret_audit_result, secret_candidate_result, secret_history_result
from quality_gate.snapshot import SnapshotError, candidate_snapshot

_LOGGER = logging.getLogger(__name__)
MANIFEST_NAME = "quality-gate.toml"
POLICY_DIR = Path(__file__).resolve().parent / "policy"
EXIT_UNCHECKED = 2
MAX_COMMAND_OUTPUT_CHARS = 16_384
MAX_COMMAND_OUTPUT_BYTES = MAX_COMMAND_OUTPUT_CHARS * 4 + 1
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0


class OutputReadError(RuntimeError):
	"""A subprocess output reader could not finish safely."""


class QualityGateError(RuntimeError):
	"""A configuration or quality check prevents completion."""

	exit_code = 1

	def __init__(
		self,
		message: str,
		*,
		check_id: str = "runtime.command",
		exit_code: int = 1,
		recovery_action: str = "restore verification and retry the quality gate",
	) -> None:
		super().__init__(redact(message))
		self.check_id = check_id
		self.exit_code = exit_code
		self.recovery_action = recovery_action


def emit(message: str) -> None:
	sys.stdout.write(f"{message}\n")


def decode_subprocess_output(output: bytes | str | None) -> str:
	"""Decode subprocess output across UTF-8 and Windows console encodings."""
	if output is None:
		return ""
	if isinstance(output, str):
		return output
	encodings = ["utf-8", locale.getpreferredencoding(False)]
	for encoding in dict.fromkeys(encodings):
		try:
			return output.decode(encoding)
		except UnicodeDecodeError:
			continue
	return output.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class PythonComponent:
	path: Path
	tests: Path | None
	requirements: Path | None
	typecheck: bool
	test_paths: tuple[Path, ...] = ()
	timeout_seconds: int = 300
	missing_test_paths: tuple[Path, ...] = ()
	path_exists: bool = True


def _manifest_error(error: ValidationError) -> QualityGateError:
	return QualityGateError(
		f"{error.path}: {error.message}",
		check_id="manifest.contract",
		exit_code=2,
		recovery_action=f"correct {error.path} and run validate",
	)


def repository_root(root: Path | None) -> Path:
	if root is not None:
		return root.resolve()
	result = subprocess.run(
		["git", "rev-parse", "--show-toplevel"],
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	if result.returncode:
		raise QualityGateError(
			"Run quality-gate inside a Git repository or pass --root.",
			check_id="runtime.repository",
			exit_code=2,
			recovery_action="run the command inside a Git repository or pass --root",
		)
	return Path(result.stdout.strip()).resolve()


def relative_path(root: Path, value: object, field: str) -> Path:
	if not isinstance(value, str) or not value:
		raise QualityGateError(
			f"{field} must be a non-empty relative path.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	resolved_root = root.resolve()
	path = (resolved_root / value).resolve()
	if resolved_root not in path.parents and path != resolved_root:
		raise QualityGateError(
			f"{field} must stay inside the repository.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	return path


def load_components(root: Path) -> list[PythonComponent]:
	try:
		manifest = load_manifest(root)
	except ValidationError as error:
		raise _manifest_error(error) from error
	components: list[PythonComponent] = []
	for index, item in enumerate(manifest.python, start=1):
		path = relative_path(root, item.path, f"python entry {index}.path")
		test_paths = tuple(
			relative_path(root, test_path, f"python entry {index}.test_paths[{test_index}]")
			for test_index, test_path in enumerate(item.test_paths)
		)
		missing_tests = [test_path for test_path in test_paths if not test_path.exists()]
		existing_tests = tuple(test_path for test_path in test_paths if test_path.exists())
		components.append(
			PythonComponent(
				path,
				test_paths[0] if test_paths else None,
				None,
				True,
				existing_tests,
				item.timeout_seconds,
				tuple(missing_tests),
				path.is_dir(),
			)
		)
	return components


def validate(root: Path | None = None) -> None:
	actual_root = repository_root(root)
	load_manifest(actual_root)
	emit(f"QUALITY GATE VALID (schema 2): {actual_root / MANIFEST_NAME}")


def required_documents_result(root: Path, manifest: Manifest) -> CheckResult:
	findings: list[Finding] = []
	unreadable = False
	for document in manifest.repository.required_documents:
		target = root / document
		try:
			if not target.is_file():
				findings.append(
					Finding(
						document,
						message="required document is missing",
						action=f"restore {document}",
					)
				)
			elif not target.read_bytes().strip():
				findings.append(
					Finding(
						document, message="required document is empty", action=f"restore {document}"
					)
				)
		except OSError:
			unreadable = True
			findings.append(
				Finding(
					document,
					message="required document is unreadable",
					action=f"restore {document}",
				)
			)
	return CheckResult(
		check_id="manifest.documents",
		status=(Status.UNCHECKED if unreadable else Status.FAILED if findings else Status.PASSED),
		summary="required documents satisfy the schema 2 contract"
		if not findings
		else "required documents are incomplete",
		findings=tuple(findings),
		recovery_action=(
			"restore access to every unreadable document and run check again"
			if unreadable
			else "restore every required document and run check again"
			if findings
			else None
		),
	)


def _run_bounded_subprocess(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	timeout: float | None,
) -> tuple[int, str]:
	"""Run a subprocess while retaining only a bounded amount of output."""
	process = subprocess.Popen(
		command,
		cwd=root,
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
	)
	retained = bytearray()
	reader_errors: list[Exception] = []

	def drain_output() -> None:
		assert process.stdout is not None
		try:
			while True:
				chunk = process.stdout.read(4096)
				if not chunk:
					return
				remaining = MAX_COMMAND_OUTPUT_BYTES - len(retained)
				if remaining > 0:
					retained.extend(chunk[:remaining])
		except (OSError, ValueError) as error:
			reader_errors.append(error)

	reader = threading.Thread(target=drain_output, daemon=True)
	reader.start()
	try:
		returncode = process.wait(timeout=timeout)
	except subprocess.TimeoutExpired:
		process.kill()
		process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
		reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
		raise
	reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader.is_alive():
		raise subprocess.TimeoutExpired(command, PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader_errors:
		raise OutputReadError("subprocess output could not be read") from reader_errors[0]
	return returncode, decode_subprocess_output(bytes(retained))


def run(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	*,
	timeout: float | None = None,
) -> str:
	"""Run one quality command and return its decoded standard output."""
	try:
		returncode, output = _run_bounded_subprocess(command, root, environment, timeout)
	except OSError as error:
		raise QualityGateError(
			f"{' '.join(command)} could not be executed: {error}",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the required tool or runtime and retry the quality gate",
		) from error
	except subprocess.TimeoutExpired as error:
		raise QualityGateError(
			f"{' '.join(command)} timed out after {timeout:g} seconds",
			check_id="runtime.timeout",
			exit_code=2,
			recovery_action="inspect the command and retry within the declared time budget",
		) from error
	except OutputReadError as error:
		raise QualityGateError(
			f"{' '.join(command)} output could not be read",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore subprocess output handling and retry the quality gate",
		) from error
	if returncode:
		detail = output.strip()
		detail = _bounded_output(detail)
		if not detail:
			detail = f"Command exited with {returncode}."
		lower_detail = detail.casefold()
		missing_tool = any(
			f"no module named {tool_name}" in lower_detail
			for tool_name in ("ruff", "mypy", "pytest")
		)
		pytest_collection_error = "pytest" in " ".join(command).casefold() and any(
			marker in lower_detail
			for marker in ("error collecting", "no tests collected", "no tests ran")
		)
		raise QualityGateError(
			f"{' '.join(command)}\n\n{redact(detail)}",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED if missing_tool or pytest_collection_error else 1,
			recovery_action=(
				(
					"restore the required tool in the verification runtime "
					"and retry the quality gate"
					if missing_tool
					else "restore a collectable pytest suite and retry the quality gate"
				)
				if missing_tool or pytest_collection_error
				else "fix the reported quality finding and retry the quality gate"
			),
		)
	return _bounded_output(output).strip()


def _bounded_output(value: str) -> str:
	"""Limit external command output retained by the gate."""
	if len(value) <= MAX_COMMAND_OUTPUT_CHARS:
		return value
	return value[:MAX_COMMAND_OUTPUT_CHARS] + "\n[output truncated]"


def temporary_directory(root: Path) -> tempfile.TemporaryDirectory[str]:
	return tempfile.TemporaryDirectory(prefix="quality-gate-")


def _error_result(error: QualityGateError) -> CheckResult:
	status = Status.UNCHECKED if error.exit_code == EXIT_UNCHECKED else Status.FAILED
	return CheckResult(
		check_id=error.check_id,
		status=status,
		summary=redact(str(error)),
		findings=(Finding(message=redact(str(error))),),
		recovery_action=error.recovery_action,
	)


def _remaining(deadline: float, timeout: int) -> float:
	remaining = min(float(timeout), deadline - time.monotonic())
	if remaining <= 0:
		raise QualityGateError(
			"quality gate exceeded its overall time budget",
			check_id="runtime.gate_timeout",
			exit_code=2,
			recovery_action="inspect slow checks and retry within the gate time budget",
		)
	return remaining


def _ci_history_refs(base: str | None, head: str | None) -> tuple[str | None, str]:
	"""Resolve pull-request commit SHAs from GitHub event data when available."""
	event_path = os.environ.get("GITHUB_EVENT_PATH")
	event_base: str | None = None
	event_head: str | None = None
	if event_path:
		try:
			event = json.loads(Path(event_path).read_text(encoding="utf-8"))
			pull_request = event.get("pull_request", {})
			base_data = pull_request.get("base", {})
			head_data = pull_request.get("head", {})
			if isinstance(base_data, dict) and isinstance(base_data.get("sha"), str):
				event_base = base_data["sha"]
			if isinstance(head_data, dict) and isinstance(head_data.get("sha"), str):
				event_head = head_data["sha"]
		except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
			_LOGGER.debug("GitHub event payload is unavailable", exc_info=True)
	return (
		base or event_base or os.environ.get("GITHUB_BASE_REF"),
		head or event_head or os.environ.get("GITHUB_SHA") or "HEAD",
	)


def _safe_environment(temporary_path: str) -> dict[str, str]:
	allowed = {
		"LANG",
		"LC_ALL",
		"PATHEXT",
		"PATH",
		"SYSTEMROOT",
		"WINDIR",
	}
	environment = {key: value for key, value in os.environ.items() if key in allowed}
	environment.update(
		{
			"HOME": temporary_path,
			"TMP": temporary_path,
			"TEMP": temporary_path,
			"PYTHONIOENCODING": "utf-8",
			"PYTHONUTF8": "1",
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUFF_CACHE_DIR": temporary_path,
			"MYPY_CACHE_DIR": temporary_path,
			"COVERAGE_FILE": str(Path(temporary_path) / ".coverage"),
		}
	)
	if os.name == "nt":
		environment["USERPROFILE"] = temporary_path
	return environment


def _runtime_python(prepared: PreparedEnvironment, component_index: int) -> Path:
	try:
		inspection = prepared.runtimes[component_index - 1]
	except IndexError as error:
		raise QualityGateError(
			f"runtime is unavailable for Python component {component_index}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		) from error
	if not inspection.current or inspection.python is None:
		raise QualityGateError(
			f"runtime is unavailable for Python component {component_index}: "
			f"{inspection.reason or 'runtime is stale'}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		)
	return inspection.python


def _tool_command(prepared: PreparedEnvironment, python: Path, name: str) -> list[str]:
	release_manifest = getattr(prepared, "release_manifest", None)
	if release_manifest is not None:
		for tool in release_manifest.tools:
			if tool.name == name:
				if tool.path.lower().endswith(".whl"):
					return [str(python), "-m", name]
				return [str(prepared.policy_root / tool.path)]
	return [str(python), "-m", name]


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


def _execute_commands(
	actual_root: Path,
	environment: dict[str, str],
	commands: list[tuple[str, list[str], int]],
	component_timeout: int,
	deadline: float,
) -> tuple[list[str], list[QualityGateError], dict[str, str]]:
	executed: list[str] = []
	run_errors: list[QualityGateError] = []
	run_outputs: dict[str, str] = {}
	for check_id, command, timeout in commands:
		executed.append(check_id)
		try:
			output = run(
				command,
				actual_root,
				environment,
				timeout=_remaining(deadline, max(timeout, component_timeout)),
			)
			if output and check_id.endswith(".coverage_report"):
				run_outputs[check_id] = output
		except QualityGateError as error:
			error.check_id = check_id
			run_errors.append(error)
	return executed, run_errors, run_outputs


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


def _run_python_checks(
	actual_root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
) -> list[CheckResult]:
	deadline = time.monotonic() + manifest.repository.gate_timeout_seconds
	results: list[CheckResult] = []
	with temporary_directory(actual_root) as temporary_path:
		environment = _safe_environment(temporary_path)
		environment["QUALITY_GATE_POLICY_ROOT"] = str(prepared.policy_root)
		run_errors: list[QualityGateError] = []
		executed: list[str] = []
		run_outputs: dict[str, str] = {}
		for component_index, component in enumerate(components, start=1):
			if not component.path_exists:
				results.append(
					CheckResult(
						check_id=f"python.component_{component_index}.path",
						status=Status.UNCHECKED,
						summary="Python component path is unavailable",
						findings=(
							Finding(
								path=str(component.path.relative_to(actual_root)),
								message="declared component path does not exist",
								action="restore the declared component path",
							),
						),
						recovery_action=(
							"restore the declared component path and retry the quality gate"
						),
					)
				)
				continue
			for missing_path_index, missing_path in enumerate(
				component.missing_test_paths, start=1
			):
				results.append(
					CheckResult(
						check_id=f"python.component_{component_index}.test_path_{missing_path_index}",
						status=Status.UNCHECKED,
						summary="declared test path is unavailable",
						findings=(
							Finding(
								path=str(missing_path.relative_to(actual_root)),
								message="declared test path does not exist",
								action="restore the declared test path",
							),
						),
						recovery_action="restore the declared test path and retry the quality gate",
					)
				)
			try:
				python_executable = _runtime_python(prepared, component_index)
			except QualityGateError as error:
				results.append(_error_result(error))
				continue
			component_executed, component_errors, component_outputs = _execute_commands(
				actual_root,
				environment,
				_component_commands(
					actual_root, manifest, component, prepared, component_index, python_executable
				),
				component.timeout_seconds,
				deadline,
			)
			executed.extend(component_executed)
			run_errors.extend(component_errors)
			run_outputs.update(component_outputs)
	results.extend(_command_results(executed, run_errors, run_outputs))
	for component_index, component in enumerate(components, start=1):
		tests_not_applicable = component.tests is None
		has_coverage = _has_pinned_coverage(prepared)
		if tests_not_applicable:
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.pytest",
					status=Status.NOT_APPLICABLE,
					summary="tests are explicitly not applicable",
					recovery_action="declare test paths if tests apply",
				)
			)
		if tests_not_applicable or not has_coverage:
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.coverage",
					status=Status.NOT_APPLICABLE,
					summary=(
						"coverage is not applicable without tests"
						if tests_not_applicable and has_coverage
						else "coverage provider is not in the pinned policy inventory"
					),
					recovery_action=(
						"declare test paths before collecting report-only coverage"
						if tests_not_applicable and has_coverage
						else (
							"add a pinned coverage provider only when report-only coverage is "
							"required"
						)
					),
				)
			)
	return results


def format_paths(root: Path | None, paths: tuple[str, ...]) -> None:
	"""Format only the explicit Python paths with the pinned Ruff release."""
	actual_root = repository_root(root)
	try:
		manifest = load_manifest(actual_root)
		components = load_components(actual_root)
	except ValidationError as error:
		raise _manifest_error(error) from error
	if not components:
		raise QualityGateError(
			"no Python component is declared",
			check_id="python.components",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare a Python component before formatting Python files",
		)
	prepared = prepare(actual_root)
	deadline = time.monotonic() + manifest.repository.gate_timeout_seconds
	selected: dict[int, list[str]] = {}
	for raw_path in paths:
		candidate = relative_path(actual_root, raw_path, "format path")
		if not candidate.exists():
			raise QualityGateError(
				f"format path does not exist: {raw_path}",
				check_id="python.format",
				exit_code=EXIT_UNCHECKED,
				recovery_action=f"restore {raw_path} and retry format",
			)
		for index, component in enumerate(components):
			if candidate == component.path or component.path in candidate.parents:
				selected.setdefault(index, []).append(str(candidate.relative_to(actual_root)))
				break
		else:
			raise QualityGateError(
				f"format path is outside every Python component: {raw_path}",
				check_id="python.format",
				exit_code=EXIT_UNCHECKED,
				recovery_action="format only paths declared by a Python component",
			)
	with temporary_directory(actual_root) as temporary_path:
		environment = _safe_environment(temporary_path)
		format_errors: list[QualityGateError] = []
		for index, explicit_paths in selected.items():
			python_executable = _runtime_python(prepared, index + 1)
			try:
				run(
					[
						*_tool_command(prepared, python_executable, "ruff"),
						"format",
						"--config",
						str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
						*explicit_paths,
					],
					actual_root,
					environment,
					timeout=_remaining(deadline, manifest.repository.command_timeout_seconds),
				)
			except QualityGateError as error:
				format_errors.append(error)
		if format_errors:
			raise QualityGateError(
				"\n".join(str(error) for error in format_errors),
				check_id="python.format",
				exit_code=(
					EXIT_UNCHECKED
					if any(error.exit_code == EXIT_UNCHECKED for error in format_errors)
					else 1
				),
				recovery_action="restore formatting verification and retry format",
			)

	emit("format: complete - stage the changes, then run check")


def _check_snapshot(
	actual_root: Path,
	*,
	verbose: bool = False,
	repository_root: Path | None = None,
	index_file: Path | None = None,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit"] = "check",
) -> Verdict:
	actual_root = actual_root.resolve()
	try:
		manifest = load_manifest(actual_root)
	except ValidationError as error:
		verdict = Verdict((_error_result(_manifest_error(error)),))
		return verdict
	contract_result = required_documents_result(actual_root, manifest)
	repository_results = [
		*git_integrity_results(
			actual_root,
			manifest,
			repository=repository_root or actual_root,
			index_file=index_file,
		),
		workflow_result(actual_root, manifest),
		*documentation_results(actual_root, manifest),
	]
	results = [contract_result, *repository_results]
	results.append(lessons_result(actual_root, is_complete_required=mode == "audit"))
	try:
		components = load_components(actual_root)
		prepared = prepare(
			actual_root,
			repository_root=repository_root or actual_root,
		)
	except QualityGateError as error:
		return Verdict((*results, _error_result(error)))
	except (DistributionError, RuntimeUnavailable, OSError) as error:
		quality_error = QualityGateError(
			str(error),
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run sync and setup, then retry the quality gate",
		)
		return Verdict((*results, _error_result(quality_error)))
	history_base, history_head = _ci_history_refs(base, head)
	results.append(secret_candidate_result(actual_root, manifest, prepared))
	if mode == "audit":
		results.append(secret_audit_result(repository_root or actual_root, manifest, prepared))
	else:
		results.append(
			secret_history_result(
				repository_root or actual_root,
				manifest,
				prepared,
				base=history_base,
				head=history_head,
			)
		)
	if not components:
		results.append(
			CheckResult(
				check_id="python.components",
				status=Status.NOT_APPLICABLE,
				summary="no Python component is declared",
				recovery_action="declare a Python component if Python checks apply",
			)
		)
	else:
		try:
			policy_path = prepared.policy_root / "quality_gate" / "policy"
			if not policy_path.is_dir():
				raise QualityGateError(
					"cached policy release has no policy directory",
					check_id="runtime.policy",
					exit_code=EXIT_UNCHECKED,
					recovery_action="sync a complete policy release and retry the quality gate",
				)
			run_results = _run_python_checks(actual_root, manifest, components, prepared)
		except QualityGateError as error:
			return Verdict((*results, _error_result(error)))
		except (DistributionError, RuntimeUnavailable, OSError) as error:
			quality_error = QualityGateError(
				str(error),
				check_id="runtime.policy",
				exit_code=EXIT_UNCHECKED,
				recovery_action="run sync and setup, then retry the quality gate",
			)
			return Verdict((*results, _error_result(quality_error)))
		results.extend(run_results)
	verdict = Verdict(tuple(results))
	return verdict


def check(
	root: Path | None = None,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
) -> Verdict:
	"""Run the complete quality contract against the exact staged candidate."""

	actual_root = repository_root(root)
	verdict = _run_snapshot(
		actual_root,
		verbose=verbose,
		base=base,
		head=head,
		mode="check",
	)
	emit(render(verdict, verbose=verbose))
	return verdict


def _run_snapshot(
	actual_root: Path,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit"] = "check",
) -> Verdict:
	"""Run one candidate snapshot and convert snapshot failures to a verdict."""
	try:
		with candidate_snapshot(actual_root) as snapshot:
			verdict = _check_snapshot(
				snapshot.root,
				verbose=verbose,
				repository_root=actual_root,
				index_file=getattr(snapshot, "repository_index", None),
				base=base,
				head=head,
				mode=mode,
			)
	except SnapshotError as error:
		quality_error = QualityGateError(
			error.message,
			check_id="candidate.snapshot",
			exit_code=EXIT_UNCHECKED,
			recovery_action=f"restore a stable supported Git index and run {mode} again",
		)
		verdict = Verdict((_error_result(quality_error),))
	return verdict


def audit(root: Path | None = None, *, verbose: bool = False) -> Verdict:
	"""Run every implemented domain and an explicit full-history secret audit."""
	actual_root = repository_root(root)
	verdict = _run_snapshot(
		actual_root,
		verbose=verbose,
		mode="audit",
	)
	emit(render(verdict, verbose=verbose))
	return verdict
