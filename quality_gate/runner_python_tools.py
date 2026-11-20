"""Quality Gate runner: python tools operations."""

from __future__ import annotations

from typing import Literal

from . import runner as _runner


def _component_commands(
	actual_root: _runner.Path,
	manifest: _runner.Manifest,
	component: _runner.PythonComponent,
	prepared: _runner.PreparedEnvironment,
	component_index: int,
	python_executable: _runner.Path,
) -> list[tuple[str, list[str], int]]:
	prefix = f"python.component_{component_index}"
	arguments = [str(component.path.relative_to(actual_root))]
	commands: list[tuple[str, list[str], int]] = [
		(
			f"{prefix}.ruff",
			[
				*_runner._tool_command(prepared, python_executable, "ruff"),
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
				*_runner._tool_command(prepared, python_executable, "ruff"),
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
					*_runner._tool_command(prepared, python_executable, "mypy"),
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
					*_runner._tool_command(prepared, python_executable, "pytest"),
					*test_arguments,
				],
				manifest.repository.test_timeout_seconds,
			)
		)
		if _runner._has_pinned_coverage(prepared):
			commands.extend(
				(
					(
						f"{prefix}.coverage",
						[
							*_runner._tool_command(prepared, python_executable, "coverage"),
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
							*_runner._tool_command(prepared, python_executable, "coverage"),
							"report",
							"--show-missing",
						],
						manifest.repository.command_timeout_seconds,
					),
				)
			)
	return commands


def _native_tool_configuration(
	check_id: str,
) -> tuple[Literal["ruff", "mypy"], str, str] | None:
	"""Map one native check to its tool, policy file, and cache environment key."""
	if check_id.endswith((".ruff", ".format")):
		return "ruff", "ruff.toml", "RUFF_CACHE_DIR"
	if check_id.endswith(".mypy"):
		return "mypy", "mypy.ini", "MYPY_CACHE_DIR"
	return None


def _native_cache_identity(
	actual_root: _runner.Path,
	check_id: str,
	command: list[str],
	prepared: _runner.PreparedEnvironment,
	component: _runner.PythonComponent,
	component_index: int,
) -> _runner.NativeToolCacheIdentity | None:
	"""Build a snapshot-independent key for one exact pinned native tool command."""
	tool_configuration = _runner._native_tool_configuration(check_id)
	if tool_configuration is None:
		return None
	tool_name, configuration_name, _environment_key = tool_configuration

	release_manifest = getattr(prepared, "release_manifest", None)
	tools = getattr(release_manifest, "tools", ())
	tool = next(
		(item for item in tools if getattr(item, "name", "").casefold() == tool_name),
		None,
	)
	runtime_identities = getattr(prepared, "runtime_identities", ())
	if tool is None or len(runtime_identities) < component_index:
		return None
	runtime_identity_value = runtime_identities[component_index - 1]
	if not isinstance(runtime_identity_value, _runner.RuntimeIdentity):
		return None
	policy_root = getattr(prepared, "policy_root", None)
	cache_root = getattr(prepared, "cache_root", None)
	if not isinstance(policy_root, _runner.Path) or not isinstance(cache_root, _runner.Path):
		return None
	configuration = policy_root / "quality_gate" / "policy" / configuration_name
	try:
		policy_digest = _runner.hashlib.sha256(configuration.read_bytes()).hexdigest()
		import_root = component.path.relative_to(actual_root).as_posix()
	except OSError:
		return None
	except ValueError:
		return None

	repository_identity = _runner._native_cache_repository_identity(
		runtime_identity_value.repository
	)
	cache_runtime_identity = _runner.replace(
		runtime_identity_value,
		repository=repository_identity,
	)
	normalized_options = _runner._normalized_native_tool_options(
		command, tool_name, configuration_name, policy_digest
	)

	return _runner.NativeToolCacheIdentity(
		repository=repository_identity,
		engine_version=_runner.__version__,
		runtime_fingerprint=_runner.runtime_fingerprint(cache_runtime_identity),
		tool=tool_name,
		tool_version=tool.version,
		tool_digest=tool.sha256,
		policy_release=prepared.release_manifest.version,
		policy_digest=policy_digest,
		platform=f"{_runner.sys.platform}-{_runner.platform.machine().casefold()}",
		python_version=runtime_identity_value.python_version,
		effective_options=tuple(normalized_options),
		import_roots=(import_root,),
	)


