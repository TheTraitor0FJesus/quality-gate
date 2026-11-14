"""Feature-preserving test partition."""

from __future__ import annotations

from native_feature_support72 import (
	MEASUREMENT_EXPECTED_TOOL_RUNS as MEASUREMENT_EXPECTED_TOOL_RUNS,
)
from native_feature_support72 import (
	MEASUREMENT_SOURCE_MTIME_NS as MEASUREMENT_SOURCE_MTIME_NS,
)
from native_feature_support72 import (
	MEASUREMENT_WARM_RUNS as MEASUREMENT_WARM_RUNS,
)
from native_feature_support72 import (
	Any as Any,
)
from native_feature_support72 import (
	BinaryIO as BinaryIO,
)
from native_feature_support72 import (
	Callable as Callable,
)
from native_feature_support72 import (
	CheckResult as CheckResult,
)
from native_feature_support72 import (
	EditError as EditError,
)
from native_feature_support72 import (
	GateOperation as GateOperation,
)
from native_feature_support72 import (
	Literal as Literal,
)
from native_feature_support72 import (
	NativeToolCache as NativeToolCache,
)
from native_feature_support72 import (
	NativeToolCacheIdentity as NativeToolCacheIdentity,
)
from native_feature_support72 import (
	NativeToolCacheSelection as NativeToolCacheSelection,
)
from native_feature_support72 import (
	OperationTimeoutError as OperationTimeoutError,
)
from native_feature_support72 import (
	Path as Path,
)
from native_feature_support72 import (
	PreparedEnvironment as PreparedEnvironment,
)
from native_feature_support72 import (
	SimpleNamespace as SimpleNamespace,
)
from native_feature_support72 import (
	Status as Status,
)
from native_feature_support72 import (
	_assert_native_cache_measurements as _assert_native_cache_measurements,
)
from native_feature_support72 import (
	_assert_persistent_mypy_run as _assert_persistent_mypy_run,
)
from native_feature_support72 import (
	_close_file_handle as _close_file_handle,
)
from native_feature_support72 import (
	_collect_native_cache_measurements as _collect_native_cache_measurements,
)
from native_feature_support72 import (
	_execute_measured_snapshot as _execute_measured_snapshot,
)
from native_feature_support72 import (
	_identity as _identity,
)
from native_feature_support72 import (
	_mypy_tool_command_templates as _mypy_tool_command_templates,
)
from native_feature_support72 import (
	_native_tool_command_templates as _native_tool_command_templates,
)
from native_feature_support72 import (
	_NativeToolRunRecorder as _NativeToolRunRecorder,
)
from native_feature_support72 import (
	_open_file_without_shared_reads as _open_file_without_shared_reads,
)
from native_feature_support72 import (
	_prepared_with_fixture_dependencies as _prepared_with_fixture_dependencies,
)
from native_feature_support72 import (
	_tool_finding_text as _tool_finding_text,
)
from native_feature_support72 import (
	_write_native_cache_measurement_report as _write_native_cache_measurement_report,
)
from native_feature_support72 import (
	asdict as asdict,
)
from native_feature_support72 import (
	cast as cast,
)
from native_feature_support72 import (
	hashlib as hashlib,
)
from native_feature_support72 import (
	json as json,
)
from native_feature_support72 import (
	native_cache_module as native_cache_module,
)
from native_feature_support72 import (
	native_tool_source_workspace as native_tool_source_workspace,
)
from native_feature_support72 import (
	observe_check as observe_check,
)
from native_feature_support72 import (
	open_locked_file as open_locked_file,
)
from native_feature_support72 import (
	operation_module as operation_module,
)
from native_feature_support72 import (
	os as os,
)
from native_feature_support72 import (
	platform as platform,
)
from native_feature_support72 import (
	prepare as prepare,
)
from native_feature_support72 import (
	pytest as pytest,
)
from native_feature_support72 import (
	replace as replace,
)
from native_feature_support72 import (
	runner as runner,
)
from native_feature_support72 import (
	shutil as shutil,
)
from native_feature_support72 import (
	stat as stat,
)
from native_feature_support72 import (
	subprocess as subprocess,
)
from native_feature_support72 import (
	sys as sys,
)
from native_feature_support72 import (
	time as time,
)
from native_feature_support72 import (
	use_long_windows_staging_limit_for_test_sandbox as use_long_windows_staging_limit_for_test_sandbox,
)


