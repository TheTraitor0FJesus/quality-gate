from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

import pytest

import quality_gate.distribution as distribution
import quality_gate.temp_workspace as temp_workspace
from quality_gate.release import _workspace_parents
from quality_gate.temp_workspace import (
	SYSTEM_TEMP_DIRECTORY,
	TemporaryWorkspaceError,
	temporary_workspace,
)


def test_temporary_workspace_routes_and_restores_temp_state(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	system_temp = tmp_path / "system-temp"
	system_temp.mkdir()
	monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
	previous_environment = {key: f"previous-{key}" for key in ("TMP", "TEMP", "TMPDIR")}
	for key, value in previous_environment.items():
		monkeypatch.setenv(key, value)
	monkeypatch.delenv("QUALITY_GATE_SYSTEM_TEMP", raising=False)

	stale_directory = repository / "temp" / "stale-run"
	stale_directory.mkdir(parents=True)
	(stale_directory / "old-file").write_text("stale", encoding="utf-8")
	workspace: Path | None = None
	with pytest.raises(RuntimeError, match="stop after checking cleanup"):
		with temporary_workspace(repository) as active_workspace:
			workspace = active_workspace
			assert active_workspace.parent == repository / "temp"
			assert active_workspace.is_dir()
			assert not stale_directory.exists()
			assert tempfile.gettempdir() == str(active_workspace)
			assert all(os.environ[key] == str(active_workspace) for key in previous_environment)
			assert os.environ["QUALITY_GATE_SYSTEM_TEMP"] == str(SYSTEM_TEMP_DIRECTORY)
			with temporary_workspace(repository) as nested_workspace:
				assert nested_workspace == active_workspace
			child_temporary = Path(tempfile.mkdtemp(prefix="child-"))
			assert child_temporary.parent == active_workspace
			raise RuntimeError("stop after checking cleanup")

	assert workspace is not None
	assert not workspace.exists()
	assert tempfile.tempdir == str(system_temp)
	assert {key: os.environ[key] for key in previous_environment} == previous_environment
	assert "QUALITY_GATE_SYSTEM_TEMP" not in os.environ


def test_release_workspace_uses_active_repository_temp_and_keeps_explicit_override(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	explicit_parent = tmp_path / "short-workspace"
	explicit_parent.mkdir()

	with temporary_workspace(repository) as workspace:
		assert _workspace_parents(None) == (workspace,)
	assert _workspace_parents(explicit_parent) == (explicit_parent.resolve(),)


@pytest.mark.skipif(os.name != "nt", reason="Windows cache fallback only")
def test_windows_cache_fallback_uses_the_original_system_temp(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	system_temp = tmp_path / "system-temp"
	monkeypatch.delenv("LOCALAPPDATA", raising=False)
	monkeypatch.setattr(distribution, "SYSTEM_TEMP_DIRECTORY", system_temp)

	assert distribution.default_cache_root() == system_temp / "quality-gate"


def test_temporary_workspace_preserves_an_active_process_and_removes_stale_runs(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	temp_root = repository / "temp"
	stale_directory = temp_root / "stale-run"
	stale_directory.mkdir(parents=True)
	(stale_directory / "old-file").write_text("stale", encoding="utf-8")
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	ready = coordination / "ready"
	release = coordination / "release"
	script = "\n".join(
		(
			"import sys",
			"import time",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"root, ready, release = (Path(value) for value in sys.argv[1:])",
			"with temporary_workspace(root) as workspace:",
			"    ready.write_text(str(workspace), encoding='utf-8')",
			"    deadline = time.monotonic() + 15",
			"    while not release.exists() and time.monotonic() < deadline:",
			"        time.sleep(0.01)",
			"    if not release.exists():",
			"        raise SystemExit(3)",
		)
	)
	process = subprocess.Popen(
		[sys.executable, "-c", script, str(repository), str(ready), str(release)],
		cwd=Path(__file__).resolve().parents[1],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
		text=True,
	)
	active_workspace: Path | None = None
	try:
		deadline = time.monotonic() + 15
		while not ready.exists() and time.monotonic() < deadline:
			if process.poll() is not None:
				_, stderr = process.communicate()
				pytest.fail(f"workspace subprocess exited early: {stderr}")
			time.sleep(0.01)
		assert ready.exists(), "workspace subprocess did not become active"
		active_workspace = Path(ready.read_text(encoding="utf-8"))
		assert active_workspace.is_dir()

		with temporary_workspace(repository) as current_workspace:
			assert current_workspace != active_workspace
			assert active_workspace.is_dir()
			assert not stale_directory.exists()
		assert active_workspace.is_dir()
	finally:
		release.touch()
		try:
			_, stderr = process.communicate(timeout=15)
		except subprocess.TimeoutExpired:
			process.kill()
			_, stderr = process.communicate(timeout=15)
			pytest.fail(f"workspace subprocess did not exit after release: {stderr}")

	assert process.returncode == 0, f"workspace subprocess failed: {stderr}"
	assert active_workspace is not None
	assert not active_workspace.exists()


def test_read_only_repository_uses_a_per_repository_system_temp_fallback(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repository = tmp_path / "readonly-repository"
	repository.mkdir()
	system_temp = tmp_path / "system-temp"
	system_temp.mkdir()
	monkeypatch.setattr(temp_workspace, "SYSTEM_TEMP_DIRECTORY", system_temp)
	prepare_workspace = temp_workspace._prepare_workspace
	attempted_roots: list[Path] = []

	def reject_read_only_root(root: Path) -> tuple[Path, Path, BinaryIO]:
		attempted_roots.append(root)
		if root == repository.resolve():
			error = TemporaryWorkspaceError("repository temp is not writable")
			raise error from PermissionError(errno.EACCES, "read-only repository")
		return prepare_workspace(root)

	monkeypatch.setattr(temp_workspace, "_prepare_workspace", reject_read_only_root)
	fallback_root = temp_workspace._fallback_workspace_root(repository.resolve())
	with temporary_workspace(repository) as workspace:
		assert workspace.parent == fallback_root / "temp"
		assert workspace.is_dir()
		with temporary_workspace(repository) as nested_workspace:
			assert nested_workspace == workspace

	assert not workspace.exists()
	assert attempted_roots == [repository.resolve(), fallback_root]
