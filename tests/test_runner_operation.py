"""Operation deadline, telemetry and native cache runner contracts."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from runner_operation_fixtures import (
	isolate_runner_unit_tests_from_host_gate_slots as isolate_runner_unit_tests_from_host_gate_slots,
	use_long_windows_staging_limit_for_gate_temp_workspace as use_long_windows_staging_limit_for_gate_temp_workspace,
)
from test_runner import FIXTURES, _fake_candidate_snapshot

from quality_gate import native_cache as native_cache_module, runner
from quality_gate import operation as operation_module
from quality_gate.distribution import ExternalTool, ReleaseManifest
from quality_gate.launcher import PreparedEnvironment
from quality_gate.operation import GateOperation
from quality_gate.runtime import RuntimeIdentity

EXPECTED_NATIVE_CACHE_EXECUTIONS = 2


def test_deptry_timing_scope_uses_repository_relative_dependency_paths(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	component = runner.PythonComponent(
		path=tmp_path / "quality_gate",
		tests=None,
		requirements=None,
		typecheck=True,
		dependency_inputs=(tmp_path / "pyproject.toml", tmp_path / "requirements.in"),
	)
	observed: dict[str, str] = {}

	def capture(
		check_id: str,
		scope: str,
		_check: object,
		*,
		process_required: bool = False,
	) -> runner.CheckResult:
		observed["check_id"] = check_id
		observed["scope"] = scope
		assert process_required
		return runner.CheckResult(
			check_id,
			runner.Status.NOT_APPLICABLE,
			"captured telemetry scope",
			recovery_action="none required for this fixture",
		)

	monkeypatch.setattr(runner, "observe_check", capture)
	result = runner._dependency_hygiene_result(
		tmp_path,
		runner.load_manifest(FIXTURES / "valid"),
		component,
		cast(PreparedEnvironment, SimpleNamespace()),
		1,
		None,
		tmp_path / "deptry.json",
		time.monotonic() + 30,
	)

	assert result.check_id == "python.component_1.deptry"
	assert observed == {
		"check_id": "python.component_1.deptry",
		"scope": "quality_gate; dependencies=pyproject.toml,requirements.in",
	}


def test_unmeasured_deptry_scope_uses_repository_relative_dependency_paths() -> None:
	root = FIXTURES / "valid"
	results = runner._OperationResults(root)
	manifest = runner.load_manifest(root)
	component = replace(manifest.python[0], dependency_inputs=("pyproject.toml",))
	results.manifest = replace(manifest, python=(component,))

	assert results.scope_for("python.component_1.deptry") == ("app; dependencies=pyproject.toml")


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
	report = runner.render(gate.verdict(), profile=True)
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
