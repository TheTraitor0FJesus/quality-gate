"""Public CLI tests for the Python component contract."""

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
from quality_gate.runner_dependency import MAX_DEPTRY_REPORT_BYTES

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
) -> tuple[int, str]:
	arguments = ["quality-gate", "--root", str(root), "check"]
	if verbose:
		arguments.append("--verbose")
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


def test_cli_summarizes_declared_components_without_individual_passed_lines(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	root = _disposable_repository(tmp_path, "multi-component")
	seen_commands: list[list[str]] = []
	_patch_runtime(monkeypatch, root, seen_commands, None)

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 0
	assert output.splitlines()[0] == "Quality Gate: 0"
	assert "Observed results:" in output
	assert "python.component_1.ruff: passed" not in output
	assert "python.component_2.ruff: passed" not in output
	assert "python.component_1.pytest: not_applicable" not in output
	assert "python.component_2.pytest: not_applicable" not in output
	assert len([command for command in seen_commands if "ruff" in command]) == (
		EXPECTED_MULTI_COMPONENT_RUFF_CHECKS
	)


@pytest.mark.parametrize("code", ["DEP001", "DEP002", "DEP003", "DEP004"])
def test_cli_reports_supported_dependency_findings(
	code: str,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(
		monkeypatch,
		[_deptry_issue(code, "example-package", "app/main.py", 3)],
	)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 1
	assert "python.component_1.deptry: failed" in output
	assert code in output
	assert "app/main.py:3" in output.replace("\\", "/")


def test_cli_applies_one_exact_dependency_waiver(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	waiver = """[[waivers]]
kind = "standard"
check_id = "python.component_1.deptry"
target = "pyproject.toml::DEP002::plugin-package"
reason = "The application discovers this reviewed plugin through configuration."
approved_by = "Repository Owner"
reviewed_on = "2026-09-02"
expires_on = "2027-09-02"
"""
	_declare_dependency_input(root, waiver)
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(
		monkeypatch,
		[_deptry_issue("DEP002", "plugin-package", "pyproject.toml", None)],
	)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert result == 0
	assert "python.component_1.deptry: waived" in output
	assert "pyproject.toml::DEP002::plugin-package" in output


def test_cli_keeps_unwaived_dependency_findings_blocking(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	waiver = """[[waivers]]
kind = "standard"
check_id = "python.component_1.deptry"
target = "pyproject.toml::DEP002::plugin-package"
reason = "The application discovers this reviewed plugin through configuration."
approved_by = "Repository Owner"
reviewed_on = "2026-09-02"
expires_on = "2027-09-02"
"""
	_declare_dependency_input(root, waiver)
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(
		monkeypatch,
		[
			_deptry_issue("DEP002", "plugin-package", "pyproject.toml", None),
			_deptry_issue("DEP002", "stale-package", "pyproject.toml", None),
		],
	)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == 1
	assert "python.component_1.deptry: failed" in output
	assert "stale-package" in output
	assert "plugin-package violates DEP002" not in output


@pytest.mark.parametrize(
	("returncode", "tool_output"),
	[
		(1, "No module named deptry"),
		(2, "Dependency metadata could not be extracted"),
	],
)
def test_cli_reports_unavailable_dependency_analysis_as_unchecked(
	returncode: int,
	tool_output: str,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(monkeypatch, None, returncode=returncode, output=tool_output)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "python.component_1.deptry: unchecked" in output
	assert "python.component_1.deptry: passed" not in output


@pytest.mark.parametrize(
	("issues", "returncode"),
	[
		("not JSON", 1),
		({"unexpected": "object"}, 1),
		([], 1),
		([_deptry_issue("DEP001", "package", "app/main.py", 1)], 0),
		([_deptry_issue("DEP001", "package", "../escape.py", 1)], 1),
		([_deptry_issue("DEP001", "package", "", 1)], 1),
	],
)
def test_cli_rejects_unusable_dependency_reports(
	issues: object,
	returncode: int,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(monkeypatch, issues, returncode=returncode)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "python.component_1.deptry: unchecked" in output


def test_cli_rejects_oversized_dependency_report(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(monkeypatch, "x" * (MAX_DEPTRY_REPORT_BYTES + 1), returncode=1)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "python.component_1.deptry: unchecked" in output


def test_cli_rejects_missing_dependency_metadata(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			"dependency_inputs = []", 'dependency_inputs = ["missing.toml"]'
		),
		encoding="utf-8",
	)
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "python.component_1.deptry: unchecked" in output
	assert "missing.toml" in output


@pytest.mark.parametrize(
	("configuration", "source"),
	[
		('\nignore = ["DEP002"]\n', ""),
		("", "import plugin  # deptry: ignore[DEP001]\n"),
		("", "import plugin  # deptry: ignore[DEP001]  # noqa: F401\n"),
		("", "import plugin  # noqa: F401  # deptry: ignore[DEP001]\n"),
	],
)
def test_cli_rejects_native_deptry_suppressions(
	configuration: str,
	source: str,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	pyproject = root / "pyproject.toml"
	pyproject.write_text(pyproject.read_text(encoding="utf-8") + configuration, encoding="utf-8")
	(root / "app" / "main.py").write_text(source, encoding="utf-8")
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(monkeypatch, [], returncode=0)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert result in {1, EXIT_UNCHECKED}
	assert "python.component_1.deptry: passed" not in output
	assert "typed waiver" in output


def test_cli_does_not_treat_string_literals_as_inline_suppressions(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	(root / "app" / "main.py").write_text('value = "# deptry: ignore[DEP001]"\n', encoding="utf-8")
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(monkeypatch, [], returncode=0)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys, verbose=True)

	assert result == 0
	assert "python.component_1.deptry: passed" in output


def test_cli_rejects_unclassified_requirement_metadata(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			'["pyproject.toml"]', '["pyproject.toml", "requirements-test.txt"]'
		),
		encoding="utf-8",
	)
	(root / "requirements-test.txt").write_text("pytest==9.1.1\n", encoding="utf-8")
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "not classified as runtime or development" in output


def test_cli_rejects_multiple_pyproject_inputs(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			'["pyproject.toml"]', '["pyproject.toml", "config/pyproject.toml"]'
		),
		encoding="utf-8",
	)
	(root / "config").mkdir()
	shutil.copy2(root / "pyproject.toml", root / "config" / "pyproject.toml")
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "at most one pyproject.toml" in output


def test_cli_rejects_mixed_pyproject_and_requirements_inputs(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			'["pyproject.toml"]', '["pyproject.toml", "requirements.txt"]'
		),
		encoding="utf-8",
	)
	(root / "requirements.txt").write_text("ignored-package==1\n", encoding="utf-8")
	_patch_runtime(monkeypatch, root, [], None)
	_patch_deptry_process(monkeypatch, [], returncode=0)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "mixed pyproject.toml and requirements" in output


def test_cli_rejects_oversized_dependency_metadata(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = _disposable_repository(tmp_path, "valid")
	_declare_dependency_input(root)
	(root / "pyproject.toml").write_text(
		"#" * (MAX_DEPTRY_REPORT_BYTES + 1), encoding="utf-8"
	)
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "dependency metadata exceeds the size limit" in output
