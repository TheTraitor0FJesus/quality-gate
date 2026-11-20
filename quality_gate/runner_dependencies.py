"""Quality Gate runner: dependencies operations."""

from __future__ import annotations

from . import runner as _runner


def _dependency_configuration(pyprojects: list[_runner.Path]) -> dict[str, object]:
	if len(pyprojects) > 1:
		raise _runner.QualityGateError(
			"deptry accepts at most one pyproject.toml for a Python component",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="declare one authoritative pyproject.toml per Python component",
		)
	if not pyprojects:
		return {}
	try:
		if pyprojects[0].stat().st_size > _runner.MAX_DEPTRY_REPORT_BYTES:
			raise _runner.QualityGateError(
				"dependency metadata exceeds the size limit",
				check_id="runtime.command",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action="reduce the dependency metadata size and retry",
			)
		content = pyprojects[0].read_bytes()
		if len(content) > _runner.MAX_DEPTRY_REPORT_BYTES:
			raise _runner.QualityGateError(
				"dependency metadata exceeds the size limit",
				check_id="runtime.command",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action="reduce the dependency metadata size and retry",
			)
		metadata = _runner.tomllib.loads(content.decode("utf-8"))
	except (OSError, UnicodeError, _runner.tomllib.TOMLDecodeError) as error:
		raise _runner.QualityGateError(
			"deptry configuration metadata is unavailable or invalid",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore valid dependency metadata and retry",
		) from error
	tool = metadata.get("tool", {})
	candidate = tool.get("deptry", {}) if isinstance(tool, dict) else {}
	configuration = candidate if isinstance(candidate, dict) else {}
	for key in sorted(_runner._DEPTRY_SUPPRESSION_KEYS.intersection(configuration)):
		raise _runner.QualityGateError(
			f"tool.deptry.{key} bypasses the typed waiver contract",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action=f"remove tool.deptry.{key} and use one exact current manifest waiver",
		)
	return configuration


def _dependency_requirement_groups(
	actual_root: _runner.Path,
	component: _runner.PythonComponent,
	pyprojects: list[_runner.Path],
	configuration: dict[str, object],
) -> tuple[list[_runner.Path], list[_runner.Path]]:
	runtime_names = configuration.get("requirements_files", ["requirements.txt"])
	development_names = configuration.get(
		"requirements_files_dev", ["dev-requirements.txt", "requirements-dev.txt"]
	)
	if not isinstance(runtime_names, list) or not all(
		isinstance(value, str) for value in runtime_names
	):
		raise _runner.QualityGateError(
			"tool.deptry.requirements_files must classify dependency input paths",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="declare valid runtime requirement paths in tool.deptry",
		)
	if not isinstance(development_names, list) or not all(
		isinstance(value, str) for value in development_names
	):
		raise _runner.QualityGateError(
			"tool.deptry.requirements_files_dev must classify dependency input paths",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="declare valid development requirement paths in tool.deptry",
		)
	runtime_set = {value.replace("\\", "/") for value in runtime_names}
	development_set = {value.replace("\\", "/") for value in development_names}
	runtime_requirements: list[_runner.Path] = []
	development_requirements: list[_runner.Path] = []
	for path in component.dependency_inputs:
		if path in pyprojects:
			continue
		relative = str(path.relative_to(actual_root)).replace("\\", "/")
		if relative in development_set or path.name in development_set:
			development_requirements.append(path)
		elif relative in runtime_set or path.name in runtime_set:
			runtime_requirements.append(path)
		else:
			raise _runner.QualityGateError(
				f"dependency input is not classified as runtime or development: {relative}",
				check_id="runtime.command",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action=(
					"classify every requirements input in tool.deptry requirements_files or "
					"requirements_files_dev"
				),
			)
	return runtime_requirements, development_requirements


