"""CLI Python component execution and reporting contracts."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cli_python_contract import (
	EXIT_UNCHECKED,
	REPOSITORY,
	_coverage_release_manifest,
	_declare_dependency_input,
	_disposable_repository,
	_git,
	_invoke,
	_patch_runtime,
)

from quality_gate import runner
from quality_gate.cli import main
from quality_gate.temp_workspace import TemporaryWorkspaceError


def test_cli_ignores_development_imports_in_nested_test_directories(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	pytest.importorskip("deptry", reason="ticket 07 owns deptry release packaging")
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	pyproject = root / "pyproject.toml"
	pyproject.write_text(
		pyproject.read_text(encoding="utf-8").replace(
			'dependencies = ["runtime-package"]', "dependencies = []"
		),
		encoding="utf-8",
	)
	(root / "app" / "tests").mkdir()
	(root / "app" / "tests" / "test_app.py").write_text("import pytest\n", encoding="utf-8")
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert result == 0
	assert "python.component_1.deptry: passed" in output


def test_cli_does_not_load_undeclared_root_deptry_suppressions(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	pytest.importorskip("deptry", reason="ticket 07 owns deptry release packaging")
	root = _disposable_repository(tmp_path, "valid")
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			"dependency_inputs = []", 'dependency_inputs = ["requirements.txt"]'
		),
		encoding="utf-8",
	)
	(root / "requirements.txt").write_text("", encoding="utf-8")
	(root / "pyproject.toml").write_text(
		"""[tool.deptry]
ignore = ["DEP001"]
""",
		encoding="utf-8",
	)
	(root / "app" / "main.py").write_text(
		"import undeclared_fixture_dependency\n", encoding="utf-8"
	)
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 1
	assert "python.component_1.deptry: failed" in output
	assert "DEP001" in output


def test_cli_uses_pinned_deptry_for_invalid_and_corrected_candidates(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	pytest.importorskip("deptry", reason="ticket 07 owns deptry release packaging")
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	pyproject = root / "pyproject.toml"
	pyproject.write_text(
		pyproject.read_text(encoding="utf-8").replace(
			'dependencies = ["runtime-package"]', "dependencies = []"
		),
		encoding="utf-8",
	)
	(root / "app" / "main.py").write_text(
		"import undeclared_fixture_dependency\n", encoding="utf-8"
	)
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	failed_result, failed_output = _invoke(root, monkeypatch, capsys)

	assert failed_result == 1
	assert "python.component_1.deptry: failed" in failed_output
	assert "DEP001" in failed_output

	pyproject.write_text(
		pyproject.read_text(encoding="utf-8").replace(
			"dependencies = []", 'dependencies = ["undeclared-fixture-dependency"]'
		),
		encoding="utf-8",
	)
	assert _git(root, "add", "pyproject.toml").returncode == 0

	passed_result, passed_output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert passed_result == 0
	assert "python.component_1.deptry: passed" in passed_output


def test_cli_explicitly_excludes_notebooks_from_dependency_hygiene(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	pytest.importorskip("deptry", reason="ticket 07 owns deptry release packaging")
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	(root / "app" / "main.py").write_text("import runtime_package\n", encoding="utf-8")
	(root / "app" / "analysis.ipynb").write_text(
		json.dumps(
			{
				"cells": [
					{
						"cell_type": "code",
						"execution_count": None,
						"metadata": {},
						"outputs": [],
						"source": ["import notebook_only_dependency\n"],
					}
				],
				"metadata": {},
				"nbformat": 4,
				"nbformat_minor": 5,
			}
		),
		encoding="utf-8",
	)
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert result == 0, output
	assert "python.component_1.deptry: passed" in output


def test_source_contract_pins_deptry_and_marks_tool_groups_as_development() -> None:
	contract = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))

	assert "deptry==0.25.1" in contract["project"]["optional-dependencies"]["tools"]
	assert set(contract["tool"]["deptry"]["optional_dependencies_dev_groups"]) == {
		"test",
		"tools",
	}


def test_cli_blocks_and_accepts_the_staged_python_policy_candidate(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	source = root / "app" / "app.py"
	source.write_text(
		"""def unsafe(values: list[str] = []) -> None:
\tvalues.append("x")
""",
		encoding="utf-8",
	)
	assert _git(root, "add", "app/app.py").returncode == 0
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *_args, **_kwargs: SimpleNamespace(
			policy_root=REPOSITORY,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	monkeypatch.setattr(
		runner,
		"secret_candidate_result",
		lambda *_args, **_kwargs: runner.CheckResult(
			"secrets.candidate", runner.Status.PASSED, "no credentials detected"
		),
	)

	failed_result, failed_output = _invoke(root, monkeypatch, capsys)

	assert failed_result == 1, failed_output
	assert "python.component_1.ruff: failed" in failed_output

	source.write_text(
		"""def safe(values: list[str] | None = None) -> None:
