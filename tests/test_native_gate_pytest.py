"""Exercise the native Git hook's wheel-backed Python verification."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_native_gate_queue
from test_native_gate_queue import (
	_GATE_TIMEOUT_SECONDS,
	_SUCCESS_EXIT,
	_load_native_bridge,
	_prepare_native_fixture,
)

from quality_gate.contracts import load_manifest
from quality_gate.hooks import commit_gate_timeout_seconds
from quality_gate.runtime import RuntimeManager


@pytest.fixture
def native_hook_root() -> Iterator[Path]:
	# Native venv launchers need a short physical path; keep ownership repository-local.
	temp = Path(__file__).resolve().parents[1] / "temp"
	temp.mkdir(exist_ok=True)
	with tempfile.TemporaryDirectory(prefix="nh", dir=temp) as directory:
		yield Path(directory)


@pytest.mark.parametrize("lint_failure", [False, True])
@pytest.mark.parametrize("released_engine", [False, True], ids=["candidate-v5", "released-v4"])
def test_native_bridge_keeps_quality_checks_independent_of_tests(
	native_hook_root: Path, monkeypatch: pytest.MonkeyPatch, lint_failure: bool,
	released_engine: bool,
) -> None:
	if released_engine:
		def use_released_wheel(
			_project: Path, wheel: Path, target: Path, _version: str,
		) -> Path:
			shutil.copytree(wheel.parent, target)
			return target / wheel.name
		monkeypatch.setattr(test_native_gate_queue, "_write_candidate_wheel", use_released_wheel)
	bridge, _ = _load_native_bridge()
	project_root = Path(__file__).resolve().parents[1]
	repository, staged_path, queue_root, _ = _prepare_native_fixture(
		native_hook_root, project_root, bridge, monkeypatch, release_version="v4.9.0"
	)
	version = f"{sys.version_info.major}.{sys.version_info.minor}"
	timeout = 45
	manifest_path = repository / "quality-gate.toml"
	manifest_path.write_text(
		staged_path.read_text(encoding="utf-8")
		.replace('domains = ["repository"]', 'domains = ["repository", "python"]')
		.replace(
			f"gate_timeout_seconds = {_GATE_TIMEOUT_SECONDS}", f"gate_timeout_seconds = {timeout}"
		)
		+ f"""
[[python]]
name = "app"
path = "app"
python_version = "{version}"
dependency_inputs = []
test_paths = ["tests"]
tests_applicable = true
""",
		encoding="utf-8",
	)
	(repository / "app").mkdir()
	(repository / "app" / "app.py").write_text(("import os\n" if lint_failure else "VALUE = 1\n"), encoding="utf-8")
	(repository / "tests").mkdir()
	(repository / "tests" / "test_app.py").write_text(
		"def test_app():\n    assert False\n", encoding="utf-8"
	)
	subprocess.run(["git", "add", "--", "."], cwd=repository, check=True, capture_output=True)
	# The hook must inspect the staged lint finding, not this repaired working file.
	(repository / "app" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
	manifest = load_manifest(repository)

	def reuse_test_tools(python: Path, _root: Path, _inputs: tuple[str, ...]) -> None:
		runtime = python.parent.parent
		site_packages = (
			runtime / "Lib/site-packages"
			if os.name == "nt"
			else runtime / "lib" / f"python{version}" / "site-packages"
		)
		(site_packages / "test-tools.pth").write_text(
			sysconfig.get_path("purelib") + "\n", encoding="utf-8"
		)

	prepared = RuntimeManager(queue_root).ensure(
		repository,
		manifest,
		manifest.python[0],
		python_executable=Path(sys.executable),
		install=reuse_test_tools,
	)
	assert prepared.current, prepared.reason
	staged = bridge._staged_manifest(repository)
	assert staged == manifest_path.read_bytes()
	bridge.COMMIT_GATE_TIMEOUT_SECONDS = commit_gate_timeout_seconds(staged)
	result = bridge.run_released_command(
		repository,
		["--root", str(repository), "check", "--verbose"],
		manifest=staged,
		capture_output=True,
	)
	assert result.returncode == (1 if lint_failure or released_engine else _SUCCESS_EXIT), result.stdout + result.stderr
	if released_engine:
		assert "python.component_1.pytest: failed" in result.stdout
		assert "python.component_1.coverage:" in result.stdout
	else:
		assert "python.component_1.pytest" not in result.stdout
		assert "python.component_1.coverage" not in result.stdout
	assert f"python.component_1.ruff: {'failed' if lint_failure else 'passed'}" in result.stdout
