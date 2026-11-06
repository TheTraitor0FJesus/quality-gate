"""Deterministic quality checks used by the local hook and CI."""

from __future__ import annotations

import time
from contextlib import nullcontext
from pathlib import Path
from typing import Literal

from .admission import (
	AdmissionError,
	gate_admission,
	running_in_ci,
)
from .contracts import (
	CheckResult,
	Finding,
	Manifest,
	Status,
	ValidationError,
	Verdict,
	load_manifest,
	redact,
)
from .distribution import DistributionError
from .integrity import (
	documentation_results,
	git_integrity_results,
	required_documents_result,
	workflow_result,
)
from .launcher import PreparedEnvironment, prepare, prepare_bootstrap
from .lessons import lessons_result
from .reporting import render
from .runner_context import (
	RUFF_THREADS_ENV as RUFF_THREADS_ENV,
)
from .runner_context import (
	PythonComponent as PythonComponent,
)
from .runner_context import (
	_ci_history_refs,
	_manifest_error,
	_remaining,
	_ruff_environment,
	_runtime_python,
	_safe_environment,
)
from .runner_context import (
	load_components as load_components,
)
from .runner_context import (
	relative_path as relative_path,
)
from .runner_context import (
	repository_root as repository_root,
)
from .runner_dependency import (
	_dependency_hygiene_result,
	_tool_command,
)
from .runner_errors import QualityGateError as QualityGateError
from .runner_helpers import _error_result, _progress
from .runner_helpers import emit as emit
from .runner_helpers import temporary_directory as temporary_directory
from .runner_helpers import validate as validate
from .runner_python import _command_results, _component_commands, _has_pinned_coverage
from .runner_subprocess import (
	_run_bounded_subprocess,
)
from .runner_subprocess import (
	run as run,
)
from .runner_web import (
	_biome_asset_paths,
	_biome_command,
	_biome_component_commands,
	_biome_unchecked_results,
)
from .runner_web import (
	web_budget_results as web_budget_results,
)
from .runtime import RuntimeUnavailable
from .secrets import secret_audit_result, secret_candidate_result, secret_history_result
from .snapshot import SnapshotError, candidate_snapshot
from .temp_workspace import TemporaryWorkspaceError, temporary_workspace

POLICY_DIR = Path(__file__).resolve().parent / "policy"
EXIT_UNCHECKED = 2


def _run_web_checks(
	actual_root: Path,
	manifest: Manifest,
	prepared: PreparedEnvironment,
	*,
	deadline: float,
) -> list[CheckResult]:
	if not manifest.web:
		return []
	commands: list[tuple[str, list[str], int]] = []
	pre_results: list[CheckResult] = []
	for component_index, component in enumerate(manifest.web, start=1):
		try:
			paths = _biome_asset_paths(actual_root, component, component_index)
			command = _biome_command(prepared)
		except (OSError, QualityGateError) as error:
			quality_error = (
				error
				if isinstance(error, QualityGateError)
				else QualityGateError(
					str(error),
					check_id="runtime.policy",
					exit_code=EXIT_UNCHECKED,
					recovery_action=(
						"restore the pinned Biome policy and web assets, "
						"then retry the quality gate"
					),
				)
			)
			pre_results.extend(
				_biome_unchecked_results(
					component_index,
					str(quality_error),
					recovery_action=quality_error.recovery_action,
				)
			)
			continue
		commands.extend(
			_biome_component_commands(
				component_index, command, paths, manifest.repository.command_timeout_seconds
			)
		)
	with temporary_directory(actual_root) as temporary_path:
		executed, errors, _ = _execute_commands(
			actual_root,
			_safe_environment(temporary_path),
			commands,
			manifest.repository.command_timeout_seconds,
			deadline,
		)
	return [*pre_results, *_command_results(executed, errors, {})]


def _web_check_results(
	actual_root: Path,
	manifest: Manifest,
	prepared: PreparedEnvironment,
	results: list[CheckResult],
	*,
	deadline: float,
) -> list[CheckResult]:
	try:
		return _run_web_checks(actual_root, manifest, prepared, deadline=deadline)
	except (QualityGateError, DistributionError, RuntimeUnavailable, OSError) as error:
		existing_biome_ids = {result.check_id for result in results if ".biome_" in result.check_id}
		fallback: list[CheckResult] = []
		for component_index in range(1, len(manifest.web) + 1):
			for result in _biome_unchecked_results(
				component_index,
				redact(str(error)),
				recovery_action="restore the pinned Biome policy and retry the quality gate",
			):
				if result.check_id not in existing_biome_ids:
					fallback.append(result)
					existing_biome_ids.add(result.check_id)
		return fallback


def _run_python_snapshot_checks(
	actual_root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
	*,
	deadline: float,
) -> list[CheckResult]:
	policy_path = prepared.policy_root / "quality_gate" / "policy"
	if not policy_path.is_dir():
		raise QualityGateError(
			"cached policy release has no policy directory",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="sync a complete policy release and retry the quality gate",
		)
	return _run_python_checks(actual_root, manifest, components, prepared, deadline=deadline)


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
			command_environment = (
				_ruff_environment(environment)
				if check_id.endswith((".ruff", ".format"))
				else environment
			)
			output = run(
				command,
				actual_root,
				command_environment,
				timeout=_remaining(deadline, max(timeout, component_timeout)),
			)
			if output and check_id.endswith(".coverage_report"):
				run_outputs[check_id] = output
		except QualityGateError as error:
			error.check_id = check_id
			run_errors.append(error)
	return executed, run_errors, run_outputs


