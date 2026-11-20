"""Quality Gate runner: web operations."""

from __future__ import annotations

from typing import Literal

from . import runner as _runner


def _is_excluded(path: _runner.PurePosixPath, patterns: tuple[str, ...]) -> bool:
	for pattern in patterns:
		if path.match(pattern):
			return True
		prefix = pattern[:-3].rstrip("/") + "/"
		if pattern.endswith("/**") and path.as_posix().startswith(prefix):
			return True
	return False


def _is_link(path: _runner.Path) -> bool:
	is_junction = getattr(path, "is_junction", None)
	return path.is_symlink() or (is_junction is not None and is_junction())


def _unsafe_web_path(root: _runner.Path, path: _runner.Path) -> bool:
	resolved_root = root.resolve(strict=True)
	resolved_path = path.resolve(strict=True)
	if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
		return True
	relative_parts = path.relative_to(root).parts
	path_chain = (
		root.joinpath(*relative_parts[:index]) for index in range(1, len(relative_parts) + 1)
	)
	return any(_runner._is_link(candidate) for candidate in path_chain)


def _web_assets(
	root: _runner.Path,
	component: _runner.WebComponent,
	patterns: tuple[str, ...],
) -> tuple[tuple[_runner.Path, ...], tuple[_runner.Finding, ...]]:
	component_root = root / component.root
	assets: set[_runner.Path] = set()
	unsafe: list[_runner.Finding] = []
	for pattern in patterns:
		for candidate in component_root.glob(pattern):
			relative = _runner.PurePosixPath(candidate.relative_to(component_root).as_posix())
			if _runner._is_excluded(relative, component.exclude):
				continue
			try:
				is_unsafe = _runner._unsafe_web_path(root, candidate)
			except OSError:
				is_unsafe = True
			if is_unsafe:
				unsafe.append(
					_runner.Finding(
						path=candidate.relative_to(root).as_posix(),
						message="declared web asset is outside or traverses a symbolic link",
						action="replace it with a project-owned regular file",
					)
				)
				continue
			if not candidate.is_file():
				continue
			assets.add(candidate)
	return tuple(sorted(assets, key=lambda path: path.relative_to(root).as_posix())), tuple(unsafe)