\tif values is not None:
\t\tvalues.append("x")
""",
		encoding="utf-8",
	)
	assert _git(root, "add", "app/app.py").returncode == 0

	passed_result, passed_output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert passed_result == 0
	assert "python.component_1.ruff: passed" in passed_output


def test_cli_reports_explicitly_not_applicable_tests_without_blocking(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_patch_runtime(monkeypatch, root, [], None)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 0
	assert "not applicable" in output
	assert "python.component_1.pytest: not_applicable" not in output


def test_cli_reports_pytest_collection_failure_as_unchecked(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	seen_commands: list[list[str]] = []

	def fail_collection(
		command: list[str],
		_command_root: Path,
		_environment: dict[str, str],
		_timeout: float,
	) -> None:
		if "pytest" in command:
			raise runner.QualityGateError(
				"ERROR collecting tests\\test_app.py: token=CLI_COLLECTION_SECRET",
				exit_code=EXIT_UNCHECKED,
				recovery_action="restore a collectable pytest suite",
			)

	_patch_runtime(monkeypatch, root, seen_commands, fail_collection)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "python.component_1.pytest: unchecked" in output
	assert "CLI_COLLECTION_SECRET" not in output
	assert "Traceback" not in output
	assert any(
		part.replace("\\", "/") == "tests/extra" for command in seen_commands for part in command
	)


def test_cli_reports_timeout_as_unchecked_without_a_failed_verdict(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	manifest = root / "quality-gate.toml"
	manifest_text = manifest.read_text(encoding="utf-8")
	assert 'required_documents = ["quality-gate.toml"]' in manifest_text
	manifest.write_text(
		manifest_text.replace(
			'required_documents = ["quality-gate.toml"]',
			'required_documents = ["quality-gate.toml", "docs/missing.md"]',
		),
		encoding="utf-8",
	)
	assert _git(root, "add", "quality-gate.toml").returncode == 0

	def time_out(
		command: list[str],
		_command_root: Path,
		_environment: dict[str, str],
		_timeout: float,
	) -> None:
		if "pytest" in command:
			raise runner.QualityGateError(
				"pytest timed out after 1 seconds",
				exit_code=EXIT_UNCHECKED,
				recovery_action="inspect the test suite and retry within the time budget",
			)

	_seen_commands: list[list[str]] = []
	_patch_runtime(monkeypatch, root, _seen_commands, time_out)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "manifest.documents: failed" in output
	assert "docs/missing.md" in output
	assert "python.component_1.pytest: unchecked" in output
	assert "python.component_1.pytest: failed" not in output


@pytest.mark.parametrize("command", ["check", "audit"])
def test_cli_renders_early_workspace_unavailability_as_unchecked(
	command: str,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")

	def unavailable_workspace(_root: Path) -> object:
		raise TemporaryWorkspaceError("repository workspace unavailable")

	monkeypatch.setattr("quality_gate.temp_workspace.temporary_workspace", unavailable_workspace)
	monkeypatch.setattr(sys, "argv", ["quality-gate", "--root", str(root), command])

	result = main()
	output = capsys.readouterr().out

	assert result == EXIT_UNCHECKED
	assert output.splitlines()[0] == "Quality Gate: 2"
	assert "temporary.workspace: unchecked - repository workspace unavailable" in output
	assert "restore a writable repository temp directory" in output


def test_cli_uses_a_sanitized_environment_and_redacts_secret_output(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	secret = "CLI_ENV_SECRET_VALUE"
	monkeypatch.setenv("TOKEN", secret)
	monkeypatch.setenv("PYTHONPATH", "user-pythonpath")
	monkeypatch.setenv("VIRTUAL_ENV", "user-venv")
	seen_environments: list[dict[str, str]] = []

	def inspect_environment(
		_command: list[str],
		_command_root: Path,
		environment: dict[str, str],
		_timeout: float,
	) -> None:
		seen_environments.append(environment)

	_patch_runtime(monkeypatch, root, [], inspect_environment)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 0
	assert seen_environments
	assert all(secret not in environment.values() for environment in seen_environments)
	assert all("PYTHONPATH" not in environment for environment in seen_environments)
	assert all("VIRTUAL_ENV" not in environment for environment in seen_environments)
	assert secret not in output


def test_cli_prints_report_only_coverage_when_provider_is_pinned(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	release_manifest = _coverage_release_manifest()
	secret = "CLI_COVERAGE_SECRET"

	def coverage_report(
		command: list[str],
		_command_root: Path,
		_environment: dict[str, str],
		_timeout: float,
	) -> str | None:
		if "coverage" in command and "report" in command:
			return f"Name Stmts Miss Cover\nTOTAL 10 1 90% token={secret}\n"
		return None

	_patch_runtime(monkeypatch, root, [], coverage_report, release_manifest)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 0
	assert "coverage report: TOTAL 10 1 90%" in output
	assert "Report-only: python.component_1.coverage_report" in output
	assert "python.component_1.coverage_report: passed" not in output
	assert secret not in output
	assert "threshold" not in output.casefold()


def test_cli_does_not_enforce_report_only_coverage_failures(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	release_manifest = _coverage_release_manifest()

	def coverage_failure(
		command: list[str],
		_command_root: Path,
		_environment: dict[str, str],
		_timeout: float,
	) -> None:
		if "coverage" in command:
			raise runner.QualityGateError(
				"coverage provider failed",
				recovery_action="inspect the optional coverage report",
			)

	_patch_runtime(monkeypatch, root, [], coverage_failure, release_manifest)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 0
	assert "coverage report unavailable; report-only: coverage provider failed" in output
	assert "python.component_1.coverage_report: passed" not in output


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_cli_keeps_paths_portable_and_output_compact_across_platforms(
	separator: str,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	seen_commands: list[list[str]] = []

	def fail_with_platform_path(
		command: list[str],
		_command_root: Path,
		_environment: dict[str, str],
		_timeout: float,
	) -> None:
		if "pytest" in command:
			raise runner.QualityGateError(
				f"C:{separator}repo{separator}tests{separator}test_app.py:1: token=CLI_PATH_SECRET",
				exit_code=EXIT_UNCHECKED,
				recovery_action="restore the test suite",
			)

	_patch_runtime(monkeypatch, root, seen_commands, fail_with_platform_path)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "CLI_PATH_SECRET" not in output
	assert "tests/test_app.py" in output.replace("\\", "/")
	assert any(
		part.replace("\\", "/") == "tests/extra" for command in seen_commands for part in command
	)
	assert output.count("Quality Gate:") == 1
