"""Deterministic quality checks used by the local hook and CI."""

from __future__ import annotations

import locale
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

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
from quality_gate.reporting import render
from quality_gate.snapshot import SnapshotError, candidate_snapshot

MANIFEST_NAME = "quality-gate.toml"
POLICY_DIR = Path(__file__).resolve().parent / "policy"
EXIT_UNCHECKED = 2


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
		if not path.is_dir():
			raise QualityGateError(
				f"Python component path does not exist: {item.path}",
				check_id="python.contract",
				exit_code=2,
				recovery_action=f"restore {item.path} and run validate",
			)
		test_paths = tuple(
			relative_path(root, test_path, f"python entry {index}.test_paths[{test_index}]")
			for test_index, test_path in enumerate(item.test_paths)
		)
		missing_tests = [
			str(test_path.relative_to(root)) for test_path in test_paths if not test_path.is_dir()
		]
		if missing_tests:
			raise QualityGateError(
				f"declared test path does not exist: {missing_tests[0]}",
				check_id="python.tests",
				exit_code=2,
				recovery_action=f"restore {missing_tests[0]} and run validate",
			)
		components.append(
			PythonComponent(
				path,
				test_paths[0] if test_paths else None,
				None,
				True,
				test_paths,
				item.timeout_seconds,
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


def run(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	*,
	timeout: float | None = None,
) -> None:
	try:
		result = subprocess.run(
			command,
			cwd=root,
			env=environment,
			capture_output=True,
			check=False,
			timeout=timeout,
		)
	except subprocess.TimeoutExpired as error:
		raise QualityGateError(
			f"{' '.join(command)} timed out after {timeout:g} seconds",
			check_id="runtime.timeout",
			exit_code=2,
			recovery_action="inspect the command and retry within the declared time budget",
		) from error
	if result.returncode:
		detail = (
			decode_subprocess_output(result.stdout) + decode_subprocess_output(result.stderr)
		).strip()
		if not detail:
			detail = f"Command exited with {result.returncode}."
		raise QualityGateError(
			f"{' '.join(command)}\n\n{redact(detail)}",
			check_id="runtime.command",
			exit_code=1,
			recovery_action="fix the reported quality finding and retry the quality gate",
		)


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


def _safe_environment(temporary_path: str) -> dict[str, str]:
	allowed = {
		"LANG",
		"LC_ALL",
		"PATHEXT",
		"PATH",
		"PYTHONHOME",
		"PYTHONPATH",
		"SYSTEMROOT",
		"VIRTUAL_ENV",
		"WINDIR",
	}
	environment = {key: value for key, value in os.environ.items() if key in allowed}
	environment.update(
		{
			"TMP": temporary_path,
			"TEMP": temporary_path,
			"PYTHONIOENCODING": "utf-8",
			"PYTHONUTF8": "1",
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUFF_CACHE_DIR": temporary_path,
			"MYPY_CACHE_DIR": temporary_path,
		}
	)
	return environment


def _run_python_checks(
	actual_root: Path, manifest: Manifest, components: list[PythonComponent]
) -> list[CheckResult]:
	deadline = time.monotonic() + manifest.repository.gate_timeout_seconds
	with temporary_directory(actual_root) as temporary_path:
		environment = _safe_environment(temporary_path)
		run_errors: list[QualityGateError] = []
		executed: list[str] = []
		for component_index, component in enumerate(components, start=1):
			arguments = [str(component.path.relative_to(actual_root))]
			prefix = f"python.component_{component_index}"
			commands: list[tuple[str, list[str], int]] = [
				(
					f"{prefix}.ruff",
					[
						sys.executable,
						"-m",
						"ruff",
						"check",
						"--config",
						str(POLICY_DIR / "ruff.toml"),
						*arguments,
					],
					manifest.repository.command_timeout_seconds,
				),
				(
					f"{prefix}.format",
					[
						sys.executable,
						"-m",
						"ruff",
						"format",
						"--check",
						"--config",
						str(POLICY_DIR / "ruff.toml"),
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
							sys.executable,
							"-m",
							"mypy",
							"--config-file",
							str(POLICY_DIR / "mypy.ini"),
							*arguments,
						],
						manifest.repository.command_timeout_seconds,
					)
				)
			for test_path in component.test_paths:
				commands.append(
					(
						f"{prefix}.pytest",
						[
							sys.executable,
							"-m",
							"pytest",
							str(test_path.relative_to(actual_root)),
							"-q",
							"-p",
							"no:cacheprovider",
						],
						manifest.repository.test_timeout_seconds,
					)
				)
			for check_id, command, timeout in commands:
				executed.append(check_id)
				try:
					run(
						command,
						actual_root,
						environment,
						timeout=_remaining(deadline, max(timeout, component.timeout_seconds)),
					)
				except QualityGateError as error:
					error.check_id = check_id
					run_errors.append(error)
	results: list[CheckResult] = []
	for check_id in executed:
		errors = [error for error in run_errors if error.check_id == check_id]
		status = Status.PASSED
		if errors:
			status = (
				Status.UNCHECKED
				if any(error.exit_code == EXIT_UNCHECKED for error in errors)
				else Status.FAILED
			)
		results.append(
			CheckResult(
				check_id=check_id,
				status=status,
				summary="check passed" if status is Status.PASSED else "check requires attention",
				findings=tuple(Finding(message=redact(str(error))) for error in errors),
				recovery_action=(
					"restore verification and fix the reported finding, then retry"
					if errors
					else None
				),
			)
		)
	for component_index, component in enumerate(components, start=1):
		if not component.test_paths:
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.pytest",
					status=Status.NOT_APPLICABLE,
					summary="tests are explicitly not applicable",
					recovery_action="declare test paths if tests apply",
				)
			)
	return results


def _check_snapshot(actual_root: Path, *, verbose: bool = False) -> Verdict:
	actual_root = actual_root.resolve()
	try:
		manifest = load_manifest(actual_root)
	except ValidationError as error:
		verdict = Verdict((_error_result(_manifest_error(error)),))
		return verdict
	contract_result = required_documents_result(actual_root, manifest)
	try:
		components = load_components(actual_root)
	except QualityGateError as error:
		verdict = Verdict((contract_result, _error_result(error)))
		return verdict
	try:
		run_results = _run_python_checks(actual_root, manifest, components)
	except QualityGateError as error:
		verdict = Verdict((contract_result, _error_result(error)))
		return verdict
	results = [contract_result]
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
		results.extend(run_results)
	verdict = Verdict(tuple(results))
	return verdict


def check(root: Path | None = None, *, verbose: bool = False) -> Verdict:
	"""Run the complete quality contract against the exact staged candidate."""

	actual_root = repository_root(root)
	try:
		with candidate_snapshot(actual_root) as snapshot:
			verdict = _check_snapshot(snapshot.root, verbose=verbose)
	except SnapshotError as error:
		quality_error = QualityGateError(
			error.message,
			check_id="candidate.snapshot",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore a stable supported Git index and run check again",
		)
		verdict = Verdict((_error_result(quality_error),))
	emit(render(verdict, verbose=verbose))
	return verdict