def _web_budget_result(
	root: _runner.Path,
	component: _runner.WebComponent,
	component_index: int,
	*,
	language: Literal["javascript", "css"],
) -> _runner.CheckResult:
	patterns = component.javascript if language == "javascript" else component.css
	label = "JavaScript" if language == "javascript" else "CSS"
	check_id = f"web.component_{component_index}.{language}_budget"
	if not patterns:
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.NOT_APPLICABLE,
			summary=f"no {label} assets are declared",
			recovery_action=f"declare {label} asset patterns if this component owns them",
		)
	component_root = root / component.root
	root_parts = component.root.split("/")
	path_chain = tuple(
		root.joinpath(*root_parts[:index]) for index in range(1, len(root_parts) + 1)
	)
	try:
		resolved_root = root.resolve(strict=True)
		resolved_component_root = component_root.resolve(strict=True)
	except OSError:
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.UNCHECKED,
			summary=f"{label} asset boundary could not be resolved",
			findings=(
				_runner.Finding(
					path=component.root,
					message="declared web component root could not be resolved",
					action="restore a readable project-owned component root",
				),
			),
			recovery_action="restore a readable web component root and retry",
		)
	root_is_unsafe = (
		resolved_component_root != resolved_root
		and resolved_root not in resolved_component_root.parents
	) or any(_runner._is_link(path) for path in path_chain)
	if root_is_unsafe:
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.UNCHECKED,
			summary=f"{label} asset boundary is unsafe",
			findings=(
				_runner.Finding(
					path=component.root,
					message="declared web component root is or traverses a symbolic link",
					action="use a project-owned regular directory inside the repository",
				),
			),
			recovery_action="declare a regular web component root inside the repository",
		)
	if not component_root.is_dir():
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.UNCHECKED,
			summary=f"{label} asset budget is unavailable",
			findings=(
				_runner.Finding(
					path=component.root,
					message="declared web component root does not exist",
					action="restore the declared web component root",
				),
			),
			recovery_action="restore the declared web component root and retry the quality gate",
		)
	try:
		assets, unsafe = _runner._web_assets(root, component, patterns)
		sizes = tuple((asset, asset.stat().st_size) for asset in assets)
	except OSError:
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.UNCHECKED,
			summary=f"{label} assets could not be measured",
			recovery_action="restore readable project-owned web assets and retry the quality gate",
		)
	if unsafe:
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.UNCHECKED,
			summary=f"{label} asset boundary contains an unsafe file",
			findings=unsafe,
			recovery_action="replace linked or external assets with project-owned regular files",
		)
	if not sizes:
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.UNCHECKED,
			summary=f"declared {label} patterns match no project-owned assets",
			recovery_action=f"correct the {label} patterns or add the declared assets and retry",
		)
	file_kib = component.javascript_file_kib if language == "javascript" else component.css_file_kib
	total_kib = (
		component.javascript_total_kib if language == "javascript" else component.css_total_kib
	)
	file_limit = file_kib * 1024
	total_limit = total_kib * 1024
	findings = [
		_runner.Finding(
			path=asset.relative_to(root).as_posix(),
			message=f"{label} file size {size} bytes exceeds {file_limit} bytes",
			action=f"reduce the file to at most {file_kib} KiB or set a reviewed component limit",
		)
		for asset, size in sizes
		if size > file_limit
	]
	total_size = sum(size for _, size in sizes)
	if total_size > total_limit:
		findings.append(
			_runner.Finding(
				path=component.root,
				message=f"total {label} size {total_size} bytes exceeds {total_limit} bytes",
				action=(
					f"reduce component assets to at most {total_kib} KiB or set a reviewed limit"
				),
			)
		)
	if findings:
		return _runner.CheckResult(
			check_id=check_id,
			status=_runner.Status.FAILED,
			summary=f"{label} assets exceed the configured size budget",
			findings=tuple(findings),
			recovery_action="reduce the staged assets or review and declare a justified limit",
		)
	return _runner.CheckResult(
		check_id=check_id,
		status=_runner.Status.PASSED,
		summary=f"{len(sizes)} {label} asset(s), {total_size} bytes, within budget",
	)


def _fixed_check_result(result: _runner.CheckResult) -> _runner.Callable[[], _runner.CheckResult]:
	def check() -> _runner.CheckResult:
		return result

	return check


def _web_budget_check(
	root: _runner.Path,
	component: _runner.WebComponent,
	component_index: int,
	language: Literal["javascript", "css"],
) -> _runner.Callable[[], _runner.CheckResult]:
	def check() -> _runner.CheckResult:
		return _runner._web_budget_result(root, component, component_index, language=language)

	return check


def web_budget_results(
	root: _runner.Path, manifest: _runner.Manifest
) -> tuple[_runner.CheckResult, ...]:
	results: list[_runner.CheckResult] = []
	for index, component in enumerate(manifest.web, start=1):
		for language in ("javascript", "css"):
			check_id = f"web.component_{index}.{language}_budget"
			results.append(
				_runner.observe_check(
					check_id,
					component.root,
					_runner._web_budget_check(root, component, index, language),
				)
			)
	return tuple(results)


def _biome_unchecked_results(
	component_index: int,
	message: str,
	*,
	findings: tuple[_runner.Finding, ...] = (),
	recovery_action: str = (
		"restore the Biome policy and declared web assets, then retry the quality gate"
	),
) -> list[_runner.CheckResult]:
	return [
		_runner.CheckResult(
			check_id=f"web.component_{component_index}.biome_{check}",
			status=_runner.Status.UNCHECKED,
			summary=message,
			findings=findings,
			recovery_action=recovery_action,
		)
		for check in ("lint", "format")
	]


