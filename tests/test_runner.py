from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from quality_gate import runner
from quality_gate.distribution import ExternalTool, ReleaseFile, ReleaseManifest
from quality_gate.launcher import PreparedEnvironment

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_TIMEOUT_WAIT_CALLS = 2


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

	environment = runner._safe_environment("quality-gate-temporary")

	assert environment["HOME"] == "quality-gate-temporary"
	assert environment["TMP"] == "quality-gate-temporary"
	assert environment["TEMP"] == "quality-gate-temporary"
	if os.name == "nt":
		assert environment["USERPROFILE"] == "quality-gate-temporary"
	assert "PYTHONPATH" not in environment
	assert "VIRTUAL_ENV" not in environment
	assert "QUALITY_GATE_POLICY_ROOT" not in environment


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

	monkeypatch.setattr(runner, "run", record_run)

	runner.format_paths(root, ("app",))

	assert len(calls) == 1
	assert calls[0][calls[0].index("format") + 1] == "--config"
	assert calls[0][-1] == "app"
	assert "--check" not in calls[0]


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
	assert verdict.exit_code == runner.EXIT_UNCHECKED


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
