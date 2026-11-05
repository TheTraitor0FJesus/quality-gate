"""Quality Gate runner: process operations."""

from __future__ import annotations

from . import runner as _runner


def _run_python_snapshot_checks(
	actual_root: _runner.Path,
	manifest: _runner.Manifest,
	components: list[_runner.PythonComponent],
	prepared: _runner.PreparedEnvironment,
	*,
	deadline: float,
) -> list[_runner.CheckResult]:
	policy_path = prepared.policy_root / "quality_gate" / "policy"
	if not policy_path.is_dir():
		raise _runner.QualityGateError(
			"cached policy release has no policy directory",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="sync a complete policy release and retry the quality gate",
		)
	return _runner._run_python_checks(
		actual_root, manifest, components, prepared, deadline=deadline
	)


def validate(root: _runner.Path | None = None) -> None:
	actual_root = _runner.repository_root(root)
	_runner.load_manifest(actual_root)
	_runner.emit(f"QUALITY GATE VALID (schema 2): {actual_root / _runner.MANIFEST_NAME}")


def required_documents_result(
	root: _runner.Path, manifest: _runner.Manifest
) -> _runner.CheckResult:
	findings: list[_runner.Finding] = []
	unreadable = False
	for document in manifest.repository.required_documents:
		target = root / document
		try:
			if not target.is_file():
				findings.append(
					_runner.Finding(
						document,
						message="required document is missing",
						action=f"restore {document}",
					)
				)
			elif not target.read_bytes().strip():
				findings.append(
					_runner.Finding(
						document, message="required document is empty", action=f"restore {document}"
					)
				)
		except OSError:
			unreadable = True
			findings.append(
				_runner.Finding(
					document,
					message="required document is unreadable",
					action=f"restore {document}",
				)
			)
	return _runner.CheckResult(
		check_id="manifest.documents",
		status=(
			_runner.Status.UNCHECKED
			if unreadable
			else _runner.Status.FAILED
			if findings
			else _runner.Status.PASSED
		),
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
	root: _runner.Path,
	environment: dict[str, str],
	timeout: float | None,
) -> tuple[int, str]:
	"""Run a subprocess while retaining only a bounded amount of output."""
	operation = _runner.current_operation()
	if operation is not None:
		try:
			code, output, diagnostics = operation.run_process(
				command,
				root=root,
				environment=environment,
				timeout_seconds=timeout,
				limit=_runner.MAX_COMMAND_OUTPUT_BYTES,
				truncate=True,
			)
		except _runner.EditError as error:
			if str(error) == "deadline":
				raise _runner.subprocess.TimeoutExpired(command, timeout or 0) from error
			raise _runner.OutputReadError("owned process output could not be collected") from error
		return code, _runner.decode_subprocess_output(output + diagnostics)
	return _runner._run_legacy_subprocess(command, root, environment, timeout)