def _biome_inventory(policy_root: _runner.Path) -> _runner.BiomeBinary:
	"""Return the pinned Biome version, platform path, and digest."""
	try:
		raw = _runner.tomllib.loads(
			(policy_root / "quality_gate" / "policy" / _runner.BIOME_POLICY).read_text(
				encoding="utf-8"
			)
		)
	except (OSError, UnicodeError, _runner.tomllib.TOMLDecodeError) as error:
		raise _runner.QualityGateError(
			"Biome inventory is missing or invalid",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		) from error
	biome = raw.get("biome")
	platform = "windows-x64" if _runner.os.name == "nt" else "linux-x64"
	if not isinstance(biome, dict) or not isinstance(biome.get("version"), str):
		raise _runner.QualityGateError(
			"Biome inventory has no pinned version",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		)
	platforms = biome.get("platforms")
	entry = platforms.get(platform) if isinstance(platforms, dict) else None
	expected_path = {
		"linux-x64": "biome-linux-x64",
		"windows-x64": "biome-win32-x64.exe",
	}[platform]
	path = entry.get("path") if isinstance(entry, dict) else None
	digest = entry.get("sha256") if isinstance(entry, dict) else None
	if (
		not isinstance(path, str)
		or not path.strip()
		or path != expected_path
		or not isinstance(digest, str)
		or not _runner.re.fullmatch(r"[0-9a-f]{64}", digest)
	):
		raise _runner.QualityGateError(
			f"Biome inventory has no usable {platform} binary pin",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		)
	return _runner.BiomeBinary(biome["version"], path, digest)


def _biome_command(prepared: _runner.PreparedEnvironment) -> list[str]:
	pinned = _runner._biome_inventory(prepared.policy_root)
	release_manifest = getattr(prepared, "release_manifest", None)
	tools = getattr(release_manifest, "tools", ())
	tool = next((item for item in tools if item.name == "biome"), None)
	if tool is None or (
		tool.version != pinned.version
		or tool.path.replace("\\", "/") != pinned.path
		or tool.sha256 != pinned.sha256
	):
		raise _runner.QualityGateError(
			"selected release does not contain the pinned Biome binary",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="sync the release with the exact pinned Biome inventory and retry",
		)
	config = prepared.policy_root / "quality_gate" / "policy" / _runner.BIOME_CONFIG
	if not config.is_file():
		raise _runner.QualityGateError(
			"Biome configuration is unavailable",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome configuration and retry the quality gate",
		)
	binary = prepared.policy_root / tool.path
	try:
		if (
			not binary.is_file()
			or _runner.hashlib.sha256(binary.read_bytes()).hexdigest() != tool.sha256
		):
			raise OSError("Biome binary is missing or corrupt")
	except OSError as error:
		raise _runner.QualityGateError(
			"Biome binary is missing or corrupt",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="sync the exact pinned Biome binary and retry the quality gate",
		) from error
	return [str(binary), "ci", "--config-path", str(config)]


def _biome_asset_paths(
	root: _runner.Path, component: _runner.WebComponent, component_index: int
) -> list[str]:
	budget_results: list[_runner.CheckResult] = []
	if component.javascript:
		budget_results.append(
			_runner._web_budget_result(root, component, component_index, language="javascript")
		)
	if component.css:
		budget_results.append(
			_runner._web_budget_result(root, component, component_index, language="css")
		)
	if any(result.status is _runner.Status.UNCHECKED for result in budget_results):
		raise _runner.QualityGateError(
			"Biome asset inputs are unavailable",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="restore readable declared web assets and retry the quality gate",
		)
	assets: set[_runner.Path] = set()
	for patterns in (component.javascript, component.css):
		matched, unsafe = _runner._web_assets(root, component, patterns)
		if unsafe:
			raise _runner.QualityGateError(
				"Biome asset boundary contains an unsafe file",
				check_id="runtime.policy",
				exit_code=_runner.EXIT_UNCHECKED,
				recovery_action=(
					"replace linked or external assets with project-owned regular files"
				),
			)
		assets.update(matched)
	if not assets:
		raise _runner.QualityGateError(
			"declared Biome patterns match no project-owned assets",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action="correct the declared web patterns and retry the quality gate",
		)
	if any(asset.stat().st_size > _runner.BIOME_MAX_FILE_BYTES for asset in assets):
		raise _runner.QualityGateError(
			"declared Biome asset exceeds the policy file-size limit",
			check_id="runtime.policy",
			exit_code=_runner.EXIT_UNCHECKED,
			recovery_action=(
				"reduce the declared asset below the Biome policy file-size limit and retry"
			),
		)
	return [asset.relative_to(root).as_posix() for asset in sorted(assets)]


