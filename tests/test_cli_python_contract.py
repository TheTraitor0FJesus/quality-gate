"""Public CLI tests for the Python component contract."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
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
	root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
	monkeypatch.setattr(sys, "argv", ["quality-gate", "--root", str(root), "check"])
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

	result, output = _invoke(root, monkeypatch, capsys)

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
	_patch_deptry_process(monkeypatch, "x" * (runner.MAX_DEPTRY_REPORT_BYTES + 1), returncode=1)
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

	result, output = _invoke(root, monkeypatch, capsys)

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

	result, output = _invoke(root, monkeypatch, capsys)

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
		"#" * (runner.MAX_DEPTRY_REPORT_BYTES + 1), encoding="utf-8"
	)
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "dependency metadata exceeds the size limit" in output


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

	result, output = _invoke(root, monkeypatch, capsys)

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

	passed_result, passed_output = _invoke(root, monkeypatch, capsys)

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

	result, output = _invoke(root, monkeypatch, capsys)

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

	assert failed_result == 1
	assert "python.component_1.ruff: failed" in failed_output

	source.write_text(
		"""def safe(values: list[str] | None = None) -> None:
\tif values is not None:
\t\tvalues.append("x")
""",
		encoding="utf-8",
	)
	assert _git(root, "add", "app/app.py").returncode == 0

	passed_result, passed_output = _invoke(root, monkeypatch, capsys)

	assert passed_result == 0
	assert "python.component_1.ruff: passed" in passed_output


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
