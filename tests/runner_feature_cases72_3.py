"""Feature-preserving test partition."""

from __future__ import annotations

from runner_feature_support72 import (
	EXPECTED_BIOME_INVOCATIONS as EXPECTED_BIOME_INVOCATIONS,
)
from runner_feature_support72 import (
	EXPECTED_NATIVE_CACHE_EXECUTIONS as EXPECTED_NATIVE_CACHE_EXECUTIONS,
)
from runner_feature_support72 import (
	EXPECTED_TIMEOUT_WAIT_CALLS as EXPECTED_TIMEOUT_WAIT_CALLS,
)
from runner_feature_support72 import (
	FIXTURES as FIXTURES,
)
from runner_feature_support72 import (
	UNCHECKED_EXIT as UNCHECKED_EXIT,
)
from runner_feature_support72 import (
	AbstractContextManager as AbstractContextManager,
)
from runner_feature_support72 import (
	AdmissionError as AdmissionError,
)
from runner_feature_support72 import (
	ExternalTool as ExternalTool,
)
from runner_feature_support72 import (
	GateOperation as GateOperation,
)
from runner_feature_support72 import (
	Path as Path,
)
from runner_feature_support72 import (
	PreparedEnvironment as PreparedEnvironment,
)
from runner_feature_support72 import (
	ReleaseFile as ReleaseFile,
)
from runner_feature_support72 import (
	ReleaseManifest as ReleaseManifest,
)
from runner_feature_support72 import (
	RuntimeIdentity as RuntimeIdentity,
)
from runner_feature_support72 import (
	SimpleNamespace as SimpleNamespace,
)
from runner_feature_support72 import (
	ThreadPoolExecutor as ThreadPoolExecutor,
)
from runner_feature_support72 import (
	_assert_biome_invocations as _assert_biome_invocations,
)
from runner_feature_support72 import (
	_fake_biome_command as _fake_biome_command,
)
from runner_feature_support72 import (
	_fake_candidate_snapshot as _fake_candidate_snapshot,
)
from runner_feature_support72 import (
	_web_fixture as _web_fixture,
)
from runner_feature_support72 import (
	_web_manifest as _web_manifest,
)
from runner_feature_support72 import (
	_web_policy as _web_policy,
)
from runner_feature_support72 import (
	_web_prepared as _web_prepared,
)
from runner_feature_support72 import (
	cast as cast,
)
from runner_feature_support72 import (
	hashlib as hashlib,
)
from runner_feature_support72 import (
	io as io,
)
from runner_feature_support72 import (
	isolate_runner_unit_tests_from_host_gate_slots as isolate_runner_unit_tests_from_host_gate_slots,
)
from runner_feature_support72 import (
	json as json,
)
from runner_feature_support72 import (
	native_cache_module as native_cache_module,
)
from runner_feature_support72 import (
	nullcontext as nullcontext,
)
from runner_feature_support72 import (
	operation_module as operation_module,
)
from runner_feature_support72 import (
	os as os,
)
from runner_feature_support72 import (
	pytest as pytest,
)
from runner_feature_support72 import (
	replace as replace,
)
from runner_feature_support72 import (
	runner as runner,
)
from runner_feature_support72 import (
	shutil as shutil,
)
from runner_feature_support72 import (
	subprocess as subprocess,
)
from runner_feature_support72 import (
	sys as sys,
)
from runner_feature_support72 import (
	threading as threading,
)
from runner_feature_support72 import (
	time as time,
)
from runner_feature_support72 import (
	tomllib as tomllib,
)
from runner_feature_support72 import (
	use_long_windows_staging_limit_for_gate_temp_workspace as use_long_windows_staging_limit_for_gate_temp_workspace,
)


def test_format_uses_only_explicit_paths(monkeypatch: pytest.MonkeyPatch) -> None:
	root = FIXTURES / "valid"
	calls: list[list[str]] = []
	environments: list[dict[str, str]] = []
	monkeypatch.setenv(runner.RUFF_THREADS_ENV, "8")
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	def record_run(
		command: list[str], root: Path, environment: dict[str, str], *, timeout: float
	) -> None:
		calls.append(command)
		environments.append(environment)

	monkeypatch.setattr(runner, "run", record_run)

	runner.format_paths(root, ("app",))

	assert len(calls) == 1
	assert calls[0][calls[0].index("format") + 1] == "--config"
	assert calls[0][-1] == "app"
	assert "--check" not in calls[0]
	assert "RAYON_NUM_THREADS" not in environments[0]


def test_policy_access_error_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
	root = FIXTURES / "valid"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)

	def fail_prepare(*args: object, **kwargs: object) -> None:
		raise PermissionError("cache is inaccessible")

	monkeypatch.setattr(
		runner,
		"prepare",
		fail_prepare,
	)

	verdict = runner.check(root)

	policy = next(result for result in verdict.results if result.check_id == "runtime.policy")
	assert policy.status is runner.Status.UNCHECKED
	documents = next(
		result for result in verdict.results if result.check_id == "manifest.documents"
	)
	assert documents.status is runner.Status.PASSED
	python_checks = [
		result for result in verdict.results if result.check_id.startswith("python.component_")
	]
	assert python_checks
	assert all(result.status is runner.Status.UNCHECKED for result in python_checks)
	assert all(
		result.telemetry is not None and result.telemetry.disposition == "not-run"
		for result in python_checks
	)
	assert verdict.exit_code == runner.EXIT_UNCHECKED