def _dependency_command(
	actual_root: _runner.Path,
	component: _runner.PythonComponent,
	prepared: _runner.PreparedEnvironment,
	python_executable: _runner.Path,
	report_path: _runner.Path,
) -> list[str]:
	pyprojects = [path for path in component.dependency_inputs if path.name == "pyproject.toml"]
	configuration = _runner._dependency_configuration(pyprojects)
	runtime_requirements, development_requirements = _runner._dependency_requirement_groups(
		actual_root, component, pyprojects, configuration
	)
	if pyprojects and (runtime_requirements or development_requirements):
		raise _runner.QualityGateError(
			"deptry cannot analyze mixed pyproject.toml and requirements dependency inputs",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action=(
				"declare one authoritative dependency metadata format per Python component"
			),
		)
	command = [
		*_runner._tool_command(prepared, python_executable, "deptry"),
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
		command.extend(("--exclude", rf"{_runner.re.escape(relative_test)}(?:/|$)"))
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


def _dependency_issue(value: object) -> tuple[_runner.Finding, str]:
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
		or not _runner._DEPTRY_MODULE.fullmatch(module)
		or len(message) > _runner.MAX_DEPTRY_TEXT_CHARS
		or not message.isprintable()
		or (line is not None and (not isinstance(line, int) or line < 1))
	):
		raise ValueError("deptry issue contains an unsupported value")
	normalized_path = path.replace("\\", "/")
	parsed_path = _runner.PurePosixPath(normalized_path)
	if (
		len(normalized_path) > _runner.MAX_DEPTRY_TEXT_CHARS
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
		_runner.Finding(
			path=normalized_path,
			line=line,
			message=f"{code} {message}",
			action=f"fix the dependency declaration or add a reviewed waiver for {target}",
		),
		target,
	)


def _dependency_inline_suppressions(
	actual_root: _runner.Path, component: _runner.PythonComponent
) -> tuple[_runner.Finding, ...]:
	findings = []
	for path in sorted(component.path.rglob("*.py")):
		if any(
			path == test_path or test_path in path.parents for test_path in component.test_paths
		):
			continue
		try:
			if path.stat().st_size > _runner.MAX_DEPTRY_SOURCE_BYTES:
				raise _runner.QualityGateError(
					"Python source exceeds the dependency-analysis size limit",
					check_id="runtime.command",
					exit_code=_runner.EXIT_UNCHECKED,
					recovery_action="split the oversized Python source and retry",
				)
			content = path.read_bytes()
		except OSError as error:
			raise _runner.QualityGateError(
				"Python source is unreadable during dependency suppression validation",
				check_id="runtime.command",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action="restore readable Python source and retry",
			) from error
		if len(content) > _runner.MAX_DEPTRY_SOURCE_BYTES:
			raise _runner.QualityGateError(
				"Python source exceeds the dependency-analysis size limit",
				check_id="runtime.command",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action="split the oversized Python source and retry",
			)
		try:
			comments = tuple(
				token
				for token in _runner.tokenize.tokenize(_runner.io.BytesIO(content).readline)
				if token.type == _runner.tokenize.COMMENT
			)
		except (SyntaxError, _runner.tokenize.TokenError) as error:
			raise _runner.QualityGateError(
				"Python source cannot be tokenized for dependency suppression validation",
				check_id="runtime.command",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action="restore parseable Python source and retry",
			) from error
		for comment in comments:
			if not _runner._DEPTRY_INLINE_IGNORE.search(comment.string):
				continue
			findings.append(
				_runner.Finding(
					path=str(path.relative_to(actual_root)).replace("\\", "/"),
					line=comment.start[0],
					message="inline deptry suppression bypasses the typed waiver contract",
					action="remove the inline suppression and use an exact manifest waiver",
				)
			)
	return tuple(findings)


def _read_dependency_report(report_path: _runner.Path) -> list[object]:
	try:
		if report_path.stat().st_size > _runner.MAX_DEPTRY_REPORT_BYTES:
			raise ValueError("deptry report exceeds the size limit")
		content = report_path.read_bytes()
	except OSError as error:
		raise ValueError("deptry report is unavailable") from error
	if len(content) > _runner.MAX_DEPTRY_REPORT_BYTES:
		raise ValueError("deptry report exceeds the size limit")
	try:
		value = _runner.json.loads(content.decode("utf-8"))
	except (UnicodeError, _runner.json.JSONDecodeError) as error:
		raise ValueError("deptry report is not valid UTF-8 JSON") from error
	if not isinstance(value, list):
		raise ValueError("deptry report is not a list")
	return value


