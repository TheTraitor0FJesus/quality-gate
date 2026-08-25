from __future__ import annotations

import sys
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate import runner
from quality_gate.distribution import ExternalTool, ReleaseFile, ReleaseManifest

FIXTURES = Path(__file__).parent / "fixtures"


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

	assert runner._tool_command(prepared, python, "mypy") == [str(python), "-m", "mypy"]


def test_run_reports_non_utf8_subprocess_output(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(
		runner.subprocess,
		"run",
		lambda *args, **kwargs: SimpleNamespace(
			returncode=1,
			stdout="stdout ошибка".encode("cp1251"),
			stderr="stderr ошибка".encode("cp1251"),
		),
	)
	monkeypatch.setattr(runner.locale, "getpreferredencoding", lambda _do_setlocale: "cp1251")

	with pytest.raises(runner.QualityGateError, match="stdout ошибка"):
		runner.run(["pip", "install"], Path("."), {})


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
