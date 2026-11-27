"""Gate checks quality independently of test execution."""

from pathlib import Path

import pytest
from cli_feature_support72 import (
	_coverage_release_manifest,
	_disposable_repository,
	_git,
	_invoke,
	_patch_runtime,
)


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("lint_failure", [False, True])
def test_gate_never_starts_pytest_or_coverage(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	profile: bool,
	lint_failure: bool,
) -> None:
	from quality_gate import runner

	root = _disposable_repository(tmp_path, "python-tests")
	(root / "tests" / "test_app.py").write_text(
		"def test_app():\n    assert False\n", encoding="utf-8"
	)
	assert _git(root, "add", ".").returncode == 0
	commands: list[list[str]] = []

	def tool(command: list[str], *_args: object) -> None:
		if lint_failure and "ruff" in command and "check" in command:
			raise runner.QualityGateError("lint finding", check_id="runtime.command")

	_patch_runtime(monkeypatch, root, commands, tool, _coverage_release_manifest())
	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=profile)

	assert exit_code == (1 if lint_failure else 0), output
	assert not any("pytest" in command or "coverage" in command for command in commands)
	assert ".pytest:" not in output
	assert ".coverage:" not in output
	assert ".coverage_report:" not in output
