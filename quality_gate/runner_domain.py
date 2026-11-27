"""Quality Gate runner: domain operations."""

from __future__ import annotations

from typing import Literal

from . import runner as _runner


def _component_check_index(parts: list[str], domain: str) -> int | None:
	if len(parts) < _runner.CHECK_ID_COMPONENT_PARTS:
		return None
	if parts[0] != domain or not parts[1].startswith("component_"):
		return None
	try:
		index = int(parts[1].removeprefix("component_")) - 1
	except ValueError:
		return None
	return index if index >= 0 else None


def _python_component_check_scope(
	root: _runner.Path, component: _runner.ManifestPythonComponent, suffix: str
) -> str:
	if suffix.startswith("test_path_"):
		try:
			missing_index = int(suffix.removeprefix("test_path_")) - 1
		except ValueError:
			missing_index = -1
		missing_paths = tuple(path for path in component.test_paths if not (root / path).exists())
		if 0 <= missing_index < len(missing_paths):
			return missing_paths[missing_index]
	if suffix == "deptry":
		return f"{component.path}; dependencies={','.join(component.dependency_inputs)}"
	return component.path


def _planned_check_scopes(
	root: _runner.Path,
	manifest: _runner.Manifest,
	components: list[_runner.PythonComponent],
	mode: Literal["check", "audit", "product"],
	*,
	prepared: _runner.PreparedEnvironment | None = None,
) -> tuple[tuple[str, str], ...]:
	scope_resolver = _runner._OperationResults(root)
	scope_resolver.manifest = manifest
	checks: list[tuple[str, str]] = [
		("secrets.candidate", "staged candidate tree"),
		(
			("secrets.audit", "all reachable Git history")
			if mode in ("audit", "product")
			else ("secrets.history", "requested base-to-head Git range")
		),
	]
	for index, component in enumerate(manifest.web, start=1):
		for suffix in ("biome_lint", "biome_format"):
			check_id = f"web.component_{index}.{suffix}"
			checks.append((check_id, component.root))
	if not components:
		checks.append(("python.components", "declared Python components"))
	checks.extend(_runner._planned_python_check_scopes(root, components, prepared))
	return tuple(
		(check_id, scope if scope else scope_resolver.scope_for(check_id))
		for check_id, scope in checks
	)


def _planned_python_check_scopes(
	root: _runner.Path,
	components: list[_runner.PythonComponent],
	prepared: _runner.PreparedEnvironment | None,
) -> list[tuple[str, str]]:
	checks: list[tuple[str, str]] = []
	for index, component in enumerate(components, start=1):
		prefix = f"python.component_{index}"
		component_scope = component.path.relative_to(root).as_posix()
		if not component.path_exists:
			checks.append((f"{prefix}.path", component_scope))
		for missing_index, path in enumerate(component.missing_test_paths, start=1):
			checks.append(
				(f"{prefix}.test_path_{missing_index}", path.relative_to(root).as_posix())
			)
		dependency_inputs = tuple(
			path.relative_to(root).as_posix() for path in component.dependency_inputs
		)
		checks.append(
			(f"{prefix}.deptry", f"{component_scope}; dependencies={','.join(dependency_inputs)}")
		)
		checks.extend(
			(
				(f"{prefix}.ruff", component_scope),
				(f"{prefix}.format", component_scope),
			)
		)
		if component.typecheck:
			checks.append((f"{prefix}.mypy", component_scope))
	return checks


def emit(message: str) -> None:
	_runner.sys.stdout.write(f"{message}\n")


def _progress(message: str) -> None:
	_runner.sys.stderr.write(f"QUALITY GATE: {message}\n")
	_runner.sys.stderr.flush()


def decode_subprocess_output(output: bytes | str | None) -> str:
	"""Decode subprocess output across UTF-8 and Windows console encodings."""
	if output is None:
		return ""
	if isinstance(output, str):
		return output
	encodings = ["utf-8", _runner.locale.getpreferredencoding(False)]
	for encoding in dict.fromkeys(encodings):
		try:
			return output.decode(encoding)
		except UnicodeDecodeError:
			continue
	return output.decode("utf-8", errors="replace")


def _manifest_error(error: _runner.ValidationError) -> _runner.QualityGateError:
	return _runner.QualityGateError(
		f"{error.path}: {error.message}",
		check_id="manifest.contract",
		exit_code=2,
		recovery_action=f"correct {error.path} and run validate",
	)


def repository_root(root: _runner.Path | None) -> _runner.Path:
	if root is not None:
		return root.resolve()
	operation = _runner.current_operation()
	try:
		result = _runner.run_probe(
			["git", "rev-parse", "--show-toplevel"],
			timeout=None if operation is None else operation.command_timeout_seconds,
		)
	except (OSError, _runner.subprocess.TimeoutExpired) as error:
		raise _runner.QualityGateError(
			"Git could not identify the repository root.",
			check_id="runtime.repository",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="run the command inside a Git repository or pass --root",
		) from error
	if result.returncode:
		raise _runner.QualityGateError(
			"Run quality-gate inside a Git repository or pass --root.",
			check_id="runtime.repository",
			exit_code=2,
			recovery_action="run the command inside a Git repository or pass --root",
		)
	try:
		return _runner.Path(result.stdout.decode("utf-8").strip()).resolve()
	except (OSError, UnicodeError, RuntimeError) as error:
		raise _runner.QualityGateError(
			"Git returned an unusable repository root.",
			check_id="runtime.repository",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="run the command inside a Git repository or pass --root",
		) from error


def relative_path(root: _runner.Path, value: object, field: str) -> _runner.Path:
	if not isinstance(value, str) or not value:
		raise _runner.QualityGateError(
			f"{field} must be a non-empty relative path.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	resolved_root = root.resolve()
	path = (resolved_root / value).resolve()
	if resolved_root not in path.parents and path != resolved_root:
		raise _runner.QualityGateError(
			f"{field} must stay inside the repository.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	return path


def load_components(root: _runner.Path) -> list[_runner.PythonComponent]:
	try:
		manifest = _runner.load_manifest(root)
	except _runner.ValidationError as error:
		raise _runner._manifest_error(error) from error
	components: list[_runner.PythonComponent] = []
	for index, item in enumerate(manifest.python, start=1):
		path = _runner.relative_path(root, item.path, f"python entry {index}.path")
		test_paths = tuple(
			_runner.relative_path(root, test_path, f"python entry {index}.test_paths[{test_index}]")
			for test_index, test_path in enumerate(item.test_paths)
		)
		missing_tests = [test_path for test_path in test_paths if not test_path.exists()]
		existing_tests = tuple(test_path for test_path in test_paths if test_path.exists())
		components.append(
			_runner.PythonComponent(
				path=path,
				tests=test_paths[0] if test_paths else None,
				requirements=None,
				typecheck=item.typecheck,
				dependency_inputs=tuple(
					_runner.relative_path(
						root,
						dependency_input,
						f"python entry {index}.dependency_inputs[{dependency_index}]",
					)
					for dependency_index, dependency_input in enumerate(item.dependency_inputs)
				),
				test_paths=existing_tests,
				timeout_seconds=item.timeout_seconds,
				missing_test_paths=tuple(missing_tests),
				path_exists=path.is_dir(),
			)
		)
	return components