def _native_cache_repository_identity(repository: str) -> str:
	"""Use a Git common directory to identify linked worktrees as one source repo."""
	repository_root = _runner.Path(repository)
	git_path = repository_root / ".git"
	try:
		if git_path.is_dir():
			return str(git_path.resolve())
		if not git_path.is_file():
			return str(repository_root.resolve())

		git_pointer = git_path.read_text(encoding="utf-8").strip()
		git_dir_text = git_pointer.partition(":")
		if git_dir_text[0].casefold() != "gitdir":
			return str(repository_root.resolve())
		git_dir = _runner.Path(git_dir_text[2].strip())
		if not git_dir.is_absolute():
			git_dir = repository_root / git_dir
		git_dir = git_dir.resolve()
		common_dir_file = git_dir / "commondir"
		if not common_dir_file.is_file():
			return str(git_dir)
		common_dir = _runner.Path(common_dir_file.read_text(encoding="utf-8").strip())
		if not common_dir.is_absolute():
			common_dir = git_dir / common_dir
		return str(common_dir.resolve())
	except (OSError, RuntimeError, UnicodeError, ValueError):
		return str(repository_root.resolve())


def _normalized_native_tool_options(
	command: list[str],
	tool_name: Literal["ruff", "mypy"],
	configuration_name: str,
	policy_digest: str,
) -> list[str]:
	arguments = command[1:]
	if (
		len(command) >= _runner._PYTHON_MODULE_COMMAND_LENGTH
		and command[1] == "-m"
		and command[2] == tool_name
	):
		arguments = command[_runner._PYTHON_MODULE_COMMAND_LENGTH :]
	normalized_options: list[str] = []
	configuration_next = False
	for argument in arguments:
		if configuration_next:
			normalized_options.append(f"<policy:{configuration_name}:{policy_digest}>")
			configuration_next = False
		elif argument in {"--config", "--config-file"}:
			normalized_options.append(argument)
			configuration_next = True
		else:
			normalized_options.append(argument.replace("\\", "/"))
	return normalized_options


def _command_error(
	check_id: str, error: _runner.NativeToolCacheError | _runner.QualityGateError
) -> _runner.QualityGateError:
	if isinstance(error, _runner.NativeToolCacheError):
		return _runner.QualityGateError(
			f"native tool source workspace is unavailable: {error.reason}",
			check_id=check_id,
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore private tool execution and retry the quality gate",
		)
	error.check_id = check_id
	return error


def _record_command_result(
	check_id: str, run_errors: list[_runner.QualityGateError], run_outputs: dict[str, str]
) -> None:
	operation = _runner.current_operation()
	if operation is not None:
		operation.record_result(
			_runner._command_results(
				[check_id],
				[error for error in run_errors if error.check_id == check_id],
				run_outputs,
			)[0]
		)


def _execute_commands(
	actual_root: _runner.Path,
	environment: dict[str, str],
	commands: list[tuple[str, list[str], int]],
	component_timeout: int,
	deadline: float,
	*,
	prepared: _runner.PreparedEnvironment | None = None,
	component: _runner.PythonComponent | None = None,
	component_index: int | None = None,
	manifest: _runner.Manifest | None = None,
) -> tuple[list[str], list[_runner.QualityGateError], dict[str, str]]:
	executed: list[str] = []
	run_errors: list[_runner.QualityGateError] = []
	run_outputs: dict[str, str] = {}
	operation = _runner.current_operation()
	scope_resolver = _runner._OperationResults(actual_root)
	scope_resolver.manifest = manifest
	for check_id, command, timeout in commands:
		executed.append(check_id)
		stage = (
			_runner.nullcontext()
			if operation is None
			else operation.stage(
				check_id,
				scope_resolver.scope_for(check_id),
				check=True,
				process_required=True,
			)
		)
		try:
			with stage:
				try:
					tool_configuration = _runner._native_tool_configuration(check_id)
					tool_environment_key = (
						tool_configuration[2] if tool_configuration is not None else None
					)
					identity = (
						_runner._native_cache_identity(
							actual_root,
							check_id,
							command,
							prepared,
							component,
							component_index,
						)
						if prepared is not None
						and component is not None
						and component_index is not None
						and tool_environment_key is not None
						else None
					)
					selection_context: (
						_runner.AbstractContextManager[_runner.NativeToolCacheSelection | None]
						| None
					) = None
					if (
						identity is not None
						and prepared is not None
						and tool_environment_key is not None
					):
						private_path = _runner.Path(environment[tool_environment_key])
						selection_context = _runner.NativeToolCache(prepared.cache_root).lease(
							identity, private_path, operation=operation
						)
					if selection_context is None:
						selection_context = _runner.nullcontext(None)
					with selection_context as selection:
						private_path = (
							_runner.Path(environment[tool_environment_key])
							if tool_environment_key is not None
							else actual_root
						)
						source_workspace = (
							_runner.native_tool_source_workspace(
								selection,
								actual_root,
								private_path,
								operation=operation,
							)
							if selection is not None
							else _runner.nullcontext((None, actual_root))
						)
						with source_workspace as (effective_selection, command_root):
							if effective_selection is not None and operation is not None:
								operation.record_native_cache_selection(effective_selection)
							command_base_environment = dict(environment)
							if effective_selection is not None and tool_environment_key is not None:
								command_base_environment[tool_environment_key] = str(
									effective_selection.path
								)
							command_environment = (
								_runner._ruff_environment(command_base_environment)
								if tool_configuration is not None
								and tool_configuration[0] == "ruff"
								else command_base_environment
							)
							output = _runner.run(
								command,
								command_root,
								command_environment,
								timeout=_runner._remaining(
									deadline, min(timeout, component_timeout)
								),
							)
					if output and check_id.endswith((".coverage_report", ".pytest")):
						run_outputs[check_id] = output
				except (_runner.NativeToolCacheError, _runner.QualityGateError) as error:
					run_errors.append(_runner._command_error(check_id, error))
		except _runner.OperationTimeoutError as error:
			if not any(
				item.check_id == check_id and item.exit_code != _runner.EXIT_UNCHECKED
				for item in run_errors
			):
				run_errors.append(
					_runner.QualityGateError(
						str(error),
						check_id=check_id,
						exit_code=_runner.EXIT_UNCHECKED,
						recovery_action=(
							"inspect the current and last completed stages; "
							"restore the budget and retry"
						),
					)
				)
			_runner._record_command_result(check_id, run_errors, run_outputs)
			raise
		_runner._record_command_result(check_id, run_errors, run_outputs)
	return executed, run_errors, run_outputs


