"""CLI contracts for dependency analysis and component summaries."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cli_feature_support72 import (
	EXIT_UNCHECKED,
	EXPECTED_MULTI_COMPONENT_RUFF_CHECKS,
	_declare_dependency_input,
	_deptry_issue,
	_disposable_repository,
	_git,
	_invoke,
	_patch_deptry_process,
	_patch_runtime,
)

from quality_gate.runner_dependency import MAX_DEPTRY_REPORT_BYTES


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
	(root / "pyproject.toml").write_text("#" * (MAX_DEPTRY_REPORT_BYTES + 1), encoding="utf-8")
	_patch_runtime(monkeypatch, root, [], None)
	assert _git(root, "add", ".").returncode == 0

	result, output = _invoke(root, monkeypatch, capsys)

	assert result == EXIT_UNCHECKED
	assert "dependency metadata exceeds the size limit" in output
