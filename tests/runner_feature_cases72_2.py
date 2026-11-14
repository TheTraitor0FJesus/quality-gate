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


def test_snapshot_uses_operation_deadline_cleanup_and_candidate_identity(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	observed: dict[str, object] = {}
	index_digest = b"\xab" * 32
	passed = runner.Verdict(
		(runner.CheckResult("operation.fixture", runner.Status.PASSED, "check completed"),)
	)

	def snapshot(root: Path, **kwargs: object) -> AbstractContextManager[SimpleNamespace]:
		operation = runner.current_operation()
		assert operation is not None
		observed.update(kwargs)
		observed["operation_deadline"] = operation.deadline
		observed["operation_cleanup"] = operation.register_cleanup
		return nullcontext(
			SimpleNamespace(
				root=root,
				repository_index=root / "index",
				index_identity=f"git-index-sha256:{index_digest.hex()}",
			)
		)

	monkeypatch.setattr(runner, "gate_admission", lambda: nullcontext())
	monkeypatch.setattr(runner, "temporary_workspace", lambda *_args, **_kwargs: nullcontext())
	monkeypatch.setattr(
		runner,
		"_configure_operation_budget",
		lambda _root, operation: operation.configure_budget(30, 17),
	)
	monkeypatch.setattr(runner, "candidate_snapshot", snapshot)
	monkeypatch.setattr(runner, "_check_snapshot", lambda *_args, **_kwargs: passed)

	verdict = runner._run_snapshot(tmp_path)

	assert observed["timeout_seconds"] == 17
	assert observed["deadline"] == observed["operation_deadline"]
	assert observed["register_cleanup"] == observed["operation_cleanup"]
	assert verdict.operation is not None
	assert verdict.operation.candidate == f"git-index-sha256:{index_digest.hex()}"
	assert any(stage.stage == "candidate.snapshot" for stage in verdict.operation.stages)


@pytest.mark.parametrize("snapshot_seconds", [2, 5])
def test_review_snapshot_manifest_replaces_preliminary_budget(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_seconds: int
) -> None:
	manifest = (FIXTURES / "no-python" / "quality-gate.toml").read_text(encoding="utf-8")
	manifest = manifest.replace(
		"gate_timeout_seconds = 600", f"gate_timeout_seconds = {snapshot_seconds}"
	)
	manifest = manifest.replace("command_timeout_seconds = 120", "command_timeout_seconds = 7")
	(tmp_path / "quality-gate.toml").write_text(manifest, encoding="utf-8")
	now = [100.0]
	monkeypatch.setattr(operation_module.time, "monotonic", lambda: now[0])
	gate = GateOperation(tmp_path, "check", cleanup_seconds=0)
	gate.configure_budget(600, 120)
	results = runner._OperationResults(tmp_path)
	completed = False
	with gate:
		now[0] = 103.0
		assert runner._load_snapshot_manifest(tmp_path, results) is not None
		completed = True
	assert gate.deadline == 100.0 + snapshot_seconds
	assert gate.command_timeout_seconds == 7
	assert gate.timed_out == (snapshot_seconds == 2)
	assert completed == (snapshot_seconds == 5)


def test_ruff_children_receive_a_bounded_worker_environment(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.delenv(runner.RUFF_THREADS_ENV, raising=False)
	actual_run = runner.run
	observed: list[str] = []

	def inspect_child_environment(
		_command: list[str],
		root: Path,
		environment: dict[str, str],
		*,
		timeout: float | None = None,
	) -> str:
		assert timeout is not None and timeout > 0
		output = actual_run(
			[
				sys.executable,
				"-c",
				"import os; print(os.environ.get('RAYON_NUM_THREADS', 'unset'))",
			],
			root,
			environment,
			timeout=timeout,
		)
		observed.append(output.strip())
		return output

	monkeypatch.setattr(runner, "run", inspect_child_environment)
	base_environment = runner._safe_environment("quality-gate-temporary")
	deadline = time.monotonic() + 30
	executed, errors, _outputs = runner._execute_commands(
		tmp_path,
		base_environment,
		[
			("python.component_1.ruff", ["ruff", "check"], 10),
			("python.component_1.format", ["ruff", "format"], 10),
			("python.component_1.mypy", ["mypy"], 10),
		],
		10,
		deadline,
	)

	assert executed == [
		"python.component_1.ruff",
		"python.component_1.format",
		"python.component_1.mypy",
	]
	assert errors == []
	assert observed == ["4", "4", "unset"]


@pytest.mark.parametrize("storage,reason", [("persistent", None), ("private", "lock-busy")])
def test_operation_cache_selection_is_reported_without_skipping_tool(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str, reason: str | None
) -> None:
	now = [100.0]
	monkeypatch.setattr(operation_module.time, "monotonic", lambda: now[0])
	selection = native_cache_module.NativeToolCacheSelection(
		"a" * 64, tmp_path / "cache", storage, reason, 12.5
	)
	monkeypatch.setattr(runner, "_native_cache_identity", lambda *_args: object())
	monkeypatch.setattr(
		runner,
		"NativeToolCache",
		lambda _root: SimpleNamespace(lease=lambda *_args, **_kwargs: nullcontext(selection)),
	)
	monkeypatch.setattr(
		runner,
		"native_tool_source_workspace",
		lambda item, root, _private, **_kwargs: nullcontext((item, root)),
	)
	gate = GateOperation(tmp_path, "check", timeout_seconds=1)
	calls: list[list[str]] = []

	def execute(command: list[str], *_args: object, **_kwargs: object) -> str:
		calls.append(command)
		gate._mark_process_started()
		now[0] = 100.025
		return ""

	monkeypatch.setattr(runner, "run", execute)
	with gate:
		runner._execute_commands(
			tmp_path,
			{"MYPY_CACHE_DIR": str(tmp_path / "private")},
			[("python.component_1.mypy", [sys.executable, "-m", "mypy", "app"], 1)],
			1,
			gate.deadline,
			prepared=SimpleNamespace(cache_root=tmp_path / "provider"),
			component=runner.PythonComponent(tmp_path / "app", None, None, True),
			component_index=1,
		)
	result = next(item for item in gate.verdict().results if item.check_id.endswith(".mypy"))
	assert len(calls) == 1
	assert result.status is runner.Status.PASSED
	assert result.telemetry is not None
	assert result.telemetry.disposition == "executed"
	assert result.telemetry.lookup_ms == 12
	assert result.telemetry.tool_cache == f"native-{storage}"
	assert result.telemetry.reason == (reason or "")
	report = runner.render(gate.verdict())
	assert "lookup_ms=12" in report and f"tool_cache=native-{storage}" in report


def test_native_cache_selection_reuses_snapshot_independent_path_and_still_runs_tool(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	policy_root = tmp_path / "policy"
	configuration = policy_root / "quality_gate" / "policy" / "ruff.toml"
	configuration.parent.mkdir(parents=True)
	configuration.write_text('[lint]\nselect = ["F"]\n', encoding="utf-8")
	prepared = SimpleNamespace(
		policy_root=policy_root,
		cache_root=tmp_path / "provider-cache",
		release_manifest=ReleaseManifest(
			"v4.2.0",
			(),
			(ExternalTool("ruff", "0.16.4", "tools/ruff.exe", "a" * 64),),
		),
		runtime_identities=(
			RuntimeIdentity(
				repository="C:/repositories/example",
				policy_release="v4.2.0",
				component="python",
				python_version="3.11",
				component_contract={"path": "src"},
				dependency_inputs=(),
			),
		),
	)
	observed: list[tuple[Path, str]] = []
	snapshot_roots: list[Path] = []

	def inspect_invocation(
		_command: list[str],
		root: Path,
		environment: dict[str, str],
		*,
		timeout: float | None = None,
	) -> str:
		assert timeout is not None and timeout > 0
		observed.append((root, environment["RUFF_CACHE_DIR"]))
		return ""

	monkeypatch.setattr(runner, "run", inspect_invocation)
	for snapshot_name in ("snapshot-one", "snapshot-two"):
		snapshot_root = tmp_path / snapshot_name
		snapshot_roots.append(snapshot_root)
		source_root = snapshot_root / "src"
		source_root.mkdir(parents=True)
		(source_root / "module.py").write_text("def check() -> None: ...\n", encoding="utf-8")
		component = runner.PythonComponent(snapshot_root / "src", None, None, True)
		private_cache = tmp_path / snapshot_name / "private-cache"
		commands = [
			(
				"python.component_1.ruff",
				["ruff", "check", "--config", str(configuration), "src"],
				10,
			)
		]
		executed, errors, _outputs = runner._execute_commands(
			snapshot_root,
			runner._safe_environment(str(private_cache)),
			commands,
			10,
			time.monotonic() + 30,
			prepared=prepared,
			component=component,
			component_index=1,
		)
		assert executed == ["python.component_1.ruff"]
		assert errors == []

	assert len(observed) == EXPECTED_NATIVE_CACHE_EXECUTIONS
	assert snapshot_roots[0] != snapshot_roots[1]
	assert observed[0][0] == observed[1][0]
	assert observed[0][0] == Path(observed[0][1]) / "_s" / "c"
	assert observed[0][1] == observed[1][1]
	assert observed[0][1].startswith(str(prepared.cache_root))


def test_native_cache_selection_reuses_namespace_across_git_worktrees(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	repository = tmp_path / "repository"
	second_worktree = tmp_path / "second-worktree"
	empty_hooks = tmp_path / "empty-hooks"
	empty_hooks.mkdir()
	subprocess.run(
		["git", "-c", f"core.hooksPath={empty_hooks}", "init", "--quiet", str(repository)],
		check=True,
		capture_output=True,
		text=True,
		timeout=10,
	)

	def run_git(*arguments: str) -> None:
		subprocess.run(
			[
				"git",
				"-C",
				str(repository),
				"-c",
				f"core.hooksPath={empty_hooks}",
				*arguments,
			],
			check=True,
			capture_output=True,
			text=True,
			timeout=10,
		)

	run_git("config", "user.name", "Quality Gate Test")
	run_git("config", "user.email", "quality-gate-test@example.invalid")
	run_git("config", "commit.gpgsign", "false")
	source_file = repository / "src" / "module.py"
	source_file.parent.mkdir()
	source_file.write_text("def check() -> None: ...\n", encoding="utf-8")
	run_git("add", "src/module.py")
	run_git("commit", "--quiet", "-m", "test fixture")
	run_git("worktree", "add", "--quiet", "--detach", str(second_worktree), "HEAD")

	policy_root = tmp_path / "policy"
	configuration = policy_root / "quality_gate" / "policy" / "ruff.toml"
	configuration.parent.mkdir(parents=True)
	configuration.write_text('[lint]\nselect = ["F"]\n', encoding="utf-8")
	shared_cache_root = tmp_path / "provider-cache"
	persistent_started = threading.Event()
	private_started = threading.Event()
	observed: list[tuple[Path, str, str]] = []

	def inspect_invocation(
		_command: list[str],
		root: Path,
		environment: dict[str, str],
		*,
		timeout: float | None = None,
	) -> str:
		assert timeout is not None and timeout > 0
		cache_path = Path(environment["RUFF_CACHE_DIR"])
		if cache_path.is_relative_to(shared_cache_root.resolve()):
			storage = "persistent"
			persistent_started.set()
			assert private_started.wait(timeout=10)
		else:
			storage = "private"
			assert persistent_started.wait(timeout=10)
			private_started.set()
		assert (root / "src" / "module.py").read_text(encoding="utf-8") == (
			"def check() -> None: ...\n"
		)
		observed.append((root, str(cache_path), storage))
		return ""

	monkeypatch.setattr(runner, "run", inspect_invocation)
	worktrees = (repository, second_worktree)
	identities: list[str] = []
	snapshot_roots: list[Path] = []

	def execute_from_worktree(worktree: Path) -> None:
		from quality_gate.snapshot import candidate_snapshot

		identity = RuntimeIdentity(
			repository=str(worktree),
			policy_release="v4.2.0",
			component="python",
			python_version="3.11",
			component_contract={"path": "src"},
			dependency_inputs=(),
		)
		prepared = SimpleNamespace(
			policy_root=policy_root,
			cache_root=shared_cache_root,
			release_manifest=ReleaseManifest(
				"v4.2.0",
				(),
				(ExternalTool("ruff", "0.16.4", "tools/ruff.exe", "a" * 64),),
			),
			runtime_identities=(identity,),
		)
		with candidate_snapshot(worktree) as snapshot:
			root = snapshot.root
			component = runner.PythonComponent(root / "src", None, None, True)
			command = ["ruff", "check", "--config", str(configuration), "src"]
			cache_identity = runner._native_cache_identity(
				root, "python.component_1.ruff", command, prepared, component, 1
			)
			assert cache_identity is not None
			identities.append(cache_identity.namespace_id)
			snapshot_roots.append(root)
			executed, errors, _outputs = runner._execute_commands(
				root,
				runner._safe_environment(str(tmp_path / f"private-{worktree.name}")),
				[("python.component_1.ruff", command, 10)],
				10,
				time.monotonic() + 30,
				prepared=prepared,
				component=component,
				component_index=1,
			)
			assert executed == ["python.component_1.ruff"]
			assert errors == []

	with ThreadPoolExecutor(max_workers=2) as executor:
		first = executor.submit(execute_from_worktree, worktrees[0])
		assert persistent_started.wait(timeout=10)
		second = executor.submit(execute_from_worktree, worktrees[1])
		first.result(timeout=30)
		second.result(timeout=30)

	assert len(observed) == 2
	assert len(set(identities)) == 1
	persistent = next(item for item in observed if item[2] == "persistent")
	private = next(item for item in observed if item[2] == "private")
	assert persistent[0] != private[0]
	assert persistent[0].is_relative_to(shared_cache_root.resolve())
	assert private[0] in snapshot_roots
	assert private[1] == str(tmp_path / f"private-{second_worktree.name}")


def test_ruff_worker_override_is_validated_and_applied_only_to_ruff(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv(runner.RUFF_THREADS_ENV, "8")
	assert runner._ruff_environment({}) == {"RAYON_NUM_THREADS": "8"}
	for invalid in ("0", "65", "-1", "many"):
		monkeypatch.setenv(runner.RUFF_THREADS_ENV, invalid)
		with pytest.raises(runner.QualityGateError, match=runner.RUFF_THREADS_ENV):
			runner._ruff_environment({})


def test_missing_external_tool_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
	root = FIXTURES / "valid"
	missing_tool = root / "missing-ruff"
	prepared = SimpleNamespace(
		policy_root=runner.POLICY_DIR.parent.parent,
		release_manifest=ReleaseManifest(
			"v2.0.0",
			(ReleaseFile("quality_gate.whl", "a" * 64),),
			(ExternalTool("ruff", "0.15.12", missing_tool.name, "b" * 64),),
		),
		runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
	)

	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)

	verdict = runner.check(root)

	ruff = next(
		result for result in verdict.results if result.check_id == "python.component_1.ruff"
	)
	assert ruff.status is runner.Status.UNCHECKED
	assert verdict.exit_code == runner.EXIT_UNCHECKED
