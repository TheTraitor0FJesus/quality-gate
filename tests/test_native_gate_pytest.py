"""Exercise the native Git hook's wheel-backed Python verification."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

import pytest
from test_native_gate_queue import (
	_GATE_TIMEOUT_SECONDS,
	_SUCCESS_EXIT,
	_load_native_bridge,
	_prepare_native_fixture,
)

from quality_gate.contracts import load_manifest
from quality_gate.hooks import commit_gate_timeout_seconds
from quality_gate.runtime import RuntimeManager


def test_required_pytest_works_with_gate_imported_from_wheel(tmp_path: Path) -> None:
	project = Path(__file__).resolve().parents[1]
	wheel = tmp_path / "quality_gate-4.9.0-py3-none-any.whl"
	with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
		for path in (project / "quality_gate").glob("*.py"):
			archive.write(path, path.relative_to(project).as_posix())
	(tmp_path / "test_probe.py").write_text(
		"import os\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n"
		"def test_probe(tmp_path):\n"
		"    nested = tmp_path / 'test_nested.py'\n"
		"    nested.write_text('def test_nested(): pass\\n', encoding='utf-8')\n"
		"    for env in (dict(os.environ, PYTHONPATH=str(Path.cwd())), os.environ.copy()):\n"
		"        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', str(nested)],\n"
		"                                env=env, cwd=tmp_path, capture_output=True, timeout=15)\n"
		"        assert result.returncode == 0, result.stdout + result.stderr\n"
		"        assert not Path(os.environ['QUALITY_GATE_PYTEST_OBSERVATION']).exists()\n",
		encoding="utf-8",
	)
	code = """\
import os
import sys
import zipimport
from pathlib import Path
from quality_gate import runner, runner_pytest
from quality_gate.operation import GateOperation
from quality_gate.temp_workspace import temporary_workspace

assert isinstance(runner_pytest.__loader__, zipimport.zipimporter)
root = Path.cwd()
with GateOperation(root, "check") as operation:
    with temporary_workspace(root, workspace_registered=operation.register_workspace,
                             cleanup_owner=operation.handoff_workspace):
        with operation.stage("python.component_1.pytest", "test_probe.py"):
            runner.run([sys.executable, "-m", "pytest", "-q", "test_probe.py"],
                       root, os.environ.copy(), timeout=30)
    print(operation.pytest_observations["python.component_1.pytest"].totals)
"""
	result = subprocess.run(
		[sys.executable, "-B", "-c", code],
		cwd=tmp_path,
		env={**os.environ, "PYTHONPATH": str(wheel)},
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
		timeout=45,
	)
	assert result.returncode == _SUCCESS_EXIT, result.stdout + result.stderr
	assert "1 passed" in result.stdout


def test_native_bridge_runs_pytest_from_candidate_wheel(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	bridge, _ = _load_native_bridge()
	project_root = Path(__file__).resolve().parents[1]
	repository, staged_path, queue_root, _ = _prepare_native_fixture(
		tmp_path, project_root, bridge, monkeypatch
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
	(repository / "app" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
	(repository / "tests").mkdir()
	(repository / "tests" / "test_app.py").write_text(
		"def test_app():\n    assert True\n", encoding="utf-8"
	)
	subprocess.run(["git", "add", "--", "."], cwd=repository, check=True, capture_output=True)
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
	assert result.returncode == _SUCCESS_EXIT, result.stdout + result.stderr
	assert "python.component_1.pytest: passed - pytest totals: 1 passed" in result.stdout
