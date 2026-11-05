from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import quality_gate.distribution as distribution
import quality_gate.edit_cli as edit_cli
import quality_gate.temp_workspace as temp_workspace
from quality_gate.edit_protocol import EditError
from quality_gate.release import _workspace_parents
from quality_gate.temp_workspace import SYSTEM_TEMP_DIRECTORY, temporary_workspace

_SECOND_NESTED_LSTAT_CALL = 2
_SINGLE_LINK_COUNT = 1
_TRANSITIONAL_HARD_LINK_COUNT = 2
_INTERRUPTED_KEY_EXIT = 73
_INTERRUPTED_CLEANUP_EXIT = 74
_INTERRUPTED_AFTER_UNLINK_EXIT = 75


def _run_workspace_script(
	script: str, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[bytes]:
	return subprocess.run(
		[sys.executable, "-c", script, *arguments],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		capture_output=True,
		timeout=30,
		check=False,
	)


def _start_workspace_script(
	script: str, environment: dict[str, str], *arguments: str
) -> subprocess.Popen[bytes]:
	return subprocess.Popen(
		[sys.executable, "-c", script, *arguments],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)


def _wait_for_workspace_marker(
	marker: Path, process: subprocess.Popen[bytes], timeout_seconds: float = 30
) -> None:
	deadline = time.monotonic() + timeout_seconds
	while not marker.exists() and time.monotonic() < deadline:
		if process.poll() is not None:
			break
		time.sleep(0.01)
	assert marker.exists(), f"process did not reach marker {marker.name}"


def _collect_workspace_process(
	process: subprocess.Popen[bytes], timeout_seconds: float = 30
) -> tuple[bytes, bytes]:
	try:
		stdout, stderr = process.communicate(timeout=timeout_seconds)
	except subprocess.TimeoutExpired:
		process.kill()
		stdout, stderr = process.communicate(timeout=15)
	return stdout, stderr


def _assert_workspace_script_cleaned(result: subprocess.CompletedProcess[bytes]) -> Path:
	assert result.returncode == 0, result.stderr
	workspace = Path(result.stdout.decode().strip())
	assert not workspace.exists()
	return workspace


def _apply_publisher_race_final_action(
	final_action: str,
	key_path: Path,
	staging_path: Path,
	key_identity: os.stat_result,
	tmp_path: Path,
) -> tuple[Path | None, Path | None]:
	if final_action == "replaced":
		foreign_key = key_path.with_name("foreign-key")
		foreign_key.write_bytes(b"f" * temp_workspace._OWNERSHIP_KEY_BYTES)
		foreign_key.chmod(temp_workspace._OWNERSHIP_KEY_MODE)
		os.replace(foreign_key, key_path)
		assert not temp_workspace._same_file_identity(key_identity, key_path.lstat())
		return foreign_key, None
	if final_action == "missing":
		key_path.unlink()
		return None, None
	if final_action == "staging-replaced":
		foreign_target = tmp_path / "missing-replacement-target"
		staging_path.symlink_to(foreign_target)
		return None, foreign_target
	return None, None


def _publisher_race_script() -> str:
	return "\n".join(
		(
			"import errno, os, sys, time",
			"from pathlib import Path",
			"import quality_gate.temp_workspace as workspace",
			"import quality_gate.workspace_keys as workspace_keys",
			"root, coordination = map(Path, sys.argv[1:3])",
			"pause_after = int(sys.argv[3])",
			"staging_statted = coordination / 'publisher-staging-statted'",
			"resume_publisher = coordination / 'resume-publisher'",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace_keys._rename_without_replacement = unsupported",
			"original_stat = os.stat",
			"staging_stat_count = [0]",
			"def pause_after_staging_stat(path, *args, **kwargs):",
			"    metadata = original_stat(path, *args, **kwargs)",
			"    is_staging_stat = (",
			"        isinstance(path, str)",
			"        and path.startswith('.quality-gate-pending-')",
			"        and kwargs.get('dir_fd') is not None",
			"        and kwargs.get('follow_symlinks') is False",
			"    )",
			"    if is_staging_stat:",
			"        staging_stat_count[0] += 1",
			"    if is_staging_stat and staging_stat_count[0] == pause_after:",
			"        staging_statted.touch()",
			"        deadline = time.monotonic() + 30",
			"        while not resume_publisher.exists() and time.monotonic() < deadline:",
			"            time.sleep(0.01)",
			"        if not resume_publisher.exists():",
			"            os._exit(88)",
			"    return metadata",
			"workspace.os.stat = pause_after_staging_stat",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(root) as work:",
			"    (work / 'publisher').write_bytes(b'published')",
		)
	)


def _recovery_reader_script() -> str:
	return "\n".join(
		(
			"import sys",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(Path(sys.argv[1])) as work:",
			"    (work / 'reader').write_bytes(b'recovered')",
			"    print(work, flush=True)",
		)
	)


def _recover_while_publisher_paused(
	staging_statted: Path,
	publisher: subprocess.Popen[bytes],
	state: Path,
	environment: dict[str, str],
	repository: Path,
	final_action: str,
	tmp_path: Path,
) -> tuple[Path, os.stat_result, bytes, Path, Path | None, Path | None]:
	_wait_for_workspace_marker(staging_statted, publisher, timeout_seconds=25)
	key_path = state / temp_workspace._OWNERSHIP_KEY_NAME
	key_identity = key_path.lstat()
	staging_paths = list(state.glob(".quality-gate-pending-*"))
	assert len(staging_paths) == 1
	staging_path = staging_paths[0]
	assert temp_workspace._same_file_identity(key_identity, staging_path.lstat())
	assert key_identity.st_nlink == _TRANSITIONAL_HARD_LINK_COUNT
	key_bytes = key_path.read_bytes()
	reader = _run_workspace_script(_recovery_reader_script(), environment, str(repository))
	_assert_workspace_script_cleaned(reader)
	assert not staging_path.exists()
	assert temp_workspace._same_file_identity(key_identity, key_path.lstat())
	assert key_path.read_bytes() == key_bytes
	assert key_path.lstat().st_nlink == _SINGLE_LINK_COUNT
	foreign_key, foreign_target = _apply_publisher_race_final_action(
		final_action, key_path, staging_path, key_identity, tmp_path
	)
	return key_path, key_identity, key_bytes, staging_path, foreign_key, foreign_target


def _assert_publisher_race_outcome(
	final_action: str,
	publisher: subprocess.Popen[bytes],
	publisher_stdout: bytes,
	publisher_stderr: bytes,
	state: Path,
	environment: dict[str, str],
	repository: Path,
	key_path: Path,
	key_identity: os.stat_result,
	key_bytes: bytes,
	staging_path: Path,
	foreign_key: Path | None,
	foreign_target: Path | None,
) -> None:
	if final_action in {"intact", "staging-replaced"}:
		assert publisher.returncode == 0, publisher_stderr
		assert temp_workspace._same_file_identity(key_identity, key_path.lstat())
		assert key_path.read_bytes() == key_bytes
		assert key_path.lstat().st_nlink == _SINGLE_LINK_COUNT
		if final_action == "staging-replaced":
			assert foreign_target is not None
			assert staging_path.is_symlink()
			assert os.readlink(staging_path) == str(foreign_target)
			assert not foreign_target.exists()
		return
	assert publisher.returncode != 0, publisher_stdout
	assert not list(state.glob(".quality-gate-pending-*"))
	if final_action == "replaced":
		assert key_path.read_bytes() == b"f" * temp_workspace._OWNERSHIP_KEY_BYTES
		assert foreign_key is not None and not foreign_key.exists()
		return
	assert not key_path.exists()
	fresh = _run_workspace_script(_recovery_reader_script(), environment, str(repository))
	_assert_workspace_script_cleaned(fresh)


@pytest.mark.parametrize("mode", ["ruff", "validate", "secrets", "integrity", "types"])
@pytest.mark.parametrize("worker_raises", [False, True])
def test_edit_worker_uses_and_cleans_managed_project_temp(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, worker_raises: bool
) -> None:
	project = tmp_path / "project"
	project.mkdir()
	workspaces: list[Path] = []

	def run_worker(
		*_arguments: object, root: Path, environment: dict[str, str], **_options: object
	) -> tuple[int, bytes, bytes]:
		workspaces.append(root)
		assert root.parent == project / "temp"
		assert root.name.startswith("qg-")
		assert environment["TEMP"] == str(root)
		(root / "source").mkdir()
		(root / "source" / "owned.py").write_text("VALUE = 1\n", encoding="utf-8")
		if worker_raises:
			raise EditError("deadline")
		response = {"schema": 1, "status": "unchecked", "reason": "deadline", "files": []}
		if mode == "validate":
			response.update(phase="validate", writes_may_have_occurred=False)
		return 2, json.dumps(response).encode(), b""

	monkeypatch.setattr(edit_cli, "run_process", run_worker)
	request = {"root": str(project), "schema": 1}
	if mode == "validate":
		request["phase"] = "validate"
	arguments = {
		"secrets": mode == "secrets",
		"types": mode == "types",
		"integrity": mode == "integrity",
	}
	if worker_raises:
		with pytest.raises(EditError, match="deadline"):
			edit_cli._run_worker(request, b"{}", time.monotonic() + 10, **arguments)
	else:
		result = edit_cli._run_worker(request, b"{}", time.monotonic() + 10, **arguments)
		assert result["reason"] == "deadline"
	assert len(workspaces) == 1
	assert not workspaces[0].exists()
	assert not list(project.glob(".quality-gate-edit-*"))


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

	temp_root = repository / "temp"
	foreign_file = temp_root / "qg-foreign-file"
	foreign_directory = temp_root / "qg-user-candidate"
	legacy_directory = temp_root / ".quality-gate-edit-legacy"
	foreign_file.parent.mkdir(parents=True)
	foreign_file.write_text("keep", encoding="utf-8")
	foreign_directory.mkdir()
	(foreign_directory / "candidate.py").write_text("keep", encoding="utf-8")
	(foreign_directory / ".quality-gate-workspace").write_text(
		'{"format":"quality-gate-temp-workspace","root":"elsewhere",'
		'"run":"qg-user-candidate","schema":1}\n',
		encoding="utf-8",
	)
	legacy_directory.mkdir()
	(legacy_directory / "candidate.py").write_text("keep", encoding="utf-8")
	workspace: Path | None = None
	with pytest.raises(RuntimeError, match="stop after checking cleanup"):
		with temporary_workspace(repository) as active_workspace:
			workspace = active_workspace
			assert active_workspace.parent == repository / "temp"
			assert active_workspace.is_dir()
			assert foreign_file.read_text(encoding="utf-8") == "keep"
			assert (foreign_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
			assert (legacy_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
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
	assert not list(temp_root.glob(".quality-gate-owner-*"))
	assert foreign_file.read_text(encoding="utf-8") == "keep"
	assert (foreign_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
	assert (legacy_directory / "candidate.py").read_text(encoding="utf-8") == "keep"
	assert (temp_root / ".quality-gate.lock").is_file()
	assert tempfile.tempdir == str(system_temp)
	assert {key: os.environ[key] for key in previous_environment} == previous_environment
	assert "QUALITY_GATE_SYSTEM_TEMP" not in os.environ


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only file attributes only")
def test_temporary_workspace_removes_read_only_git_objects(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	with temporary_workspace(repository) as workspace:
		object_file = workspace / "pytest" / "repository" / ".git" / "objects" / "aa" / "object"
		object_file.parent.mkdir(parents=True)
		object_file.write_bytes(b"git object")
		object_file.chmod(stat.S_IREAD)

	assert not workspace.exists()


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


def test_temporary_workspace_preserves_an_active_process_and_misleading_entries(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	temp_root = repository / "temp"
	misleading_directory = temp_root / "qg-stale-run"
	misleading_directory.mkdir(parents=True)
	(misleading_directory / "old-file").write_text("preserve", encoding="utf-8")
	misleading_file = temp_root / "qg-stale-file"
	misleading_file.write_text("preserve", encoding="utf-8")
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
		(active_workspace / "candidate").mkdir()
		(active_workspace / "candidate" / "module.py").write_text(
			"value: int = 1\n", encoding="utf-8"
		)

		with temporary_workspace(repository) as current_workspace:
			assert current_workspace != active_workspace
			assert active_workspace.is_dir()
			assert (active_workspace / "candidate" / "module.py").read_text(
				encoding="utf-8"
			) == "value: int = 1\n"
			assert (misleading_directory / "old-file").read_text(encoding="utf-8") == "preserve"
			assert misleading_file.read_text(encoding="utf-8") == "preserve"
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
	assert (misleading_directory / "old-file").read_text(encoding="utf-8") == "preserve"
	assert misleading_file.read_text(encoding="utf-8") == "preserve"


def test_temporary_workspace_preserves_qg_directory_with_forged_valid_marker(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	foreign = temp_root / "qg-user-owned"
	foreign.mkdir(parents=True)
	(foreign / ".active.lock").write_bytes(b"\0")
	(foreign / ".quality-gate-workspace").write_bytes(
		temp_workspace._ownership_marker(temp_root, foreign)
	)
	(foreign / "candidate.py").write_text("keep", encoding="utf-8")

	with temporary_workspace(repository):
		assert (foreign / "candidate.py").read_text(encoding="utf-8") == "keep"

	assert foreign.is_dir()
	assert (foreign / "candidate.py").read_text(encoding="utf-8") == "keep"
