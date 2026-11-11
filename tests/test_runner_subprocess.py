"""Related behavior tests."""

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
from test_runner import (
	EXPECTED_TIMEOUT_WAIT_CALLS,
	FIXTURES,
	UNCHECKED_EXIT,
	_fake_candidate_snapshot,
)

from quality_gate import runner, runner_subprocess
from quality_gate.admission import AdmissionError
from quality_gate.distribution import ExternalTool, ReleaseFile, ReleaseManifest
from quality_gate.launcher import PreparedEnvironment


def test_load_components_accepts_python_component() -> None:
	root = FIXTURES / "valid"

	components = runner.load_components(root)

	assert components == [runner.PythonComponent(root / "app", None, None, True, (), 300)]


def test_decode_subprocess_output_handles_windows_encoding(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(
		runner_subprocess.locale, "getpreferredencoding", lambda _do_setlocale: "cp1251"
	)

	assert runner_subprocess.decode_subprocess_output("ошибка pip".encode("cp1251")) == "ошибка pip"


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

	monkeypatch.setattr(
		runner_subprocess.subprocess, "Popen", lambda *args, **kwargs: FailedProcess()
	)
	monkeypatch.setattr(
		runner_subprocess.locale, "getpreferredencoding", lambda _do_setlocale: "cp1251"
	)

	with pytest.raises(runner.QualityGateError, match="stdout ошибка"):
		runner.run(["pip", "install"], Path("."), {})


def test_run_returns_decoded_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
	class SuccessfulProcess:
		stdout = io.BytesIO(b"coverage report")

		def wait(self, timeout: float | None = None) -> int:
			return 0

		def kill(self) -> None:
			return None

	monkeypatch.setattr(
		runner_subprocess.subprocess, "Popen", lambda *args, **kwargs: SuccessfulProcess()
	)

	assert runner.run(["coverage", "report"], Path("."), {}) == "coverage report"


def test_bounded_output_marks_truncated_external_output() -> None:
	result = runner_subprocess._bounded_output(
		"x" * (runner_subprocess.MAX_COMMAND_OUTPUT_CHARS + 1)
	)

	assert result.endswith("[output truncated]")
	assert len(result) < runner_subprocess.MAX_COMMAND_OUTPUT_CHARS + 32


def test_run_bounds_subprocess_output(monkeypatch: pytest.MonkeyPatch) -> None:
	class LargeProcess:
		stdout = io.BytesIO(b"x" * (runner_subprocess.MAX_COMMAND_OUTPUT_BYTES + 100))

		def wait(self, timeout: float | None = None) -> int:
			return 0

		def kill(self) -> None:
			return None

	monkeypatch.setattr(
		runner_subprocess.subprocess, "Popen", lambda *args, **kwargs: LargeProcess()
	)

	result = runner.run(["large-output"], Path("."), {})

	assert result.endswith("[output truncated]")
	assert len(result) < runner_subprocess.MAX_COMMAND_OUTPUT_CHARS + 32


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
	monkeypatch.setattr(runner_subprocess.subprocess, "Popen", lambda *args, **kwargs: process)

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


def test_progress_does_not_claim_preparation_after_admission_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	def reject_admission() -> AbstractContextManager[None]:
		raise AdmissionError("queue expired", "retry")

	monkeypatch.setattr(runner, "running_in_ci", lambda: False)
	monkeypatch.setattr(runner, "gate_admission", reject_admission)
	runner._run_snapshot(tmp_path)
	output = capsys.readouterr().err
	assert "wait for completion without polling" in output
	assert "preparation completed" not in output
