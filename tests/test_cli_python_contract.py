"""Public CLI tests for the Python component contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate import runner
from quality_gate.cli import main
from quality_gate.contracts import load_manifest
from quality_gate.distribution import ExternalTool, ReleaseFile, ReleaseManifest

FIXTURES = Path(__file__).parent / "fixtures"
REPOSITORY = Path(__file__).resolve().parents[1]
EXIT_UNCHECKED = 2
EXPECTED_MULTI_COMPONENT_RUFF_CHECKS = 4
VALID_WORKFLOW = """name: Quality gate
on:
  pull_request:
  push:
permissions: read
concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: quality-gate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567
"""


def _coverage_release_manifest() -> ReleaseManifest:
	return ReleaseManifest(
		"v2.0.0",
		(ReleaseFile("quality_gate.whl", "a" * 64),),
		(ExternalTool("coverage", "7.0.0", "coverage.whl", "b" * 64),),
	)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["git", *arguments],
		cwd=root,
		env={
			**os.environ,
			"GIT_CONFIG_GLOBAL": str(root / "missing-global-config"),
			"GIT_CONFIG_NOSYSTEM": "1",
		},
		capture_output=True,
		text=True,
		check=False,
	)


def _disposable_repository(tmp_path: Path, fixture: str) -> Path:
	root = tmp_path / "repository"
	shutil.copytree(
		FIXTURES / fixture,
		root,
		ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
	)
	workflow = root / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	workflow.write_text(VALID_WORKFLOW, encoding="utf-8")
	assert _git(root, "init").returncode == 0
	assert _git(root, "add", ".").returncode == 0
	return root


def _patch_runtime(
	monkeypatch: pytest.MonkeyPatch,
	_root: Path,
	seen_commands: list[list[str]],
	behavior: object,
	release_manifest: ReleaseManifest | None = None,
) -> None:
	def prepare(actual_root: Path, **_kwargs: object) -> SimpleNamespace:
		manifest = load_manifest(actual_root)
		return SimpleNamespace(
			policy_root=REPOSITORY,
			release_manifest=release_manifest,
			runtimes=tuple(
				SimpleNamespace(python=Path(sys.executable), current=True)
				for _component in manifest.python
			),
		)

	def run(
		command: list[str],
		command_root: Path,
		environment: dict[str, str],
		*,
		timeout: float,
	) -> str | None:
		seen_commands.append(command)
		if callable(behavior):
			return_value = behavior(command, command_root, environment, timeout)
			if isinstance(return_value, str):
				return return_value
		return None

	monkeypatch.setattr(runner, "prepare", prepare)
	monkeypatch.setattr(runner, "run", run)


def _invoke(
	root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
	monkeypatch.setattr(sys, "argv", ["quality-gate", "--root", str(root), "check"])
	result = main()
	return result, capsys.readouterr().out


def test_cli_checks_all_declared_components_and_collapses_passed_output(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "multi-component")
	seen_commands: list[list[str]] = []
	_patch_runtime(monkeypatch, root, seen_commands, None)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 0
	assert "python.component_1.ruff: passed" in output
	assert "python.component_2.ruff: passed" in output
	assert "python.component_1.pytest: not_applicable" in output
	assert "python.component_2.pytest: not_applicable" in output
	assert "  - " not in output
	assert len([command for command in seen_commands if "ruff" in command]) == (
		EXPECTED_MULTI_COMPONENT_RUFF_CHECKS
	)


def test_cli_reports_explicitly_not_applicable_tests_without_blocking(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_patch_runtime(monkeypatch, root, [], None)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 0
	assert "python.component_1.pytest: not_applicable" in output


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
	assert "python.component_1.pytest: unchecked" in output
	assert "python.component_1.pytest: failed" not in output


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
	assert "python.component_1.coverage_report: passed" in output
	assert "coverage report: TOTAL 10 1 90%" in output
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
	assert "python.component_1.coverage: passed" in output
	assert "python.component_1.coverage_report: passed" in output
	assert "coverage report unavailable; report-only: coverage provider failed" in output


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
