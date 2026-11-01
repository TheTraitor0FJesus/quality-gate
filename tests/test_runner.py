from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from quality_gate import native_cache as native_cache_module, runner
from quality_gate.admission import AdmissionError
from quality_gate.distribution import ExternalTool, ReleaseFile, ReleaseManifest
from quality_gate.launcher import PreparedEnvironment
from quality_gate.runtime import RuntimeIdentity

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_TIMEOUT_WAIT_CALLS = 2
EXPECTED_BIOME_INVOCATIONS = 2
EXPECTED_NATIVE_CACHE_EXECUTIONS = 2
UNCHECKED_EXIT = 2


@pytest.fixture(autouse=True)
def use_long_windows_staging_limit_for_gate_temp_workspace(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name == "nt":
		monkeypatch.setattr(native_cache_module, "_WINDOWS_MAX_PATH", 32767)


def _fake_candidate_snapshot(root: Path) -> AbstractContextManager[SimpleNamespace]:
	return nullcontext(SimpleNamespace(root=root))


def test_load_components_accepts_python_component() -> None:
	root = FIXTURES / "valid"

	components = runner.load_components(root)

	assert components == [runner.PythonComponent(root / "app", None, None, True, (), 300)]


def test_decode_subprocess_output_handles_windows_encoding(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(runner.locale, "getpreferredencoding", lambda _do_setlocale: "cp1251")

	assert runner.decode_subprocess_output("ошибка pip".encode("cp1251")) == "ошибка pip"


def test_wheel_tool_runs_from_the_prepared_runtime() -> None:
	python = Path("runtime") / "Scripts" / "python.exe"
	prepared = SimpleNamespace(
		policy_root=Path("release"),
		release_manifest=ReleaseManifest(
			"v2.0.0",
			(ReleaseFile("quality_gate.whl", "a" * 64),),
			(ExternalTool("mypy", "1.19.1", "mypy.whl", "b" * 64),),
		),
	)

	assert runner._tool_command(cast(PreparedEnvironment, prepared), python, "mypy") == [
		str(python),
		"-m",
		"mypy",
	]


def test_run_reports_non_utf8_subprocess_output(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class FailedProcess:
		stdout = io.BytesIO("stdout ошибкаstderr ошибка".encode("cp1251"))

		def wait(self, timeout: float | None = None) -> int:
			return 1

		def kill(self) -> None:
			return None

	monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())
	monkeypatch.setattr(runner.locale, "getpreferredencoding", lambda _do_setlocale: "cp1251")

	with pytest.raises(runner.QualityGateError, match="stdout ошибка"):
		runner.run(["pip", "install"], Path("."), {})


def test_run_returns_decoded_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
	class SuccessfulProcess:
		stdout = io.BytesIO(b"coverage report")

		def wait(self, timeout: float | None = None) -> int:
			return 0

		def kill(self) -> None:
			return None

	monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: SuccessfulProcess())

	assert runner.run(["coverage", "report"], Path("."), {}) == "coverage report"


def test_bounded_output_marks_truncated_external_output() -> None:
	result = runner._bounded_output("x" * (runner.MAX_COMMAND_OUTPUT_CHARS + 1))

	assert result.endswith("[output truncated]")
	assert len(result) < runner.MAX_COMMAND_OUTPUT_CHARS + 32


def test_run_bounds_subprocess_output(monkeypatch: pytest.MonkeyPatch) -> None:
	class LargeProcess:
		stdout = io.BytesIO(b"x" * (runner.MAX_COMMAND_OUTPUT_BYTES + 100))

		def wait(self, timeout: float | None = None) -> int:
			return 0

		def kill(self) -> None:
			return None

	monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: LargeProcess())

	result = runner.run(["large-output"], Path("."), {})

	assert result.endswith("[output truncated]")
	assert len(result) < runner.MAX_COMMAND_OUTPUT_CHARS + 32