def _biome_component_commands(
	component_index: int,
	command: list[str],
	paths: list[str],
	timeout: int,
) -> tuple[tuple[str, list[str], int], tuple[str, list[str], int]]:
	return (
		(
			f"web.component_{component_index}.biome_lint",
			[
				*command,
				"--formatter-enabled=false",
				"--linter-enabled=true",
				"--assist-enabled=false",
				*paths,
			],
			timeout,
		),
		(
			f"web.component_{component_index}.biome_format",
			[
				*command,
				"--formatter-enabled=true",
				"--linter-enabled=false",
				"--assist-enabled=false",
				*paths,
			],
			timeout,
		),
	)


def _run_web_checks(
	actual_root: _runner.Path,
	manifest: _runner.Manifest,
	prepared: _runner.PreparedEnvironment,
	*,
	deadline: float,
) -> list[_runner.CheckResult]:
	if not manifest.web:
		return []
	commands: list[tuple[str, list[str], int]] = []
	pre_results = _runner._OperationResults(actual_root)
	pre_results.manifest = manifest
	for component_index, component in enumerate(manifest.web, start=1):
		try:
			paths = _runner._biome_asset_paths(actual_root, component, component_index)
			command = _runner._biome_command(prepared)
		except (OSError, _runner.QualityGateError) as error:
			quality_error = (
				error
				if isinstance(error, _runner.QualityGateError)
				else _runner.QualityGateError(
					str(error),
					check_id="runtime.policy",
					exit_code=_runner.EXIT_UNCHECKED,
					recovery_action=(
						"restore the pinned Biome policy and web assets, "
						"then retry the quality gate"
					),
				)
			)
			for result in _runner._biome_unchecked_results(
				component_index,
				str(quality_error),
				recovery_action=quality_error.recovery_action,
			):
				pre_results.append(
					_runner.observe_check(
						result.check_id,
						component.root,
						_runner._fixed_check_result(result),
						process_required=True,
					)
				)
			continue
		commands.extend(
			_runner._biome_component_commands(
				component_index, command, paths, manifest.repository.command_timeout_seconds
			)
		)
	with _runner.temporary_directory(actual_root) as temporary_path:
		executed, errors, _ = _runner._execute_commands(
			actual_root,
			_runner._safe_environment(temporary_path),
			commands,
			manifest.repository.command_timeout_seconds,
			deadline,
			manifest=manifest,
		)
	return [*pre_results, *_runner._command_results(executed, errors, {})]


def _web_check_results(
	actual_root: _runner.Path,
	manifest: _runner.Manifest,
	prepared: _runner.PreparedEnvironment,
	results: list[_runner.CheckResult],
	*,
	deadline: float,
) -> list[_runner.CheckResult]:
	try:
		return _runner._run_web_checks(actual_root, manifest, prepared, deadline=deadline)
	except (
		_runner.QualityGateError,
		_runner.DistributionError,
		_runner.RuntimeUnavailable,
		OSError,
	) as error:
		existing_biome_ids = {result.check_id for result in results if ".biome_" in result.check_id}
		fallback: list[_runner.CheckResult] = []
		for component_index in range(1, len(manifest.web) + 1):
			for result in _runner._biome_unchecked_results(
				component_index,
				_runner.redact(str(error)),
				recovery_action="restore the pinned Biome policy and retry the quality gate",
			):
				if result.check_id not in existing_biome_ids:
					fallback.append(
						_runner.observe_check(
							result.check_id,
							manifest.web[component_index - 1].root,
							_runner._fixed_check_result(result),
							process_required=True,
						)
					)
					existing_biome_ids.add(result.check_id)
		return fallback