def test_pinned_native_tools_reuse_native_entries_across_snapshots(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Prove warm native entries are read while both tools execute again."""
	if os.environ.get("QUALITY_GATE_NATIVE_CACHE_INTEGRATION") != "1":
		pytest.skip("set QUALITY_GATE_NATIVE_CACHE_INTEGRATION=1 for pinned-tool evidence")
	if os.name != "nt":
		pytest.skip("Ruff read-block proof uses Windows file-sharing semantics")
	repository_root = Path(__file__).parents[1].resolve()
	prepared = prepare(repository_root, repository_root=repository_root)
	python = prepared.runtimes[0].python
	if python is None:
		pytest.fail("the selected pinned component runtime is not ready")
	prepared = replace(prepared, cache_root=tmp_path / "provider-cache")
	delegate = runner.run

	class _CacheReadProbeRecorder(_NativeToolRunRecorder):
		def __call__(
			self,
			command: list[str],
			root: Path,
			environment: dict[str, str],
			*,
			timeout: float | None = None,
		) -> str:
			if self.current_run == "snapshot-warm" and "RUFF_CACHE_DIR" in environment:
				kernel32, handle = _open_file_without_shared_reads(root / "src" / "module.py")
				try:
					return super().__call__(command, root, environment, timeout=timeout)
				finally:
					_close_file_handle(kernel32, handle)
			return super().__call__(command, root, environment, timeout=timeout)

	base_recorder = _CacheReadProbeRecorder(delegate)
	monkeypatch.setattr(runner, "run", base_recorder)
	commands = tuple(
		(
			check_id,
			[*command[:3], "--verbose", *command[3:]]
			if check_id.endswith(".ruff")
			else [*command[:3], "-v", *command[3:]],
		)
		for check_id, command in _native_tool_command_templates(prepared, python)
	)
	source = "def add(left: int, right: int) -> int:\n\treturn left + right\n"
	cold_findings = _execute_measured_snapshot(
		tmp_path, prepared, base_recorder, commands, "snapshot-cold", source
	)
	warm_findings = _execute_measured_snapshot(
		tmp_path, prepared, base_recorder, commands, "snapshot-warm", source
	)
	assert cold_findings == warm_findings == []
	assert [row["tool"] for row in base_recorder.observed] == [
		"ruff",
		"mypy",
		"ruff",
		"mypy",
	]
	assert base_recorder.observed[0]["namespace_id"] == base_recorder.observed[2]["namespace_id"]
	assert base_recorder.observed[1]["namespace_id"] == base_recorder.observed[3]["namespace_id"]
	assert base_recorder.workspace_roots[0] == base_recorder.workspace_roots[2]
	assert base_recorder.workspace_roots[1] == base_recorder.workspace_roots[3]
	mypy_warm_output = base_recorder.outputs[3].casefold()
	assert "success: no issues found in 2 source files" in mypy_warm_output
	assert "build finished" in mypy_warm_output and "with 0 modules" in mypy_warm_output


def test_pinned_native_cache_invalidation_matches_clean_cache(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Changed imported stubs and policy inputs match fresh native-cache findings."""
	if os.environ.get("QUALITY_GATE_NATIVE_CACHE_INTEGRATION") != "1":
		pytest.skip("set QUALITY_GATE_NATIVE_CACHE_INTEGRATION=1 for pinned-tool evidence")
	repository_root = Path(__file__).parents[1].resolve()
	prepared = prepare(repository_root, repository_root=repository_root)
	python = prepared.runtimes[0].python
	if python is None:
		pytest.fail("the selected pinned component runtime is not ready")
	base_prepared = replace(prepared, cache_root=tmp_path / "provider-cache")
	recorder = _NativeToolRunRecorder(runner.run)
	monkeypatch.setattr(runner, "run", recorder)
	base_commands = _mypy_tool_command_templates(base_prepared, python)
	baseline_source = (
		"from support import VALUE\n\ndef add(value: int) -> int:\n\treturn value + VALUE\n"
	)
	module_source = (
		"from typing import assert_type\n"
		"from support import VALUE\n\n"
		"assert_type(VALUE, int)\n\n"
		"def add(value: int) -> int:\n\treturn value + VALUE\n"
	)
	integer_dependency = {"support.py": "VALUE = 1\n", "support.pyi": "VALUE: int\n"}
	assert not _execute_measured_snapshot(
		tmp_path,
		base_prepared,
		recorder,
		base_commands,
		"default-policy",
		baseline_source,
		integer_dependency,
		package_init=False,
	)
	default_policy_namespace = next(
		str(row["namespace_id"])
		for row in recorder.observed
		if row["run"] == "default-policy" and row["tool"] == "mypy"
	)

	source_policy_dir = base_prepared.policy_root / "quality_gate" / "policy"
	fixture_policy_root = tmp_path / "fixture-policy"
	fixture_policy_dir = fixture_policy_root / "quality_gate" / "policy"
	shutil.copytree(source_policy_dir, fixture_policy_dir)
	mypy_config = fixture_policy_dir / "mypy.ini"
	original_config = mypy_config.read_text(encoding="utf-8")
	assert "follow_imports = skip" in original_config
	assert "ignore_missing_imports = True" in original_config
	fixture_config = original_config.replace("follow_imports = skip", "follow_imports = normal")
	fixture_config = fixture_config.replace(
		"ignore_missing_imports = True", "ignore_missing_imports = False"
	)
	fixture_config += "\nmypy_path = src\nexplicit_package_bases = True\n"
	assert fixture_config != original_config
	assert "follow_imports = normal" in fixture_config
	assert "ignore_missing_imports = False" in fixture_config
	assert "mypy_path = src" in fixture_config
	assert "explicit_package_bases = True" in fixture_config
	mypy_config.write_text(fixture_config, encoding="utf-8")
	prepared = replace(base_prepared, policy_root=fixture_policy_root)
	commands = _mypy_tool_command_templates(prepared, python)
	assert not _execute_measured_snapshot(
		tmp_path,
		prepared,
		recorder,
		commands,
		"stub-cache-cold",
		module_source,
		integer_dependency,
		package_init=False,
	)
	stub_cold_namespace = _assert_persistent_mypy_run(
		recorder, base_prepared.cache_root, "stub-cache-cold"
	)
	string_dependency = {"support.py": "VALUE = 1\n", "support.pyi": "VALUE: str\n"}
	changed_stub_mtime = MEASUREMENT_SOURCE_MTIME_NS + 1_000_000_000
	changed_snapshot_mtimes = {
		"module.py": changed_stub_mtime,
		"support.pyi": changed_stub_mtime,
	}
	changed_stub_errors = _execute_measured_snapshot(
		tmp_path,
		prepared,
		recorder,
		commands,
		"stub-cache-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	assert len(changed_stub_errors) == 1
	diagnostic = changed_stub_errors[0][2].casefold()
	assert "expression is of type" in diagnostic
	assert "int" in diagnostic and "str" in diagnostic
	clean_stub_prepared = replace(prepared, cache_root=tmp_path / "clean-stub-cache")
	clean_stub_errors = _execute_measured_snapshot(
		tmp_path,
		clean_stub_prepared,
		recorder,
		_mypy_tool_command_templates(clean_stub_prepared, python),
		"stub-clean-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	assert changed_stub_errors == clean_stub_errors, (
		f"persistent findings={changed_stub_errors!r}; clean findings={clean_stub_errors!r}"
	)
	assert [error[0] for error in changed_stub_errors] == ["python.component_1.mypy"]
	stub_changed_namespace = _assert_persistent_mypy_run(
		recorder, base_prepared.cache_root, "stub-cache-changed"
	)
	assert stub_changed_namespace == stub_cold_namespace

	base_mypy_namespace = str(
		next(
			row["namespace_id"]
			for row in recorder.observed
			if row["run"] == "stub-cache-cold" and row["tool"] == "mypy"
		)
	)
	assert base_mypy_namespace != default_policy_namespace

	changed_policy_root = tmp_path / "changed-policy"
	changed_policy_dir = changed_policy_root / "quality_gate" / "policy"
	shutil.copytree(fixture_policy_dir, changed_policy_dir)
	mypy_config = changed_policy_dir / "mypy.ini"
	mypy_config.write_text(
		mypy_config.read_text(encoding="utf-8") + "\n# changed native-cache compatibility input\n",
		encoding="utf-8",
	)
	changed_config_prepared = replace(prepared, policy_root=changed_policy_root)
	changed_config_commands = _mypy_tool_command_templates(changed_config_prepared, python)
	changed_config_errors = _execute_measured_snapshot(
		tmp_path,
		changed_config_prepared,
		recorder,
		changed_config_commands,
		"config-cache-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	clean_config_prepared = replace(
		changed_config_prepared, cache_root=tmp_path / "clean-config-cache"
	)
	clean_config_errors = _execute_measured_snapshot(
		tmp_path,
		clean_config_prepared,
		recorder,
		_mypy_tool_command_templates(clean_config_prepared, python),
		"config-clean-changed",
		module_source,
		string_dependency,
		package_init=False,
		mtime_overrides_ns=changed_snapshot_mtimes,
	)
	assert changed_config_errors == clean_config_errors
	assert changed_config_errors == changed_stub_errors

	changed_config_namespace = next(
		row["namespace_id"]
		for row in recorder.observed
		if row["run"] == "config-cache-changed" and row["tool"] == "mypy"
	)
	assert (
		_assert_persistent_mypy_run(recorder, base_prepared.cache_root, "config-cache-changed")
		== changed_config_namespace
	)
	assert changed_config_namespace != base_mypy_namespace

	dependency_source = (
		"from typing import assert_type\nfrom support import VALUE\n\nassert_type(VALUE, int)\n"
	)
	integer_source_dependency = {"support.py": "VALUE = 1\n"}
	dependency_before = _prepared_with_fixture_dependencies(prepared, integer_source_dependency)
	dependency_before_errors = _execute_measured_snapshot(
		tmp_path,
		dependency_before,
		recorder,
		_mypy_tool_command_templates(dependency_before, python),
		"dependency-cache-before",
		dependency_source,
		integer_source_dependency,
		package_init=False,
	)
	assert dependency_before_errors == []
	dependency_before_namespace = _assert_persistent_mypy_run(
		recorder,
		base_prepared.cache_root,
		"dependency-cache-before",
	)

	string_source_dependency = {"support.py": "VALUE = 'text'\n"}
	dependency_after = _prepared_with_fixture_dependencies(prepared, string_source_dependency)
	dependency_after_commands = _mypy_tool_command_templates(dependency_after, python)
	dependency_after_errors = _execute_measured_snapshot(
		tmp_path,
		dependency_after,
		recorder,
		dependency_after_commands,
		"dependency-cache-after",
		dependency_source,
		string_source_dependency,
		package_init=False,
	)
	assert len(dependency_after_errors) == 1
	dependency_diagnostic = dependency_after_errors[0][2].casefold()
	assert (
		"assert_type" in dependency_diagnostic or "expression is of type" in dependency_diagnostic
	)
	assert "int" in dependency_diagnostic and "str" in dependency_diagnostic
	dependency_after_namespace = _assert_persistent_mypy_run(
		recorder,
		base_prepared.cache_root,
		"dependency-cache-after",
	)
	assert dependency_after_namespace != dependency_before_namespace

	clean_dependency_after = replace(
		dependency_after,
		cache_root=tmp_path / "clean-dependency-cache",
	)
	clean_dependency_errors = _execute_measured_snapshot(
		tmp_path,
		clean_dependency_after,
		recorder,
		_mypy_tool_command_templates(clean_dependency_after, python),
		"dependency-clean-after",
		dependency_source,
		string_source_dependency,
		package_init=False,
	)
	assert dependency_after_errors == clean_dependency_errors
