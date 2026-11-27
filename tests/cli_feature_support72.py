"""Shared fixtures and helpers for feature-preserving test partitions."""

from __future__ import annotations

import json
import os
import shutil
import stat
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


def _make_writable(root: Path) -> None:
	for path in (root, *root.rglob("*")):
		if not path.is_symlink():
			path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _disposable_repository(tmp_path: Path, fixture: str) -> Path:
	root = tmp_path / "repository"
	shutil.copytree(
		FIXTURES / fixture,
		root,
		ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
	)
	_make_writable(root)
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
	monkeypatch.setattr(
		runner,
		"secret_candidate_result",
		lambda *_args, **_kwargs: runner.CheckResult(
			"secrets.candidate", runner.Status.PASSED, "no credentials detected"
		),
	)
	monkeypatch.setattr(
		runner,
		"secret_history_result",
		lambda *_args, **_kwargs: runner.CheckResult(
			"secrets.history",
			runner.Status.NOT_APPLICABLE,
			"base-to-head history scan is not requested",
			recovery_action="provide a verified CI base reference when range scanning applies",
		),
	)


def _invoke(
	root: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	*,
	verbose: bool = False,
	profile: bool = False,
) -> tuple[int, str]:
	arguments = ["quality-gate", "--root", str(root), "check"]
	if verbose:
		arguments.append("--verbose")
	if profile:
		arguments.append("--profile")
	monkeypatch.setattr(sys, "argv", arguments)
	result = main()
	return result, capsys.readouterr().out


def _declare_dependency_input(root: Path, waiver: str = "") -> None:
	manifest = root / "quality-gate.toml"
	value = (
		manifest.read_text(encoding="utf-8")
		.replace("waivers = []", waiver or "waivers = []")
		.replace("dependency_inputs = []", 'dependency_inputs = ["pyproject.toml"]')
	)
	manifest.write_text(value, encoding="utf-8")
	(root / "pyproject.toml").write_text(
		"""[project]
name = "fixture"
version = "1.0.0"
dependencies = ["runtime-package"]

[project.optional-dependencies]
tools = ["development-tool"]

[tool.deptry]
optional_dependencies_dev_groups = ["tools"]
""",
		encoding="utf-8",
	)


def _patch_deptry_process(
	monkeypatch: pytest.MonkeyPatch,
	issues: object,
	*,
	returncode: int = 1,
	output: str = "dependency analysis requires attention",
) -> None:
	def execute(
		command: list[str],
		_command_root: Path,
		_environment: dict[str, str],
		_timeout: float | None,
	) -> tuple[int, str]:
		assert "deptry" in command
		if issues is not None:
			report = Path(command[command.index("--json-output") + 1])
			report.write_text(
				issues if isinstance(issues, str) else json.dumps(issues), encoding="utf-8"
			)
		return returncode, output

	monkeypatch.setattr(runner, "_run_bounded_subprocess", execute)


def _deptry_issue(code: str, module: str, path: str, line: int | None) -> dict[str, object]:
	return {
		"error": {"code": code, "message": f"{module} violates {code}"},
		"module": module,
		"location": {"file": path, "line": line, "column": 0},
	}