def test_bounded_subprocess_kills_and_joins_after_timeout(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	class TimeoutProcess:
		stdout = io.BytesIO(b"partial output")
		wait_calls = 0
		was_killed = False

		def wait(self, timeout: float | None = None) -> int:
			self.wait_calls += 1
			if self.wait_calls == 1:
				assert timeout is not None
				raise subprocess.TimeoutExpired(["slow"], timeout)
			return 0

		def kill(self) -> None:
			self.was_killed = True

	process = TimeoutProcess()
	monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)

	with pytest.raises(subprocess.TimeoutExpired):
		runner._run_bounded_subprocess(["slow"], Path("."), {}, 0.1)

	assert process.was_killed
	assert process.wait_calls == EXPECTED_TIMEOUT_WAIT_CALLS


def test_load_components_accepts_repository_without_python() -> None:
	root = FIXTURES / "no-python"

	assert runner.load_components(root) == []


def test_check_skips_python_tools_without_components(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "no-python"
	calls: list[tuple[list[str], dict[str, str]]] = []

	def fake_run(
		command: list[str], root: Path, environment: dict[str, str], *, timeout: float
	) -> None:
		calls.append((command, environment))

	def fake_temporary_directory(root: Path) -> AbstractContextManager[str]:
		return nullcontext("test-temporary-directory")

	monkeypatch.setattr(runner, "run", fake_run)
	monkeypatch.setattr(runner, "temporary_directory", fake_temporary_directory)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)

	runner.check(root)

	assert calls == []


def test_load_components_rejects_path_outside_repository() -> None:
	root = FIXTURES / "invalid-path"

	with pytest.raises(runner.QualityGateError, match="repository-relative"):
		runner.load_components(root)


def test_web_budget_is_unchecked_when_component_root_cannot_be_resolved(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	manifest_path = tmp_path / "quality-gate.toml"
	manifest_path.write_text(
		"""waivers = []

[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "fixture"
domains = ["repository", "web"]
required_documents = ["AGENTS.md"]

[[web]]
name = "frontend"
root = "assets"
javascript = ["*.js"]
css = []
exclude = []
""",
		encoding="utf-8",
	)
	assets = tmp_path / "assets"
	assets.mkdir()
	(assets / "app.js").write_bytes(b"x")
	manifest = runner.load_manifest(tmp_path)
	original_resolve = Path.resolve

	def fail_component_resolve(path: Path, *, strict: bool = False) -> Path:
		if path == assets:
			raise OSError("unreadable boundary")
		return original_resolve(path, strict=strict)

	monkeypatch.setattr(Path, "resolve", fail_component_resolve)

	result = runner.web_budget_results(tmp_path, manifest)[0]

	assert result.status is runner.Status.UNCHECKED
	assert result.check_id == "web.component_1.javascript_budget"
	assert result.findings[0].path == "assets"


def test_web_budget_does_not_measure_an_asset_through_an_external_link(
	tmp_path: Path,
) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		"""waivers = []

[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "fixture"
domains = ["repository", "web"]
required_documents = ["AGENTS.md"]

[[web]]
name = "frontend"
root = "assets"
javascript = ["linked/**/*.js"]
css = []
exclude = []
""",
		encoding="utf-8",
	)
	assets = tmp_path / "assets"
	outside = tmp_path / "outside"
	assets.mkdir()
	outside.mkdir()
	(outside / "escaped.js").write_bytes(b"x")
	try:
		(assets / "linked").symlink_to(outside, target_is_directory=True)
	except OSError:
		pytest.skip("directory symbolic links are unavailable on this platform")

	result = runner.web_budget_results(tmp_path, runner.load_manifest(tmp_path))[0]

	assert result.status is runner.Status.UNCHECKED
	assert result.findings[0].path == "assets/linked/escaped.js"
	assert "outside or traverses" in result.findings[0].message