def _run_python_checks(
	actual_root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
	*,
	deadline: float,
) -> list[CheckResult]:
	"""Run component checks and return results, including unavailable prerequisites.

	Use deadline as an absolute time.monotonic() limit for command execution.
	Convert unavailable runtimes and command failures into results and include
	explicitly inapplicable typing, test, and coverage checks."""
	results: list[CheckResult] = []
	with temporary_directory(actual_root) as temporary_path:
		environment = _safe_environment(temporary_path)
		temporary_root = Path(temporary_path)
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
				results.append(
					_dependency_hygiene_result(
						actual_root,
						manifest,
						component,
						prepared,
						component_index,
						None,
						temporary_root / f"deptry-{component_index}.json",
						deadline,
						_run_bounded_subprocess,
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
				results.append(
					_dependency_hygiene_result(
						actual_root,
						manifest,
						component,
						prepared,
						component_index,
						None,
						temporary_root / f"deptry-{component_index}.json",
						deadline,
						_run_bounded_subprocess,
					)
				)
				continue
			results.append(
				_dependency_hygiene_result(
					actual_root,
					manifest,
					component,
					prepared,
					component_index,
					python_executable,
					temporary_root / f"deptry-{component_index}.json",
					deadline,
					_run_bounded_subprocess,
				)
			)
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
		if not component.typecheck:
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.mypy",
					status=Status.NOT_APPLICABLE,
					summary="typing is explicitly disabled by the component contract",
					recovery_action="enable typecheck when component typing is required",
				)
			)
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
	policy_release: str | None = None,
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
		workflow_result(
			actual_root,
			manifest,
			repository_root=repository_root or actual_root,
		),
		*documentation_results(actual_root, manifest),
	]
	results = [contract_result, *repository_results]
	results.append(lessons_result(actual_root, is_complete_required=mode == "audit"))
	results.extend(web_budget_results(actual_root, manifest))
	try:
		components = load_components(actual_root)
		prepared = (
			prepare_bootstrap(
				actual_root,
				policy_release=policy_release,
				repository_root=repository_root or actual_root,
			)
			if policy_release is not None
			else prepare(actual_root, repository_root=repository_root or actual_root)
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
	_progress("repository checks completed; 1 stage remaining.")
	gate_deadline = time.monotonic() + manifest.repository.gate_timeout_seconds
	results.extend(
		_web_check_results(
			actual_root,
			manifest,
			prepared,
			results,
			deadline=gate_deadline,
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
			run_results = _run_python_snapshot_checks(
				actual_root,
				manifest,
				components,
				prepared,
				deadline=gate_deadline,
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
		results.extend(run_results)
	_progress("component checks completed; preparing report.")
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
	policy_release: str | None = None,
) -> Verdict:
	"""Run one candidate snapshot and convert snapshot failures to a verdict."""
	_progress("this check may take a long time; wait for completion without polling.")
	try:
		admission = nullcontext() if running_in_ci() else gate_admission()
		with admission:
			with temporary_workspace(actual_root):
				with candidate_snapshot(actual_root) as snapshot:
					_progress("preparation completed; 2 stages remaining.")
					verdict = _check_snapshot(
						snapshot.root,
						verbose=verbose,
						repository_root=actual_root,
						index_file=getattr(snapshot, "repository_index", None),
						base=base,
						head=head,
						mode=mode,
						policy_release=policy_release,
					)
	except AdmissionError as error:
		quality_error = QualityGateError(
			str(error),
			check_id="runtime.admission",
			exit_code=EXIT_UNCHECKED,
			recovery_action=error.recovery_action,
		)
		verdict = Verdict((_error_result(quality_error),))
	except SnapshotError as error:
		quality_error = QualityGateError(
			error.message,
			check_id="candidate.snapshot",
			exit_code=EXIT_UNCHECKED,
			recovery_action=f"restore a stable supported Git index and run {mode} again",
		)
		verdict = Verdict((_error_result(quality_error),))
	except TemporaryWorkspaceError as error:
		quality_error = QualityGateError(
			str(error),
			check_id="temporary.workspace",
			exit_code=EXIT_UNCHECKED,
			recovery_action=(
				"restore a writable repository temp directory and retry the quality gate"
			),
		)
		verdict = Verdict((_error_result(quality_error),))
	return verdict


def bootstrap_check(
	root: Path,
	*,
	policy_release: str,
	base: str | None = None,
	head: str | None = None,
) -> Verdict:
	"""Run the current checkout against a previous immutable policy release."""
	actual_root = repository_root(root)
	verdict = _run_snapshot(
		actual_root,
		base=base,
		head=head,
		policy_release=policy_release,
	)
	emit(render(verdict))
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


def bootstrap_audit(
	root: Path,
	*,
	policy_release: str,
	verbose: bool = False,
) -> Verdict:
	"""Audit one candidate using an explicitly selected immutable policy release."""
	actual_root = repository_root(root)
	verdict = _run_snapshot(
		actual_root,
		verbose=verbose,
		mode="audit",
		policy_release=policy_release,
	)
	emit(render(verdict, verbose=verbose))
	return verdict