def _dependency_hygiene_result(
	actual_root: _runner.Path,
	manifest: _runner.Manifest,
	component: _runner.PythonComponent,
	prepared: _runner.PreparedEnvironment,
	component_index: int,
	python_executable: _runner.Path | None,
	report_path: _runner.Path,
	deadline: float,
) -> _runner.CheckResult:
	check_id = f"python.component_{component_index}.deptry"
	component_scope = component.path.relative_to(actual_root).as_posix()
	dependency_scope = ",".join(
		path.relative_to(actual_root).as_posix() for path in component.dependency_inputs
	)
	return _runner.observe_check(
		check_id,
		f"{component_scope}; dependencies={dependency_scope}",
		lambda: _runner._dependency_hygiene_result_impl(
			actual_root,
			manifest,
			component,
			prepared,
			component_index,
			python_executable,
			report_path,
			deadline,
		),
		process_required=True,
	)


def _dependency_hygiene_result_impl(
	actual_root: _runner.Path,
	manifest: _runner.Manifest,
	component: _runner.PythonComponent,
	prepared: _runner.PreparedEnvironment,
	component_index: int,
	python_executable: _runner.Path | None,
	report_path: _runner.Path,
	deadline: float,
) -> _runner.CheckResult:
	check_id = f"python.component_{component_index}.deptry"
	if not component.dependency_inputs:
		return _runner.CheckResult(
			check_id,
			_runner.Status.NOT_APPLICABLE,
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
		return _runner.CheckResult(
			check_id,
			_runner.Status.UNCHECKED,
			reason,
			findings=tuple(
				_runner.Finding(
					path=str(path.relative_to(actual_root)).replace("\\", "/"),
					message="declared dependency input does not exist",
					action="restore the declared dependency input",
				)
				for path in missing_inputs
			),
			recovery_action="restore the component runtime and dependency metadata, then retry",
		)
	try:
		inline_suppressions = _runner._dependency_inline_suppressions(actual_root, component)
		if inline_suppressions:
			return _runner.CheckResult(
				check_id,
				_runner.Status.FAILED,
				"inline deptry suppressions bypass the typed waiver contract",
				findings=inline_suppressions,
				recovery_action="remove every inline deptry suppression and retry",
			)
		command = _runner._dependency_command(
			actual_root, component, prepared, python_executable, report_path
		)
		returncode, output = _runner._run_bounded_subprocess(
			command,
			actual_root,
			_runner._safe_environment(str(report_path.parent)),
			_runner._remaining(deadline, manifest.repository.command_timeout_seconds),
		)
	except (
		OSError,
		_runner.subprocess.TimeoutExpired,
		_runner.OutputReadError,
		_runner.QualityGateError,
	) as error:
		return _runner.CheckResult(
			check_id,
			_runner.Status.UNCHECKED,
			"dependency analysis could not run",
			findings=(_runner.Finding(message=_runner.redact(str(error))),),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	try:
		raw_issues = _runner._read_dependency_report(report_path)
		issues = tuple(_runner._dependency_issue(value) for value in raw_issues)
	except ValueError as error:
		return _runner.CheckResult(
			check_id,
			_runner.Status.UNCHECKED,
			"dependency analysis report is unavailable or invalid",
			findings=(_runner.Finding(message=_runner.redact(output.strip() or str(error))),),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	if returncode not in {0, 1} or (returncode == 0 and issues) or (returncode == 1 and not issues):
		return _runner.CheckResult(
			check_id,
			_runner.Status.UNCHECKED,
			"dependency analysis returned an unusable result",
			findings=(
				_runner.Finding(
					message=_runner.redact(output.strip() or f"deptry exited with {returncode}")
				),
			),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	if not issues:
		return _runner.CheckResult(
			check_id, _runner.Status.PASSED, "dependency declarations match imports"
		)
	unwaived = tuple(
		finding for finding, target in issues if manifest.resolve_waiver(check_id, target) is None
	)
	if not unwaived:
		return _runner.CheckResult(
			check_id,
			_runner.Status.WAIVED,
			"dependency findings have exact current waivers",
			findings=tuple(finding for finding, _target in issues),
			waiver_target=issues[0][1],
		)
	return _runner.CheckResult(
		check_id,
		_runner.Status.FAILED,
		"dependency declarations do not match imports",
		findings=unwaived,
		recovery_action="fix each dependency finding or add one exact current human waiver",
	)