def _command_results(
	executed: list[str],
	run_errors: list[_runner.QualityGateError],
	run_outputs: dict[str, str],
) -> list[_runner.CheckResult]:
	results: list[_runner.CheckResult] = []
	for check_id in executed:
		errors = [error for error in run_errors if error.check_id == check_id]
		is_coverage_collection = check_id.endswith(".coverage")
		is_coverage_report = check_id.endswith(".coverage_report")
		is_coverage = is_coverage_collection or is_coverage_report
		status = _runner.Status.PASSED
		if errors and not is_coverage:
			status = (
				_runner.Status.UNCHECKED
				if any(error.exit_code == _runner.EXIT_UNCHECKED for error in errors)
				else _runner.Status.FAILED
			)
		if is_coverage:
			status = _runner.Status.PASSED
		command_output = run_outputs.get(check_id, "")
		summary = "check passed" if status is _runner.Status.PASSED else "check requires attention"
		recovery_action = "restore verification and fix the reported finding, then retry"
		if check_id.endswith(".pytest"):
			pytest_output = "\n".join((command_output, *(str(error) for error in errors)))
			pytest_totals = _runner._pytest_outcome_summary(pytest_output)
			if pytest_totals is not None:
				summary = f"pytest totals: {pytest_totals}"
		if is_coverage_collection:
			summary = "coverage collection completed"
		if is_coverage_report:
			report_lines = [line.strip() for line in command_output.splitlines() if line.strip()]
			report_line = next(
				(line for line in reversed(report_lines) if line.startswith("TOTAL")),
				report_lines[-1] if report_lines else "coverage report generated",
			)
			summary = f"coverage report: {_runner.redact(report_line)}"
			recovery_action = "coverage is report-only and does not affect the quality verdict"
		if errors and is_coverage:
			summary = "coverage report unavailable; report-only: " + _runner.redact(str(errors[0]))
		results.append(
			_runner.CheckResult(
				check_id=check_id,
				status=status,
				summary=summary,
				findings=()
				if is_coverage
				else tuple(_runner.Finding(message=_runner.redact(str(error))) for error in errors),
				recovery_action=None if is_coverage else recovery_action if errors else None,
			)
		)
	return results


def _mark_component_checks_not_run(
	results: _runner._OperationResults,
	component: _runner.PythonComponent,
	component_index: int,
	prepared: _runner.PreparedEnvironment,
	reason: str,
) -> None:
	prefix = f"python.component_{component_index}"
	component_scope = component.path.relative_to(results.root).as_posix()
	for suffix in ("ruff", "format"):
		results.append_not_run(f"{prefix}.{suffix}", component_scope, reason)
	if component.typecheck:
		results.append_not_run(f"{prefix}.mypy", component_scope, reason)
	if component.tests is not None:
		test_paths = tuple(
			path.relative_to(results.root).as_posix()
			for path in (*component.test_paths, *component.missing_test_paths)
		)
		test_scope = f"{component_scope}; tests={','.join(test_paths)}"
		results.append_not_run(f"{prefix}.pytest", test_scope, reason)
		if _runner._has_pinned_coverage(prepared):
			results.append_not_run(f"{prefix}.coverage", test_scope, reason)
			results.append_not_run(f"{prefix}.coverage_report", test_scope, reason)
