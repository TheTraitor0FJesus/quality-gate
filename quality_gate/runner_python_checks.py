"""Quality Gate runner: python checks operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import runner as _runner
from .contracts import MAX_TELEMETRY_TEXT

if TYPE_CHECKING:
	from .runner_ruff_evidence import LookupStats

_RUFF_COMMAND_PAIR_SIZE = 2


def _append_missing_component_check(
	results: _runner._OperationResults, context: _runner._PythonCheckContext
) -> None:
	check_id = f"python.component_{context.component_index}.path"
	component_scope = context.component.path.relative_to(context.root).as_posix()
	missing_component = _runner.CheckResult(
		check_id=check_id,
		status=_runner.Status.UNCHECKED,
		summary="Python component path is unavailable",
		findings=(
			_runner.Finding(
				path=component_scope,
				message="declared component path does not exist",
				action="restore the declared component path",
			),
		),
		recovery_action="restore the declared component path and retry the quality gate",
	)
	results.append(
		_runner.observe_check(
			check_id,
			component_scope,
			_runner._fixed_check_result(missing_component),
		)
	)
	results.append(context.dependency_result())
	_runner._mark_component_checks_not_run(
		results,
		context.component,
		context.component_index,
		context.prepared,
		"declared component path is unavailable",
	)


def _append_missing_test_path_checks(
	results: _runner._OperationResults,
	root: _runner.Path,
	component: _runner.PythonComponent,
	component_index: int,
) -> None:
	for missing_index, missing_path in enumerate(component.missing_test_paths, start=1):
		check_id = f"python.component_{component_index}.test_path_{missing_index}"
		missing_test_path = _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.UNCHECKED,
			summary="declared test path is unavailable",
			findings=(
				_runner.Finding(
					path=missing_path.relative_to(root).as_posix(),
					message="declared test path does not exist",
					action="restore the declared test path",
				),
			),
			recovery_action="restore the declared test path and retry the quality gate",
		)
		results.append(
			_runner.observe_check(
				check_id,
				missing_path.relative_to(root).as_posix(),
				_runner._fixed_check_result(missing_test_path),
			)
		)


def _append_unavailable_runtime_checks(
	results: _runner._OperationResults,
	context: _runner._PythonCheckContext,
	error: _runner.QualityGateError,
) -> None:
	results.append(_runner._error_result(error))
	results.append(context.dependency_result())
	_runner._mark_component_checks_not_run(
		results,
		context.component,
		context.component_index,
		context.prepared,
		"component runtime is unavailable",
	)


def _record_evidence_lookup(
	operation: _runner.GateOperation,
	check_id: str,
	lookup: LookupStats,
	*,
	lookup_ms: int | None = None,
	validation_ms: int,
	reused_only: bool,
) -> None:
	"""Attach shared lookup cost without claiming that a cached format process ran."""
	timing = operation.check_timings.get(check_id)
	if timing is None:
		return
	reason = "file-local results reused; no format process" if reused_only else timing.reason
	producer = lookup.slowest_producer
	if producer is None:
		raise ValueError("reused Ruff results require original producer metadata")
	operation.check_timings[check_id] = _runner.replace(
		timing,
		lookup_ms=lookup_ms,
		validation_ms=validation_ms,
		disposition="reused" if reused_only else timing.disposition,
		reason=f"{reason}; original metadata identifies the slowest producer operation".lstrip(
			"; "
		)[:MAX_TELEMETRY_TEXT],
		original_elapsed_ms=producer.duration_ms,
		original_operation_id=f"{producer.operation}@{producer.started_at}",
	)


def _full_ruff_result(
	context: _runner._PythonCheckContext,
	environment: dict[str, str],
	specification: tuple[str, list[str], int],
) -> _runner.CheckResult:
	check_id, command, timeout = specification
	errors: list[_runner.QualityGateError] = []
	try:
		_runner.run(
			[*command, "--no-cache"],
			context.root,
			_runner._ruff_environment(environment),
			timeout=_runner._remaining(
				context.deadline, min(timeout, context.component.timeout_seconds)
			),
		)
	except _runner.QualityGateError as error:
		errors.append(_runner._command_error(check_id, error))
	return _runner._command_results([check_id], errors, {})[0]


def _run_ruff_with_evidence(
	results: _runner._OperationResults,
	context: _runner._PythonCheckContext,
	python: _runner.Path,
	environment: dict[str, str],
	commands: list[tuple[str, list[str], int]],
) -> list[tuple[str, list[str], int]]:
	"""Use per-file results only when the provider proves current eligibility."""
	operation = _runner.current_operation()
	if operation is None or len(commands) < _RUFF_COMMAND_PAIR_SIZE:
		return commands
	check_id = f"python.component_{context.component_index}.ruff"
	format_id = f"python.component_{context.component_index}.format"
	if commands[0][0] != check_id or commands[1][0] != format_id:
		return commands
	from .runner_ruff import prepare_ruff_evidence

	scope = results.scope_for(check_id)
	try:
		with operation.stage(f"{check_id}.evidence-readiness", scope):
			evidence = prepare_ruff_evidence(
				context.root,
				operation.root,
				context.component,
				context.prepared,
				python,
				context.component_index,
				environment,
				context.deadline,
				min(
					context.component.timeout_seconds,
					context.manifest.repository.command_timeout_seconds,
				),
			)
	except (OSError, ValueError, _runner.EditError, _runner.SnapshotError):
		return commands
	if evidence is None:
		return commands
	lint = _runner.observe_check(
		check_id,
		scope,
		lambda: evidence.lint_checked(lambda: _full_ruff_result(context, environment, commands[0])),
		process_required=True,
	)
	if evidence.cached:
		_record_evidence_lookup(
			operation,
			check_id,
			evidence.lookup,
			lookup_ms=evidence.lookup.lookup_ms,
			validation_ms=evidence.lint_validation_ms,
			reused_only=False,
		)
		lint = operation.record_result(lint)
	results.append(lint)
	if not evidence.lint_complete:
		return commands[1:]
	format_result = _runner.observe_check(
		format_id,
		results.scope_for(format_id),
		lambda: evidence.format_checked(
			lambda: _full_ruff_result(context, environment, commands[1])
		),
		process_required=True,
	)
	if evidence.cached:
		_record_evidence_lookup(
			operation,
			format_id,
			evidence.lookup,
			validation_ms=evidence.format_validation_ms,
			reused_only=not evidence.targets
			and format_result.status in {_runner.Status.PASSED, _runner.Status.FAILED},
		)
		format_result = operation.record_result(format_result)
	results.append(format_result)
	return commands[_RUFF_COMMAND_PAIR_SIZE:]


def _run_ready_python_component_checks(
	results: _runner._OperationResults,
	context: _runner._PythonCheckContext,
	python_executable: _runner.Path,
	environment: dict[str, str],
) -> tuple[list[str], list[_runner.QualityGateError], dict[str, str]]:
	results.append(context.dependency_result(python_executable))
	component = context.component
	commands = _runner._component_commands(
		context.root,
		context.manifest,
		component,
		context.prepared,
		context.component_index,
		python_executable,
	)
	commands = _run_ruff_with_evidence(results, context, python_executable, environment, commands)
	return _runner._execute_commands(
		context.root,
		environment,
		commands,
		component.timeout_seconds,
		context.deadline,
		prepared=context.prepared,
		component=component,
		component_index=context.component_index,
		manifest=context.manifest,
	)


def _run_python_component_checks(
	results: _runner._OperationResults,
	context: _runner._PythonCheckContext,
	environment: dict[str, str],
) -> tuple[list[str], list[_runner.QualityGateError], dict[str, str]]:
	if not context.component.path_exists:
		_runner._append_missing_component_check(results, context)
		return [], [], {}
	_runner._append_missing_test_path_checks(
		results, context.root, context.component, context.component_index
	)
	try:
		python_executable = _runner._runtime_python(context.prepared, context.component_index)
	except _runner.QualityGateError as error:
		_runner._append_unavailable_runtime_checks(results, context, error)
		return [], [], {}
	return _runner._run_ready_python_component_checks(
		results, context, python_executable, environment
	)


def _append_not_applicable_python_checks(
	results: _runner._OperationResults,
	components: list[_runner.PythonComponent],
	prepared: _runner.PreparedEnvironment,
) -> None:
	for component_index, component in enumerate(components, start=1):
		if not component.typecheck:
			results.append(
				_runner.CheckResult(
					check_id=f"python.component_{component_index}.mypy",
					status=_runner.Status.NOT_APPLICABLE,
					summary="typing is explicitly disabled by the component contract",
					recovery_action="enable typecheck when component typing is required",
				)
			)


def _run_python_checks(
	actual_root: _runner.Path,
	manifest: _runner.Manifest,
	components: list[_runner.PythonComponent],
	prepared: _runner.PreparedEnvironment,
	*,
	deadline: float,
) -> list[_runner.CheckResult]:
	results = _runner._OperationResults(actual_root)
	results.manifest = manifest
	with _runner.temporary_directory(actual_root) as temporary_path:
		environment = _runner._safe_environment(temporary_path)
		environment["QUALITY_GATE_POLICY_ROOT"] = str(prepared.policy_root)
		temporary_root = _runner.Path(temporary_path)
		for component_index, component in enumerate(components, start=1):
			context = _runner._PythonCheckContext(
				actual_root,
				manifest,
				component,
				prepared,
				component_index,
				temporary_root / f"deptry-{component_index}.json",
				deadline,
			)
			component_executed, component_errors, component_outputs = (
				_runner._run_python_component_checks(
					results,
					context,
					environment,
				)
			)
			results.extend(
				_runner._command_results(component_executed, component_errors, component_outputs)
			)
	_runner._append_not_applicable_python_checks(results, components, prepared)
	return results


def format_paths(root: _runner.Path | None, paths: tuple[str, ...]) -> None:
	"""Format only the explicit Python paths with the pinned Ruff release."""
	actual_root = _runner.repository_root(root)
	try:
		manifest = _runner.load_manifest(actual_root)
		components = _runner.load_components(actual_root)
	except _runner.ValidationError as error:
		raise _runner._manifest_error(error) from error
	if not components:
		raise _runner.QualityGateError(
			"no Python component is declared",
			check_id="python.components",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="declare a Python component before formatting Python files",
		)
	prepared = _runner.prepare(actual_root)
	deadline = _runner.time.monotonic() + manifest.repository.gate_timeout_seconds
	selected: dict[int, list[str]] = {}
	for raw_path in paths:
		candidate = _runner.relative_path(actual_root, raw_path, "format path")
		if not candidate.exists():
			raise _runner.QualityGateError(
				f"format path does not exist: {raw_path}",
				check_id="python.format",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action=f"restore {raw_path} and retry format",
			)
		for index, component in enumerate(components):
			if candidate == component.path or component.path in candidate.parents:
				selected.setdefault(index, []).append(str(candidate.relative_to(actual_root)))
				break
		else:
			raise _runner.QualityGateError(
				f"format path is outside every Python component: {raw_path}",
				check_id="python.format",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action="format only paths declared by a Python component",
			)
	with _runner.temporary_directory(actual_root) as temporary_path:
		environment = _runner._safe_environment(temporary_path)
		format_errors: list[_runner.QualityGateError] = []
		for index, explicit_paths in selected.items():
			python_executable = _runner._runtime_python(prepared, index + 1)
			try:
				_runner.run(
					[
						*_runner._tool_command(prepared, python_executable, "ruff"),
						"format",
						"--config",
						str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
						*explicit_paths,
					],
					actual_root,
					environment,
					timeout=_runner._remaining(
						deadline, manifest.repository.command_timeout_seconds
					),
				)
			except _runner.QualityGateError as error:
				format_errors.append(error)
		if format_errors:
			raise _runner.QualityGateError(
				"\n".join(str(error) for error in format_errors),
				check_id="python.format",
				exit_code=(
					_runner.EXIT_UNCHECKED
					if any(error.exit_code == _runner.EXIT_UNCHECKED for error in format_errors)
					else 1
				),
				recovery_action="restore formatting verification and retry format",
			)

	_runner.emit("format: complete - stage the changes, then run check")