def _run_legacy_subprocess(
	command: list[str],
	root: _runner.Path,
	environment: dict[str, str],
	timeout: float | None,
) -> tuple[int, str]:
	"""Preserve standalone formatting/tool callers outside an owned Gate operation."""
	process = _runner.subprocess.Popen(
		command,
		cwd=root,
		env=environment,
		stdout=_runner.subprocess.PIPE,
		stderr=_runner.subprocess.STDOUT,
	)
	retained = bytearray()
	reader_errors: list[Exception] = []
	stdout = process.stdout
	if stdout is None:
		raise _runner.OutputReadError("subprocess output pipe is unavailable")

	def drain_output() -> None:
		try:
			while True:
				chunk = stdout.read(4096)
				if not chunk:
					return
				remaining = _runner.MAX_COMMAND_OUTPUT_BYTES - len(retained)
				if remaining > 0:
					retained.extend(chunk[:remaining])
		except (OSError, ValueError) as error:
			reader_errors.append(error)

	reader = _runner.threading.Thread(target=drain_output, daemon=True)
	reader.start()
	try:
		returncode = process.wait(timeout=timeout)
	except _runner.subprocess.TimeoutExpired:
		process.kill()
		process.wait(timeout=_runner.PROCESS_CLEANUP_TIMEOUT_SECONDS)
		reader.join(timeout=_runner.PROCESS_CLEANUP_TIMEOUT_SECONDS)
		raise
	reader.join(timeout=_runner.PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader.is_alive():
		raise _runner.subprocess.TimeoutExpired(command, _runner.PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader_errors:
		raise _runner.OutputReadError("subprocess output could not be read") from reader_errors[0]
	return returncode, _runner.decode_subprocess_output(bytes(retained))


def run(
	command: list[str],
	root: _runner.Path,
	environment: dict[str, str],
	*,
	timeout: float | None = None,
) -> str:
	"""Run one quality command and return its decoded standard output."""
	try:
		returncode, output = _runner._run_bounded_subprocess(command, root, environment, timeout)
	except OSError as error:
		raise _runner.QualityGateError(
			f"{' '.join(command)} could not be executed: {error}",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore the required tool or runtime and retry the quality gate",
		) from error
	except _runner.subprocess.TimeoutExpired as error:
		raise _runner.QualityGateError(
			f"{' '.join(command)} timed out after {timeout:g} seconds",
			check_id="runtime.timeout",
			exit_code=2,
			recovery_action="inspect the command and retry within the declared time budget",
		) from error
	except _runner.OutputReadError as error:
		raise _runner.QualityGateError(
			f"{' '.join(command)} output could not be read",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore subprocess output handling and retry the quality gate",
		) from error
	if returncode:
		detail = output.strip()
		detail = _runner._bounded_output(detail)
		if not detail:
			detail = f"Command exited with {returncode}."
		lower_detail = detail.casefold()
		missing_tool = any(
			f"no module named {tool_name}" in lower_detail
			for tool_name in ("ruff", "mypy", "pytest", "biome")
		)
		pytest_collection_error = "pytest" in " ".join(command).casefold() and any(
			marker in lower_detail
			for marker in ("error collecting", "no tests collected", "no tests ran")
		)
		raise _runner.QualityGateError(
			f"{' '.join(command)}\n\n{_runner.redact(detail)}",
			check_id="runtime.command",
			exit_code=_runner.EXIT_UNCHECKED if missing_tool or pytest_collection_error else 1,
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
	return _runner._bounded_output(output).strip()


def _bounded_output(value: str) -> str:
	"""Limit external command output retained by the gate."""
	if len(value) <= _runner.MAX_COMMAND_OUTPUT_CHARS:
		return value
	marker = "\n[output truncated]\n"
	available = _runner.MAX_COMMAND_OUTPUT_CHARS - len(marker)
	prefix = available // 2
	suffix = available - prefix
	return value[:prefix] + marker + value[-suffix:]


def _pytest_outcome_summary(output: str) -> str | None:
	"""Extract bounded test totals from pytest's final terminal summary line."""
	for line in reversed(output.splitlines()):
		if not _runner._PYTEST_SUMMARY_DURATION.search(line):
			continue
		if _runner._PYTEST_NO_TESTS.search(line):
			return "0 tests ran"
		counts = [
			f"{match.group('count')} {match.group('outcome').lower()}"
			for match in _runner._PYTEST_SUMMARY_COUNT.finditer(line)
		]
		if counts:
			return ", ".join(counts)
	return None


@_runner.contextmanager
def temporary_directory(root: _runner.Path) -> _runner.Iterator[str]:
	operation = _runner.current_operation()
	if operation is None:
		with _runner.tempfile.TemporaryDirectory(prefix="quality-gate-") as directory:
			yield directory
		return
	path = _runner.Path(_runner.tempfile.mkdtemp(prefix="quality-gate-"))
	try:
		operation.register_cleanup(path)
	except BaseException:
		_runner.remove_snapshot(path)
		raise
	yield str(path)


def _error_result(error: _runner.QualityGateError) -> _runner.CheckResult:
	status = (
		_runner.Status.UNCHECKED
		if error.exit_code == _runner.EXIT_UNCHECKED
		else _runner.Status.FAILED
	)
	return _runner.CheckResult(
		check_id=error.check_id,
		status=status,
		summary=_runner.redact(str(error)),
		findings=(_runner.Finding(message=_runner.redact(str(error))),),
		recovery_action=error.recovery_action,
	)


def _remaining(deadline: float, timeout: int) -> float:
	operation = _runner.current_operation()
	if operation is not None:
		return operation.remaining(float(timeout))
	remaining = min(float(timeout), deadline - _runner.time.monotonic())
	if remaining <= 0:
		raise _runner.QualityGateError(
			"quality gate exceeded its overall time budget",
			check_id="runtime.gate_timeout",
			exit_code=2,
			recovery_action="inspect slow checks and retry within the gate time budget",
		)
	return remaining


def _snapshot_candidate_identity(snapshot: object) -> str:
	"""Read the snapshot module's public identity without interpreting its storage."""
	identity = getattr(snapshot, "index_identity", None)
	if isinstance(identity, str):
		return identity
	return "staged Git index identity unavailable"


def _configure_operation_budget(repository: _runner.Path, operation: _runner.GateOperation) -> None:
	"""Read staged time limits without restarting the operation clock."""
	try:
		result = _runner.run_probe(
			["git", "-C", str(repository), "show", f":{_runner.MANIFEST_NAME}"],
			cwd=repository,
			timeout=operation.command_timeout_seconds,
		)
	except (OSError, _runner.subprocess.TimeoutExpired):
		return
	if result.returncode:
		return
	try:
		value = _runner.tomllib.loads(result.stdout.decode("utf-8"))
	except (UnicodeError, _runner.tomllib.TOMLDecodeError):
		return
	repository_table = value.get("repository")
	defaults = repository_table.get("defaults") if isinstance(repository_table, dict) else None
	if not isinstance(defaults, dict):
		return
	gate_timeout = defaults.get("gate_timeout_seconds", _runner.DEFAULT_GATE_TIMEOUT_SECONDS)
	command_timeout = defaults.get(
		"command_timeout_seconds", _runner.DEFAULT_COMMAND_TIMEOUT_SECONDS
	)
	if isinstance(gate_timeout, bool) or not isinstance(gate_timeout, int) or gate_timeout <= 0:
		gate_timeout = _runner.DEFAULT_GATE_TIMEOUT_SECONDS
	if (
		isinstance(command_timeout, bool)
		or not isinstance(command_timeout, int)
		or command_timeout <= 0
	):
		command_timeout = _runner.DEFAULT_COMMAND_TIMEOUT_SECONDS
	operation.configure_budget(float(gate_timeout), command_timeout)


def _ci_history_refs(base: str | None, head: str | None) -> tuple[str | None, str]:
	"""Resolve pull-request commit SHAs from GitHub event data when available."""
	event_path = _runner.os.environ.get("GITHUB_EVENT_PATH")
	event_base: str | None = None
	event_head: str | None = None
	if event_path:
		try:
			event = _runner.json.loads(_runner.Path(event_path).read_text(encoding="utf-8"))
			pull_request = event.get("pull_request", {})
			base_data = pull_request.get("base", {})
			head_data = pull_request.get("head", {})
			if isinstance(base_data, dict) and isinstance(base_data.get("sha"), str):
				event_base = base_data["sha"]
			if isinstance(head_data, dict) and isinstance(head_data.get("sha"), str):
				event_head = head_data["sha"]
		except (OSError, UnicodeError, _runner.json.JSONDecodeError, AttributeError):
			_runner._LOGGER.debug("GitHub event payload is unavailable", exc_info=True)
	return (
		base or event_base or _runner.os.environ.get("GITHUB_BASE_REF"),
		head or event_head or _runner.os.environ.get("GITHUB_SHA") or "HEAD",
	)


def _safe_environment(temporary_path: str) -> dict[str, str]:
	allowed = {
		"LANG",
		"LC_ALL",
		"PATHEXT",
		"PATH",
		"QUALITY_GATE_QUEUE_TIMEOUT_SECONDS",
		"QUALITY_GATE_RUFF_THREADS",
		"SYSTEMROOT",
		"WINDIR",
	}
	environment = {key: value for key, value in _runner.os.environ.items() if key in allowed}
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
			"COVERAGE_FILE": str(_runner.Path(temporary_path) / ".coverage"),
		}
	)
	if _runner.os.name == "nt":
		environment["USERPROFILE"] = temporary_path
	environment.update(_runner.child_admission_environment())
	return environment


def _ruff_environment(environment: dict[str, str]) -> dict[str, str]:
	raw = _runner.os.environ.get(_runner.RUFF_THREADS_ENV)
	if raw is None:
		threads = _runner.DEFAULT_RUFF_THREADS
	elif (
		not _runner.re.fullmatch(r"[0-9]{1,2}", raw)
		or not 1 <= int(raw) <= _runner.MAX_RUFF_THREADS
	):
		raise _runner.QualityGateError(
			f"{_runner.RUFF_THREADS_ENV} must be an integer from 1 "
			f"through {_runner.MAX_RUFF_THREADS}",
			check_id="runtime.ruff_threads",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action=(
				f"set {_runner.RUFF_THREADS_ENV} to a whole number from 1 "
				f"through {_runner.MAX_RUFF_THREADS}"
			),
		)
	else:
		threads = int(raw)
	return environment | {"RAYON_NUM_THREADS": str(threads)}


def _runtime_python(prepared: _runner.PreparedEnvironment, component_index: int) -> _runner.Path:
	try:
		inspection = prepared.runtimes[component_index - 1]
	except IndexError as error:
		raise _runner.QualityGateError(
			f"runtime is unavailable for Python component {component_index}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		) from error
	if not inspection.current or inspection.python is None:
		raise _runner.QualityGateError(
			f"runtime is unavailable for Python component {component_index}: "
			f"{inspection.reason or 'runtime is stale'}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		)
	return inspection.python


def _tool_command(
	prepared: _runner.PreparedEnvironment, python: _runner.Path, name: str
) -> list[str]:
	release_manifest = getattr(prepared, "release_manifest", None)
	if release_manifest is not None:
		for tool in release_manifest.tools:
			if tool.name == name:
				if tool.path.lower().endswith(".whl"):
					return [str(python), "-m", name]
				return [str(prepared.policy_root / tool.path)]
	return [str(python), "-m", name]


def _has_pinned_coverage(prepared: _runner.PreparedEnvironment) -> bool:
	"""Return whether the selected policy release pins a coverage provider."""
	release_manifest = getattr(prepared, "release_manifest", None)
	return release_manifest is not None and any(
		tool.name.casefold() == "coverage" for tool in release_manifest.tools
	)