def test_workspace_failure_reports_only_the_unavailable_workspace(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "valid"

	def fail_workspace(_root: Path, **_kwargs: object) -> object:
		raise runner.TemporaryWorkspaceError("repository temp directory is unavailable")

	monkeypatch.setattr(runner, "temporary_workspace", fail_workspace)

	verdict = runner._run_snapshot(root)

	assert verdict.exit_code == runner.EXIT_UNCHECKED
	assert [result.check_id for result in verdict.results] == ["temporary.workspace"]
	assert verdict.results[0].status is runner.Status.UNCHECKED


def test_not_applicable_component_tests_are_reported(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "valid"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(runner, "run", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	verdict = runner.check(root)

	tests = next(
		result for result in verdict.results if result.check_id == "python.component_1.pytest"
	)
	assert tests.status is runner.Status.NOT_APPLICABLE


def test_pytest_collection_failure_is_failed(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "python-tests"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(
		runner,
		"temporary_directory",
		lambda root: nullcontext("test-temporary-directory"),
	)
	seen_commands: list[list[str]] = []

	def fail_collection(
		command: list[str], root: Path, environment: dict[str, str], *, timeout: float
	) -> None:
		seen_commands.append(command)
		if "pytest" in command:
			raise runner.QualityGateError(
				"ERROR collecting tests/test_app.py",
				exit_code=runner.EXIT_UNCHECKED,
				recovery_action="fix collection",
			)

	monkeypatch.setattr(runner, "run", fail_collection)

	verdict = runner.check(root)

	pytest_result = next(
		result for result in verdict.results if result.check_id == "python.component_1.pytest"
	)
	assert pytest_result.status is runner.Status.UNCHECKED
	assert [
		result.check_id
		for result in verdict.results
		if result.check_id == "python.component_1.pytest"
	] == ["python.component_1.pytest"]
	pytest_command = next(command for command in seen_commands if "pytest" in command)
	assert "tests" in pytest_command
	assert any(path.replace("\\", "/") == "tests/extra" for path in pytest_command)


def test_missing_test_path_is_unchecked_without_hiding_other_checks(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "missing-test-path"
	monkeypatch.setattr(runner, "gate_admission", lambda: nullcontext())
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(runner, "run", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	verdict = runner.check(root)

	missing = next(
		result for result in verdict.results if result.check_id == "python.component_1.test_path_1"
	)
	assert missing.status is runner.Status.UNCHECKED
	assert missing.telemetry is not None
	assert missing.telemetry.scope == "missing-tests"
	assert missing.telemetry.disposition == "executed"
	assert any(result.check_id == "python.component_1.ruff" for result in verdict.results)


def test_missing_component_path_marks_dependent_checks_not_run(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	root = tmp_path
	manifest = (FIXTURES / "valid" / "quality-gate.toml").read_text(encoding="utf-8")
	manifest = manifest.replace('policy_release = "v2.0.1"', 'policy_release = "v4.2.0"')
	manifest = manifest.replace('path = "app"', 'path = "missing-app"')
	manifest = manifest.replace(
		'test_paths = []\ntests_applicable = false\ntests_reason = "Fixture validates manifest loading without running tests."',
		'test_paths = ["tests"]\ntests_applicable = true\ntypecheck = true',
	)
	(root / "quality-gate.toml").write_text(manifest, encoding="utf-8")
	(root / "tests").mkdir()
	policy_root = runner.POLICY_DIR.parent.parent

	def passed(check_id: str) -> runner.CheckResult:
		return runner.CheckResult(check_id, runner.Status.PASSED, "check passed")

	monkeypatch.setattr(runner, "gate_admission", lambda: nullcontext())
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "git_integrity_results", lambda *_args, **_kwargs: [])
	monkeypatch.setattr(
		runner,
		"workflow_result",
		lambda *_args, **_kwargs: passed("repository.workflow"),
	)
	monkeypatch.setattr(runner, "documentation_results", lambda *_args, **_kwargs: [])
	monkeypatch.setattr(
		runner, "lessons_result", lambda *_args, **_kwargs: passed("lessons.learning")
	)
	monkeypatch.setattr(runner, "web_budget_results", lambda *_args, **_kwargs: ())
	monkeypatch.setattr(
		runner,
		"secret_candidate_result",
		lambda *_args, **_kwargs: passed("secrets.candidate"),
	)
	monkeypatch.setattr(
		runner,
		"secret_history_result",
		lambda *_args, **_kwargs: passed("secrets.history"),
	)
	monkeypatch.setattr(runner, "_ci_history_refs", lambda *_args, **_kwargs: (None, None))
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *_args, **_kwargs: SimpleNamespace(policy_root=policy_root, runtimes=()),
	)
	monkeypatch.setattr(runner, "_has_pinned_coverage", lambda _prepared: False)
	monkeypatch.setattr(
		runner,
		"temporary_directory",
		lambda *_args, **_kwargs: nullcontext("test-temporary-directory"),
	)

	verdict = runner.check(root)
	results = {result.check_id: result for result in verdict.results}

	missing_component = results["python.component_1.path"]
	assert missing_component.status is runner.Status.UNCHECKED
	assert missing_component.telemetry is not None
	assert missing_component.telemetry.scope == "missing-app"
	for suffix in ("mypy", "pytest"):
		result = results[f"python.component_1.{suffix}"]
		assert result.status is runner.Status.UNCHECKED
		assert result.telemetry is not None
		assert result.telemetry.disposition == "not-run"
		assert result.telemetry.reason == "declared component path is unavailable"


def test_component_runtime_failure_does_not_hide_other_components(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "multi-component"
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(
				SimpleNamespace(python=None, current=False, reason="runtime is missing"),
				SimpleNamespace(python=Path(sys.executable), current=True),
			),
		),
	)
	monkeypatch.setattr(runner, "run", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		runner, "temporary_directory", lambda root: nullcontext("test-temporary-directory")
	)

	verdict = runner.check(root)

	assert any(
		result.check_id == "python.component_1.runtime" and result.status is runner.Status.UNCHECKED
		for result in verdict.results
	)
	assert any(
		result.check_id == "python.component_2.ruff" and result.status is runner.Status.PASSED
		for result in verdict.results
	)


def test_check_runs_read_only_biome_lint_and_format_for_declared_assets(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	prepared = _web_prepared(policy_root)
	argv_log = _fake_biome_command(tmp_path, monkeypatch)
	(tmp_path / "assets/other/undeclared.js").parent.mkdir(parents=True, exist_ok=True)
	(tmp_path / "assets/other/undeclared.js").write_text("const unused = 1;\n", encoding="utf-8")
	before = {
		relative: (tmp_path / relative).read_bytes()
		for relative in ("assets/js/app.js", "assets/css/app.css", "assets/other/undeclared.js")
	}

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)

	verdict = runner.check(tmp_path)

	assert [result.check_id for result in verdict.results if ".biome_" in result.check_id] == [
		"web.component_1.biome_lint",
		"web.component_1.biome_format",
	]
	assert all(
		result.status is runner.Status.PASSED
		for result in verdict.results
		if ".biome_" in result.check_id
	)
	assert {relative: (tmp_path / relative).read_bytes() for relative in before} == before
	_assert_biome_invocations(argv_log, {"assets/js/app.js", "assets/css/app.css"})


@pytest.mark.parametrize(
	("javascript", "css"),
	((True, False), (False, True)),
)
def test_check_runs_biome_for_components_with_one_declared_web_language(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	javascript: bool,
	css: bool,
) -> None:
	policy_root = _web_fixture(tmp_path, javascript=javascript, css=css)
	argv_log = _fake_biome_command(tmp_path, monkeypatch)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.PASSED] * 2
	expected_assets = {"assets/js/app.js"} if javascript else {"assets/css/app.css"}
	_assert_biome_invocations(argv_log, expected_assets)


def test_biome_policy_is_version_locked_to_stable_raw_asset_checks() -> None:
	config = json.loads((runner.POLICY_DIR / "biome.json").read_text(encoding="utf-8"))
	inventory = tomllib.loads((runner.POLICY_DIR / "biome.toml").read_text(encoding="utf-8"))

	assert config["$schema"].endswith("/2.2.6/schema.json")
	assert config["linter"] == {"enabled": True, "rules": {"recommended": True}}
	assert config["assist"] == {"enabled": False}
	assert config["css"] == {"formatter": {"enabled": True}}
	assert config["files"] == {"maxSize": 5 * 1024 * 1024}
	assert inventory["biome"] == {
		"version": "2.2.6",
		"platforms": {
			"linux-x64": {
				"path": "biome-linux-x64",
				"url": "https://github.com/biomejs/biome/releases/download/%40biomejs/biome%402.2.6/biome-linux-x64",
				"sha256": "ad989034fff59c9b4e45a55d0d71cbfbd5bc673cfb7193ee01268773e1eb84a7",
			},
			"windows-x64": {
				"path": "biome-win32-x64.exe",
				"url": "https://github.com/biomejs/biome/releases/download/%40biomejs/biome%402.2.6/biome-win32-x64.exe",
				"sha256": "157039ed9aa4817595be54c7d929acab354a3284fbd05e78218e69fa5773e92b",
			},
		},
	}


def test_check_marks_biome_unchecked_when_asset_exceeds_policy_file_limit(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	prepared = _web_prepared(policy_root)
	(tmp_path / "assets/js/app.js").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2