def test_check_sets_a_writable_temporary_directory(monkeypatch: pytest.MonkeyPatch) -> None:
	root = FIXTURES / "valid"
	calls: list[tuple[list[str], dict[str, str]]] = []
	expected_call_count = 3

	def fake_run(
		command: list[str], root: Path, environment: dict[str, str], *, timeout: float
	) -> None:
		calls.append((command, environment))

	def fake_temporary_directory(root: Path) -> AbstractContextManager[str]:
		return nullcontext("test-temporary-directory")

	monkeypatch.setattr(runner, "run", fake_run)
	monkeypatch.setattr(runner, "temporary_directory", fake_temporary_directory)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)

	runner.check(root)

	assert len(calls) == expected_call_count
	assert calls[0][1]["TMP"] == calls[0][1]["TEMP"]
	assert calls[0][1]["TMP"] == "test-temporary-directory"
	assert calls[0][1]["QUALITY_GATE_POLICY_ROOT"] == str(runner.POLICY_DIR.parent.parent)


def test_safe_environment_isolated_from_user_python_state(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("HOME", "user-home")
	monkeypatch.setenv("PYTHONPATH", "user-pythonpath")
	monkeypatch.setenv("VIRTUAL_ENV", "user-venv")
	monkeypatch.setenv("QUALITY_GATE_POLICY_ROOT", "user-policy-root")
	monkeypatch.setenv("LOCALAPPDATA", "parent-local-app-data")
	monkeypatch.setenv("XDG_CACHE_HOME", "parent-xdg-cache")
	monkeypatch.setenv("CI", "true")
	monkeypatch.setenv("GITHUB_ACTIONS", "true")

	environment = runner._safe_environment("quality-gate-temporary")

	assert environment["HOME"] == "quality-gate-temporary"
	assert environment["TMP"] == "quality-gate-temporary"
	assert environment["TEMP"] == "quality-gate-temporary"
	if os.name == "nt":
		assert environment["USERPROFILE"] == "quality-gate-temporary"
	assert "PYTHONPATH" not in environment
	assert "VIRTUAL_ENV" not in environment
	assert "QUALITY_GATE_POLICY_ROOT" not in environment
	assert "LOCALAPPDATA" not in environment
	assert "XDG_CACHE_HOME" not in environment
	assert "CI" not in environment
	assert "GITHUB_ACTIONS" not in environment
	assert "QUALITY_GATE_ADMISSION_ROOT" not in environment
	assert "RAYON_NUM_THREADS" not in environment


def test_expired_admission_returns_before_snapshot_construction(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	def reject_admission() -> AbstractContextManager[None]:
		raise AdmissionError("local queue wait expired; no check was run", "free a local slot")

	monkeypatch.setattr(runner, "running_in_ci", lambda: False)
	monkeypatch.setattr(runner, "gate_admission", reject_admission)
	monkeypatch.setattr(
		runner,
		"candidate_snapshot",
		lambda _root: pytest.fail("snapshot construction ran without queue admission"),
	)

	verdict = runner.check(tmp_path)

	assert verdict.exit_code == UNCHECKED_EXIT
	assert verdict.results[0].check_id == "runtime.admission"
	assert verdict.results[0].status is runner.Status.UNCHECKED


def test_check_skips_local_admission_in_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(runner, "running_in_ci", lambda: True)
	monkeypatch.setattr(
		runner,
		"gate_admission",
		lambda: pytest.fail("CI must not wait on the local queue"),
	)
	monkeypatch.setattr(runner, "temporary_workspace", lambda _root: nullcontext())
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	passed = runner.Verdict(
		(runner.CheckResult("ci.fixture", runner.Status.PASSED, "CI check completed"),)
	)
	monkeypatch.setattr(runner, "_check_snapshot", lambda *_args, **_kwargs: passed)

	assert runner._run_snapshot(tmp_path) is passed


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
	assert not any(result.check_id.startswith("python.component_") for result in verdict.results)
	assert verdict.exit_code == runner.EXIT_UNCHECKED


def test_workspace_failure_reports_only_the_unavailable_workspace(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	root = FIXTURES / "valid"

	def fail_workspace(_root: Path) -> object:
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
	assert any(result.check_id == "python.component_1.ruff" for result in verdict.results)


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


def _web_manifest(*, javascript: bool = True, css: bool = True) -> str:
	javascript_declaration = 'javascript = ["js/**/*.js"]\n' if javascript else ""
	css_declaration = 'css = ["css/**/*.css"]\n' if css else ""
	return f"""waivers = []

[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "fixture"
domains = ["repository", "web"]
required_documents = ["AGENTS.md"]

[[web]]
name = "frontend"
root = "assets"
{javascript_declaration}{css_declaration}exclude = []
"""


def _web_policy(tmp_path: Path, *, binary: bytes | None = None) -> Path:
	policy_root = tmp_path / "policy-root"
	policy_dir = policy_root / "quality_gate" / "policy"
	shutil.copytree(runner.POLICY_DIR, policy_dir, copy_function=shutil.copyfile)
	if binary is not None:
		inventory = tomllib.loads((policy_dir / "biome.toml").read_text())
		platform = "windows-x64" if os.name == "nt" else "linux-x64"
		entry = inventory["biome"]["platforms"][platform]
		binary_path = policy_root / entry["path"]
		binary_path.write_bytes(binary)
		inventory_text = (policy_dir / "biome.toml").read_text()
		(policy_dir / "biome.toml").write_text(
			inventory_text.replace(entry["sha256"], hashlib.sha256(binary).hexdigest())
		)
	return policy_root


def _web_fixture(
	tmp_path: Path,
	*,
	binary: bytes | None = b"standalone biome",
	javascript: bool = True,
	css: bool = True,
) -> Path:
	(tmp_path / "quality-gate.toml").write_text(
		_web_manifest(javascript=javascript, css=css), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	for relative in ("assets/js/app.js", "assets/css/app.css"):
		asset = tmp_path / relative
		asset.parent.mkdir(parents=True, exist_ok=True)
		content = "const value = 1;\n" if relative.endswith(".js") else "body { color: red; }\n"
		asset.write_text(content, encoding="utf-8")
	return _web_policy(tmp_path, binary=binary)


def _fake_biome_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	script = tmp_path / "fake_biome.py"
	config = tmp_path / "fake-biome.json"
	config.write_text("{}\n", encoding="utf-8")
	script.write_text(
		"""from pathlib import Path
import sys
import json

args = sys.argv[1:]
with Path(__file__).with_suffix(".jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if not args or args[0] != "ci" or "--config-path" not in args:
    raise SystemExit(90)
if "--write" in args or "--fix" in args:
    raise SystemExit(91)
asset_paths = [Path(value) for value in args if value.endswith((".js", ".css"))]
if any(path.name == "undeclared.js" for path in asset_paths):
    raise SystemExit(92)
for path in asset_paths:
    content = path.read_text(encoding="utf-8")
    if "--linter-enabled=true" in args and ("unused" in content or "lint-error" in content):
        print("lint finding")
        raise SystemExit(1)
    if "--formatter-enabled=true" in args and "bad-format" in content:
        print("format finding")
        raise SystemExit(1)
""",
		encoding="utf-8",
	)
	monkeypatch.setattr(
		runner,
		"_biome_command",
		lambda _prepared: [sys.executable, str(script), "ci", "--config-path", str(config)],
	)
	return script.with_suffix(".jsonl")


def _assert_biome_invocations(log_path: Path, expected_assets: set[str]) -> None:
	invocations = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
	assert len(invocations) == EXPECTED_BIOME_INVOCATIONS
	for args in invocations:
		assert "--write" not in args
		assert "--fix" not in args
		assert {value for value in args if value.endswith((".js", ".css"))} == expected_assets
		assert "--assist-enabled=false" in args

	lint_args = next(args for args in invocations if "--linter-enabled=true" in args)
	format_args = next(args for args in invocations if "--formatter-enabled=true" in args)
	assert "--formatter-enabled=false" in lint_args
	assert "--linter-enabled=false" in format_args


def _web_prepared(policy_root: Path) -> SimpleNamespace:
	policy = tomllib.loads((policy_root / "quality_gate" / "policy" / "biome.toml").read_text())[
		"biome"
	]
	platform = "windows-x64" if os.name == "nt" else "linux-x64"
	binary = policy["platforms"][platform]
	return SimpleNamespace(
		policy_root=policy_root,
		release_manifest=ReleaseManifest(
			"v2.0.0",
			(ReleaseFile("quality_gate.whl", "a" * 64),),
			(ExternalTool("biome", policy["version"], binary["path"], binary["sha256"]),),
		),
		runtimes=(),
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


def test_check_marks_biome_unchecked_when_standalone_binary_is_missing(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path, binary=None)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2
	assert verdict.exit_code == runner.EXIT_UNCHECKED


def test_check_marks_biome_unchecked_when_standalone_binary_is_corrupt(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	platform = "windows-x64" if os.name == "nt" else "linux-x64"
	entry = tomllib.loads((policy_root / "quality_gate" / "policy" / "biome.toml").read_text())[
		"biome"
	]["platforms"][platform]
	(policy_root / entry["path"]).write_bytes(b"corrupt biome")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2


def test_check_does_not_duplicate_biome_results_after_partial_web_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	(tmp_path / "quality-gate.toml").write_text(
		_web_manifest()
		+ '\n[[web]]\nname = "second"\nroot = "assets"\n'
		+ 'javascript = ["js/**/*.js"]\ncss = []\nexclude = []\n',
		encoding="utf-8",
	)
	prepared = _web_prepared(policy_root)
	command_calls = 0

	def fail_first_biome_command(_prepared: object) -> list[str]:
		nonlocal command_calls
		command_calls += 1
		if command_calls == 1:
			raise runner.QualityGateError(
				"first Biome component is unavailable",
				exit_code=runner.EXIT_UNCHECKED,
				recovery_action="restore Biome",
			)
		return ["unused"]

	def fail_temporary_directory(_root: Path) -> object:
		raise OSError("temporary directory unavailable")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)
	monkeypatch.setattr(runner, "_biome_command", fail_first_biome_command)
	monkeypatch.setattr(runner, "temporary_directory", fail_temporary_directory)

	verdict = runner.check(tmp_path)

	biome_ids = [result.check_id for result in verdict.results if ".biome_" in result.check_id]
	assert biome_ids == [
		"web.component_1.biome_lint",
		"web.component_1.biome_format",
		"web.component_2.biome_lint",
		"web.component_2.biome_format",
	]
	assert all(
		result.status is runner.Status.UNCHECKED
		for result in verdict.results
		if ".biome_" in result.check_id
	)


@pytest.mark.parametrize(
	("relative", "content", "failed_check"),
	(
		("assets/js/app.js", "const unused = 1;\n", "web.component_1.biome_lint"),
		("assets/css/app.css", "a { lint-error: true; }\n", "web.component_1.biome_lint"),
		("assets/css/app.css", "a{bad-format}\n", "web.component_1.biome_format"),
	),
)
def test_check_reports_biome_findings_for_invalid_staged_content(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	relative: str,
	content: str,
	failed_check: str,
) -> None:
	policy_root = _web_fixture(tmp_path)
	prepared = _web_prepared(policy_root)
	_fake_biome_command(tmp_path, monkeypatch)
	(tmp_path / relative).write_text(content, encoding="utf-8")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)

	verdict = runner.check(tmp_path)

	biome_results = {
		result.check_id: result.status for result in verdict.results if ".biome_" in result.check_id
	}
	assert biome_results[failed_check] is runner.Status.FAILED
	other_check = next(check_id for check_id in biome_results if check_id != failed_check)
	assert biome_results[other_check] is runner.Status.PASSED
