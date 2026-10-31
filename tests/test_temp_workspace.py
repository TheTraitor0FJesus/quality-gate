from __future__ import annotations

import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast

import pytest

import quality_gate.distribution as distribution
import quality_gate.edit_cli as edit_cli
import quality_gate.temp_workspace as temp_workspace
from quality_gate.edit_protocol import EXIT_UNCHECKED, EditError
from quality_gate.release import _workspace_parents
from quality_gate.temp_workspace import (
	SYSTEM_TEMP_DIRECTORY,
	TemporaryWorkspaceError,
	temporary_workspace,
)

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
			"root, coordination = map(Path, sys.argv[1:3])",
			"pause_after = int(sys.argv[3])",
			"staging_statted = coordination / 'publisher-staging-statted'",
			"resume_publisher = coordination / 'resume-publisher'",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace._rename_without_replacement = unsupported",
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


def test_temporary_workspace_reclaims_owned_run_after_process_interruption(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	ready = coordination / "ready"
	script = "\n".join(
		(
			"import sys",
			"import time",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"root, ready = Path(sys.argv[1]), Path(sys.argv[2])",
			"with temporary_workspace(root) as workspace:",
			"    child = workspace / 'candidate'",
			"    child.mkdir()",
			"    (child / 'module.py').write_text('value = 1\\n', encoding='utf-8')",
			"    ready.write_text(str(workspace), encoding='utf-8')",
			"    while True:",
			"        time.sleep(1)",
		)
	)
	process = subprocess.Popen(
		[sys.executable, "-c", script, str(repository), str(ready)],
		cwd=Path(__file__).resolve().parents[1],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
		text=True,
	)
	owned_workspace: Path | None = None
	try:
		deadline = time.monotonic() + 15
		while not ready.exists() and time.monotonic() < deadline:
			if process.poll() is not None:
				_, stderr = process.communicate()
				pytest.fail(f"workspace subprocess exited early: {stderr}")
			time.sleep(0.01)
		assert ready.exists(), "workspace subprocess did not become active"
		owned_workspace = Path(ready.read_text(encoding="utf-8"))
		assert (owned_workspace / ".quality-gate-workspace").is_file()
		assert (owned_workspace / "candidate" / "module.py").exists()
		process.kill()
		process.communicate(timeout=15)
		assert process.returncode != 0

		with temporary_workspace(repository) as current_workspace:
			assert current_workspace != owned_workspace
			assert not owned_workspace.exists()
			assert current_workspace.is_dir()
	finally:
		if process.poll() is None:
			process.kill()
			process.communicate(timeout=15)
	assert owned_workspace is not None
	assert not owned_workspace.exists()


def test_missing_marker_preserves_ambiguous_workspace_and_allows_next_run(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(temp_workspace, "_user_state_directory", lambda: tmp_path / "state")
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	stale, _, active_lock, _, _ = temp_workspace._create_run_directory(temp_root)
	(stale / ".quality-gate-workspace").unlink()
	(stale / "foreign").write_bytes(b"preserve")
	temp_workspace._unlock(active_lock)

	with temporary_workspace(repository) as workspace:
		assert workspace != stale
		(workspace / "usable").write_bytes(b"ok")
		assert (stale / "foreign").read_bytes() == b"preserve"
	assert (stale / "foreign").read_bytes() == b"preserve"


def test_forged_cleanup_record_cannot_replace_missing_marker(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(temp_workspace, "_user_state_directory", lambda: tmp_path / "state")
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	stale, _, active_lock, _, _ = temp_workspace._create_run_directory(temp_root)
	(stale / ".quality-gate-workspace").unlink()
	(stale / "foreign").write_bytes(b"preserve")
	forged = temp_workspace._cleanup_record_path(temp_root, stale)
	forged.write_bytes(b'{"signature":"forged"}')
	temp_workspace._unlock(active_lock)
	with temporary_workspace(repository) as workspace:
		assert workspace.is_dir()
		assert (stale / "foreign").read_bytes() == b"preserve"
	assert forged.read_bytes() == b'{"signature":"forged"}'


def test_partial_cleanup_record_does_not_strand_owned_workspace(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(temp_workspace, "_user_state_directory", lambda: tmp_path / "state")
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	stale, _, active_lock, _, _ = temp_workspace._create_run_directory(temp_root)
	(stale / "owned").write_bytes(b"owned")
	partial = temp_workspace._cleanup_record_path(temp_root, stale)
	partial.write_bytes(b'{"format":')
	temp_workspace._unlock(active_lock)

	with temporary_workspace(repository) as workspace:
		assert workspace.is_dir()
		assert not stale.exists()
	assert partial.read_bytes() == b'{"format":'


def test_temporary_workspace_reclaims_owned_run_with_dangling_cleanup_symlink(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(temp_workspace, "_user_state_directory", lambda: tmp_path / "state")
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	stale, _, active_lock, _, _ = temp_workspace._create_run_directory(temp_root)
	(stale / "owned").write_bytes(b"owned")
	cleanup_intent = temp_workspace._cleanup_record_path(temp_root, stale)
	foreign_target = tmp_path / "missing-foreign-cleanup-target"
	try:
		cleanup_intent.symlink_to(foreign_target)
	except OSError as error:
		pytest.skip(f"file symlink creation is unavailable: {error}")
	link_target = os.readlink(cleanup_intent)
	temp_workspace._unlock(active_lock)

	with temporary_workspace(repository) as workspace:
		assert workspace != stale
		assert workspace.is_dir()
		assert not stale.exists()
		assert cleanup_intent.is_symlink()
		assert os.readlink(cleanup_intent) == link_target
		assert not foreign_target.exists()
		(workspace / "fresh").write_bytes(b"usable")
		assert (workspace / "fresh").read_bytes() == b"usable"

	assert not workspace.exists()
	assert cleanup_intent.is_symlink()
	assert os.readlink(cleanup_intent) == link_target
	assert not foreign_target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link fallback only")
@pytest.mark.parametrize(
	"rename_error",
	[
		pytest.param(OSError("safe recovery is unavailable"), id="unsupported-platform"),
		pytest.param(AttributeError("no no-replace rename"), id="missing-symbol"),
		pytest.param(OSError(errno.ENOSYS, "no no-replace rename"), id="enosys"),
		pytest.param(OSError(errno.EINVAL, "no no-replace rename"), id="einval"),
	],
)
def test_publish_new_file_uses_hard_link_fallback_and_cleans_temporary(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rename_error: Exception
) -> None:
	target = tmp_path / "published-key"
	content = b"k" * temp_workspace._OWNERSHIP_KEY_BYTES

	def unavailable_rename(*_args: object) -> None:
		raise rename_error

	monkeypatch.setattr(temp_workspace, "_rename_without_replacement", unavailable_rename)
	temp_workspace._publish_new_file(target, content)

	metadata = target.lstat()
	assert metadata.st_nlink == _SINGLE_LINK_COUNT
	assert temp_workspace._plain_lock_file(target, metadata)
	assert not list(tmp_path.glob(".quality-gate-pending-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link fallback only")
def test_plain_lock_file_retries_after_hard_link_transition(tmp_path: Path) -> None:
	target = tmp_path / "published-key"
	temporary = tmp_path / ".quality-gate-pending-test"
	content = b"k" * temp_workspace._OWNERSHIP_KEY_BYTES
	target.write_bytes(content)
	os.link(target, temporary)
	metadata = target.lstat()
	assert metadata.st_nlink == _TRANSITIONAL_HARD_LINK_COUNT
	result: list[bool] = []
	reader_started = threading.Event()

	def read_plain_file() -> None:
		reader_started.set()
		result.append(temp_workspace._plain_lock_file(target, metadata))

	reader = threading.Thread(target=read_plain_file)
	reader.start()
	assert reader_started.wait(timeout=5)
	temporary.unlink()
	reader.join(timeout=5)
	assert not reader.is_alive()
	assert result == [True]
	assert temp_workspace._read_ownership_key(target) == content
	assert target.lstat().st_nlink == _SINGLE_LINK_COUNT


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link fallback only")
def test_publish_new_file_hard_link_fallback_preserves_occupied_target(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	target = tmp_path / "occupied"
	target.write_bytes(b"foreign")

	def unsupported_rename(*_args: object) -> None:
		raise OSError(errno.ENOSYS, "no no-replace rename")

	monkeypatch.setattr(temp_workspace, "_rename_without_replacement", unsupported_rename)
	with pytest.raises(FileExistsError):
		temp_workspace._publish_new_file(target, b"replacement")

	assert target.read_bytes() == b"foreign"
	assert not list(tmp_path.glob(".quality-gate-pending-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX cleanup areas only")
def test_changed_state_key_keeps_previous_cleanup_area_and_allows_cleanup(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-a"))
	with temporary_workspace(repository) as first:
		(first / "a").write_bytes(b"a")
	areas = list((repository / "temp").glob(".quality-gate-cleanup-*"))
	assert len(areas) == 1
	area = areas[0]
	(area / "foreign-data").write_bytes(b"preserve")
	previous = {item.name: item.read_bytes() for item in area.iterdir() if item.is_file()}
	stale, _, active_lock, _, _ = temp_workspace._create_run_directory(repository / "temp")
	(stale / "owned").write_bytes(b"old key")
	temp_workspace._unlock(active_lock)
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-b"))
	with temporary_workspace(repository) as second:
		(second / "b").write_bytes(b"b")
		assert (stale / "owned").read_bytes() == b"old key"
	assert not second.exists()
	assert previous == {item.name: item.read_bytes() for item in area.iterdir() if item.is_file()}
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-a"))
	with temporary_workspace(repository):
		assert not stale.exists()
	assert (area / "foreign-data").read_bytes() == b"preserve"


def test_interrupted_key_publication_allows_next_workspace(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	environment = os.environ.copy()
	state_base = tmp_path / "state-base"
	environment["LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"] = str(state_base)
	script = "\n".join(
		(
			"import os",
			"import quality_gate.temp_workspace as workspace",
			f"def stop(*args, **kwargs): os._exit({_INTERRUPTED_KEY_EXIT})",
			"workspace.os.rename = stop"
			if os.name == "nt"
			else "workspace._rename_without_replacement = stop",
			"workspace._ownership_key(create=True)",
		)
	)
	process = subprocess.run(
		[sys.executable, "-c", script],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		capture_output=True,
		timeout=30,
		check=False,
	)
	assert process.returncode == _INTERRUPTED_KEY_EXIT, process.stderr
	state = state_base / "quality-gate" / "temp-workspace"
	assert not (state / "temp-workspace.key").exists()
	script = "\n".join(
		(
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			f"with temporary_workspace(Path({str(repository)!r})) as work:",
			"    (work / 'usable').write_bytes(b'ok')",
		)
	)
	process = subprocess.run(
		[sys.executable, "-c", script],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		capture_output=True,
		timeout=30,
		check=False,
	)
	assert process.returncode == 0, process.stderr
	assert (state / "temp-workspace.key").stat().st_size == temp_workspace._OWNERSHIP_KEY_BYTES


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link publication recovery only")
def test_interrupted_hard_link_key_publication_recovers_through_workspace(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	state_base = tmp_path / "state-base"
	environment = os.environ.copy()
	environment["XDG_STATE_HOME"] = str(state_base)
	script = "\n".join(
		(
			"import errno, os",
			"import quality_gate.temp_workspace as workspace",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace._rename_without_replacement = unsupported",
			"real_link = os.link",
			"def link_then_exit(*args, **kwargs):",
			"    real_link(*args, **kwargs)",
			f"    os._exit({_INTERRUPTED_KEY_EXIT})",
			"workspace.os.link = link_then_exit",
			"workspace._ownership_key(create=True)",
		)
	)
	process = subprocess.run(
		[sys.executable, "-c", script],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		capture_output=True,
		timeout=30,
		check=False,
	)
	assert process.returncode == _INTERRUPTED_KEY_EXIT, process.stderr
	state = state_base / "quality-gate" / "temp-workspace"
	key_path = state / temp_workspace._OWNERSHIP_KEY_NAME
	staging_paths = list(state.glob(".quality-gate-pending-*"))
	assert len(staging_paths) == 1
	staging_path = staging_paths[0]
	key_identity = key_path.lstat()
	staging_identity = staging_path.lstat()
	key_bytes = key_path.read_bytes()
	assert len(key_bytes) == temp_workspace._OWNERSHIP_KEY_BYTES
	assert temp_workspace._same_file_identity(key_identity, staging_identity)
	assert key_identity.st_nlink == _TRANSITIONAL_HARD_LINK_COUNT

	foreign_link = state / "foreign-key-link"
	os.link(key_path, foreign_link)
	assert key_path.lstat().st_nlink == _TRANSITIONAL_HARD_LINK_COUNT + 1
	workspace_script = "\n".join(
		(
			"import sys",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(Path(sys.argv[1])) as work:",
			"    (work / 'fresh').write_bytes(b'usable')",
			"    print(work, flush=True)",
		)
	)
	refused = _run_workspace_script(workspace_script, environment, str(repository))
	assert refused.returncode != 0, refused.stdout
	assert b"temporary workspace ownership key is unsafe" in refused.stderr
	assert staging_path.exists()
	assert foreign_link.exists()
	assert key_path.read_bytes() == key_bytes
	foreign_link.unlink()

	unrelated_pending = state / ".quality-gate-pending-unrelated"
	unrelated_pending.write_bytes(b"foreign staging data")
	foreign_target = tmp_path / "missing-foreign-target"
	foreign_symlink = state / ".quality-gate-pending-foreign-link"
	foreign_symlink.symlink_to(foreign_target)
	symlink_target = os.readlink(foreign_symlink)
	created = _run_workspace_script(workspace_script, environment, str(repository))
	_assert_workspace_script_cleaned(created)
	current_key_identity = key_path.lstat()
	assert temp_workspace._same_file_identity(key_identity, current_key_identity)
	assert key_path.read_bytes() == key_bytes
	assert current_key_identity.st_nlink == _SINGLE_LINK_COUNT
	assert not staging_path.exists()
	assert unrelated_pending.read_bytes() == b"foreign staging data"
	assert foreign_symlink.is_symlink()
	assert os.readlink(foreign_symlink) == symlink_target
	assert not foreign_target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link publication only")
def test_interrupted_after_staging_unlink_keeps_published_key_usable(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	state_base = tmp_path / "state-base"
	state = state_base / "quality-gate" / "temp-workspace"
	environment = os.environ.copy()
	environment["XDG_STATE_HOME"] = str(state_base)
	script = "\n".join(
		(
			"import errno, os, stat, sys",
			"from pathlib import Path",
			"import quality_gate.temp_workspace as workspace",
			"state = Path(sys.argv[1])",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace._rename_without_replacement = unsupported",
			"real_fsync = os.fsync",
			"def stop_after_staging_unlink(fd):",
			"    metadata = os.fstat(fd)",
			"    if (",
			"        stat.S_ISDIR(metadata.st_mode)",
			"        and workspace._same_file_identity(metadata, state.lstat())",
			"    ):",
			"        if not list(state.glob('.quality-gate-pending-*')):",
			f"            os._exit({_INTERRUPTED_AFTER_UNLINK_EXIT})",
			"    return real_fsync(fd)",
			"workspace.os.fsync = stop_after_staging_unlink",
			"workspace._ownership_key(create=True)",
		)
	)
	process = _run_workspace_script(script, environment, str(state))
	assert process.returncode == _INTERRUPTED_AFTER_UNLINK_EXIT, process.stderr
	key_path = state / temp_workspace._OWNERSHIP_KEY_NAME
	key_identity = key_path.lstat()
	key_bytes = key_path.read_bytes()
	assert len(key_bytes) == temp_workspace._OWNERSHIP_KEY_BYTES
	assert key_identity.st_nlink == _SINGLE_LINK_COUNT
	assert not list(state.glob(".quality-gate-pending-*"))

	workspace_script = "\n".join(
		(
			"import sys",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(Path(sys.argv[1])) as work:",
			"    (work / 'usable').write_bytes(b'ok')",
			"    print(work, flush=True)",
		)
	)
	workspace_result = _run_workspace_script(workspace_script, environment, str(repository))
	_assert_workspace_script_cleaned(workspace_result)
	assert temp_workspace._same_file_identity(key_identity, key_path.lstat())
	assert key_path.read_bytes() == key_bytes
	assert key_path.lstat().st_nlink == _SINGLE_LINK_COUNT


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link publication recovery only")
@pytest.mark.parametrize("final_action", ["intact", "staging-replaced", "replaced", "missing"])
@pytest.mark.parametrize("staging_stat_number", [1, 2])
def test_concurrent_key_recovery_and_publisher_cleanup_race(
	tmp_path: Path, final_action: str, staging_stat_number: int
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	state_base = tmp_path / "state-base"
	state = state_base / "quality-gate" / "temp-workspace"
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	staging_statted = coordination / "publisher-staging-statted"
	resume_publisher = coordination / "resume-publisher"
	environment = os.environ.copy()
	environment["XDG_STATE_HOME"] = str(state_base)
	publisher = _start_workspace_script(
		_publisher_race_script(),
		environment,
		str(repository),
		str(coordination),
		str(staging_stat_number),
	)
	key_path: Path
	key_identity: os.stat_result
	key_bytes: bytes
	staging_path: Path
	foreign_key: Path | None
	foreign_target: Path | None
	try:
		(
			key_path,
			key_identity,
			key_bytes,
			staging_path,
			foreign_key,
			foreign_target,
		) = _recover_while_publisher_paused(
			staging_statted,
			publisher,
			state,
			environment,
			repository,
			final_action,
			tmp_path,
		)
	finally:
		resume_publisher.touch(exist_ok=True)
		publisher_stdout, publisher_stderr = _collect_workspace_process(publisher)
	_assert_publisher_race_outcome(
		final_action,
		publisher,
		publisher_stdout,
		publisher_stderr,
		state,
		environment,
		repository,
		key_path,
		key_identity,
		key_bytes,
		staging_path,
		foreign_key,
		foreign_target,
	)


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link recovery only")
def test_concurrent_public_recovery_readers_share_staging_removal(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	state_base = tmp_path / "state-base"
	state = state_base / "quality-gate" / "temp-workspace"
	state.mkdir(mode=0o700, parents=True)
	key_path = state / temp_workspace._OWNERSHIP_KEY_NAME
	key_bytes = b"k" * temp_workspace._OWNERSHIP_KEY_BYTES
	key_path.write_bytes(key_bytes)
	key_path.chmod(temp_workspace._OWNERSHIP_KEY_MODE)
	staging_path = state / ".quality-gate-pending-orphan"
	os.link(key_path, staging_path)
	key_identity = key_path.lstat()
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	first_ready = coordination / "first-ready"
	second_ready = coordination / "second-ready"
	release = coordination / "release"
	environment = os.environ.copy()
	environment["XDG_STATE_HOME"] = str(state_base)
	script = "\n".join(
		(
			"import os, sys, time",
			"from pathlib import Path",
			"import quality_gate.temp_workspace as workspace",
			"worker, root, coordination = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])",
			"original_unlink = os.unlink",
			"def ordered_unlink(path, *args, **kwargs):",
			"    is_staging = isinstance(path, str) and path.startswith('.quality-gate-pending-')",
			"    if is_staging and kwargs.get('dir_fd') is not None:",
			"        (coordination / f'{worker}-ready').touch()",
			"        deadline = time.monotonic() + 30",
			"        while (",
			"            not (coordination / 'release').exists()",
			"            and time.monotonic() < deadline",
			"        ):",
			"            time.sleep(0.01)",
			"        if not (coordination / 'release').exists():",
			"            os._exit(88)",
			"        if worker == 'second':",
			"            while (",
			"                not (coordination / 'first-unlinked').exists()",
			"                and time.monotonic() < deadline",
			"            ):",
			"                time.sleep(0.01)",
			"            if not (coordination / 'first-unlinked').exists():",
			"                os._exit(89)",
			"    result = original_unlink(path, *args, **kwargs)",
			"    if is_staging and worker == 'first':",
			"        (coordination / 'first-unlinked').touch()",
			"    return result",
			"workspace.os.unlink = ordered_unlink",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(root) as work:",
			"    (work / worker).write_bytes(b'ok')",
			"    print(work, flush=True)",
		)
	)
	processes = [
		_start_workspace_script(script, environment, worker, str(repository), str(coordination))
		for worker in ("first", "second")
	]
	try:
		_wait_for_workspace_marker(first_ready, processes[0], timeout_seconds=25)
		_wait_for_workspace_marker(second_ready, processes[1], timeout_seconds=25)
	finally:
		release.touch(exist_ok=True)
		outputs = [_collect_workspace_process(process) for process in processes]

	assert all(process.returncode == 0 for process in processes), outputs
	assert all(not Path(stdout.decode().strip()).exists() for stdout, _stderr in outputs)
	assert not staging_path.exists()
	assert temp_workspace._same_file_identity(key_identity, key_path.lstat())
	assert key_path.read_bytes() == key_bytes
	assert key_path.lstat().st_nlink == _SINGLE_LINK_COUNT


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link publication only")
def test_concurrent_workspace_can_read_key_during_slow_directory_fsync(
	tmp_path: Path,
) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	state_base = tmp_path / "state-base"
	state = state_base / "quality-gate" / "temp-workspace"
	state.mkdir(mode=0o700, parents=True)
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	entered_fsync = coordination / "entered-fsync"
	release_fsync = coordination / "release-fsync"
	environment = os.environ.copy()
	environment["XDG_STATE_HOME"] = str(state_base)
	publisher_script = "\n".join(
		(
			"import errno, os, stat, sys, time",
			"from pathlib import Path",
			"import quality_gate.temp_workspace as workspace",
			"root, state, entered, release = map(Path, sys.argv[1:])",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace._rename_without_replacement = unsupported",
			"real_fsync = os.fsync",
			"def slow_directory_fsync(fd):",
			"    metadata = os.fstat(fd)",
			"    if (",
			"        stat.S_ISDIR(metadata.st_mode)",
			"        and workspace._same_file_identity(metadata, state.lstat())",
			"    ):",
			"        entered.touch()",
			"        deadline = time.monotonic() + 20",
			"        while not release.exists() and time.monotonic() < deadline:",
			"            time.sleep(0.01)",
			"        if not release.exists():",
			"            os._exit(88)",
			"    return real_fsync(fd)",
			"workspace.os.fsync = slow_directory_fsync",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(root) as work:",
			"    (work / 'publisher').write_bytes(b'ok')",
		)
	)
	publisher = subprocess.Popen(
		[
			sys.executable,
			"-c",
			publisher_script,
			str(repository),
			str(state),
			str(entered_fsync),
			str(release_fsync),
		],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
	)
	try:
		deadline = time.monotonic() + 25
		while not entered_fsync.exists() and time.monotonic() < deadline:
			if publisher.poll() is not None:
				break
			time.sleep(0.01)
		assert entered_fsync.exists(), "publisher did not reach the state-directory fsync"
		key_path = state / temp_workspace._OWNERSHIP_KEY_NAME
		assert key_path.lstat().st_nlink == _SINGLE_LINK_COUNT
		workspace_script = "\n".join(
			(
				"import sys",
				"from pathlib import Path",
				"from quality_gate.temp_workspace import temporary_workspace",
				"with temporary_workspace(Path(sys.argv[1])) as work:",
				"    (work / 'concurrent-reader').write_bytes(b'ok')",
			)
		)
		reader = subprocess.run(
			[sys.executable, "-c", workspace_script, str(repository)],
			cwd=Path(__file__).resolve().parents[1],
			env=environment,
			capture_output=True,
			timeout=30,
			check=False,
		)
		assert reader.returncode == 0, reader.stderr
	finally:
		release_fsync.touch(exist_ok=True)
		try:
			stdout, stderr = publisher.communicate(timeout=30)
		except subprocess.TimeoutExpired:
			publisher.kill()
			stdout, stderr = publisher.communicate(timeout=15)
	assert publisher.returncode == 0, (stdout, stderr)


def test_concurrent_first_workspaces_share_one_complete_key(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	coordination = tmp_path / "coordination"
	coordination.mkdir()
	environment = os.environ.copy()
	environment["LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"] = str(tmp_path / "state")
	script = "\n".join(
		(
			"import sys, time",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace, _ownership_key",
			"root, coordination, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]",
			"(coordination / name).touch()",
			"while not (coordination / 'start').exists(): time.sleep(0.01)",
			"with temporary_workspace(root) as work:",
			"    (work / 'usable').write_bytes(b'ok')",
			"    print(_ownership_key(create=False).hex(), flush=True)",
		)
	)
	processes = [
		subprocess.Popen(
			[sys.executable, "-c", script, str(repository), str(coordination), str(index)],
			cwd=Path(__file__).resolve().parents[1],
			env=environment,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
		)
		for index in range(4)
	]
	try:
		deadline = time.monotonic() + 30
		while len(list(coordination.iterdir())) != len(processes) and time.monotonic() < deadline:
			time.sleep(0.01)
		assert len(list(coordination.iterdir())) == len(processes)
		(coordination / "start").touch()
		results = [process.communicate(timeout=60) for process in processes]
		assert all(process.returncode == 0 for process in processes), results
		keys = [stdout.strip() for stdout, _stderr in results]
		assert len(set(keys)) == 1 and len(keys[0]) == 2 * temp_workspace._OWNERSHIP_KEY_BYTES
	finally:
		for process in processes:
			if process.poll() is None:
				process.kill()
				process.communicate(timeout=15)


def test_incomplete_legacy_key_is_preserved_and_restored_key_reclaims_old_workspace(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	state = tmp_path / "state"
	monkeypatch.setattr(temp_workspace, "_user_state_directory", lambda: state)
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	old, _, active_lock, _, _ = temp_workspace._create_run_directory(temp_root)
	(old / "owned").write_bytes(b"old")
	temp_workspace._unlock(active_lock)
	key_path = state / "temp-workspace.key"
	original = key_path.read_bytes()
	key_path.write_bytes(b"partial")

	with temporary_workspace(repository) as current:
		assert current.is_dir()
		assert (old / "owned").read_bytes() == b"old"
	assert key_path.read_bytes() == b"partial"
	recovery, _, active_lock, _, _ = temp_workspace._create_run_directory(temp_root)
	(recovery / "owned").write_bytes(b"recovery key")
	temp_workspace._unlock(active_lock)
	key_path.write_bytes(original)
	with temporary_workspace(repository):
		assert not old.exists()
		assert not recovery.exists()


def test_interrupted_cleanup_publication_is_retried_on_next_run(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	environment = os.environ.copy()
	environment["LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"] = str(tmp_path / "state")
	script = "\n".join(
		(
			"import os, sys",
			"from pathlib import Path",
			"import quality_gate.temp_workspace as workspace",
			"real = workspace.os.rename"
			if os.name == "nt"
			else "real = workspace._rename_without_replacement",
			"def stop(source, destination, *args, **kwargs):",
			"    if Path(destination).name.startswith('.quality-gate-cleanup-record-'):",
			f"        os._exit({_INTERRUPTED_CLEANUP_EXIT})",
			"    return real(source, destination, *args, **kwargs)",
			"workspace.os.rename = stop"
			if os.name == "nt"
			else "workspace._rename_without_replacement = stop",
			"with workspace.temporary_workspace(Path(sys.argv[1])) as work:",
			"    (work / 'owned').write_bytes(b'owned')",
			"    print(work, flush=True)",
		)
	)
	process = subprocess.run(
		[sys.executable, "-c", script, str(repository)],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		capture_output=True,
		text=True,
		timeout=30,
		check=False,
	)
	assert process.returncode == _INTERRUPTED_CLEANUP_EXIT, process.stderr
	stale = Path(process.stdout.strip())
	assert (stale / "owned").read_bytes() == b"owned"
	script = "\n".join(
		(
			"import sys",
			"from pathlib import Path",
			"from quality_gate.temp_workspace import temporary_workspace",
			"with temporary_workspace(Path(sys.argv[1])) as work:",
			"    (work / 'usable').write_bytes(b'ok')",
		)
	)
	process = subprocess.run(
		[sys.executable, "-c", script, str(repository)],
		cwd=Path(__file__).resolve().parents[1],
		env=environment,
		capture_output=True,
		text=True,
		timeout=30,
		check=False,
	)
	assert process.returncode == 0, process.stderr
	assert not stale.exists()


def test_reclamation_preserves_a_directory_replaced_after_ownership_check(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	stale = temp_root / "qg-stale-owned"
	displaced = temp_root / "qg-displaced-owned"
	stale.mkdir(parents=True)
	(stale / ".active.lock").write_bytes(b"\0")
	(stale / "owned-content").write_text("owned", encoding="utf-8")
	(stale / ".quality-gate-workspace").write_bytes(
		temp_workspace._ownership_marker(temp_root, stale)
	)
	stale_identity = stale.lstat()
	temp_workspace._write_ownership_record(temp_root, stale, stale_identity)

	original_remove = temp_workspace._remove_entry
	replaced = False

	def replace_before_remove(
		path: Path, *, expected_identity: os.stat_result | None = None
	) -> None:
		nonlocal replaced
		if path == stale and expected_identity is not None and not replaced:
			path.replace(displaced)
			path.mkdir()
			(path / "foreign-content").write_text("preserve", encoding="utf-8")
			replaced = True
		original_remove(path, expected_identity=expected_identity)

	monkeypatch.setattr(temp_workspace, "_remove_entry", replace_before_remove)
	with temporary_workspace(repository) as current:
		assert current.is_dir()
		assert (stale / "foreign-content").read_text(encoding="utf-8") == "preserve"
		assert (displaced / "owned-content").read_text(encoding="utf-8") == "owned"

	assert replaced
	assert (stale / "foreign-content").read_text(encoding="utf-8") == "preserve"
	assert (displaced / ".quality-gate-workspace").is_file()


def test_stale_reclamation_keeps_root_controls_when_child_removal_fails(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	stale = temp_root / "qg-stale-owned"
	stale.mkdir(parents=True)
	active_lock = stale / ".active.lock"
	active_lock.write_bytes(b"\0")
	marker = stale / ".quality-gate-workspace"
	marker.write_bytes(temp_workspace._ownership_marker(temp_root, stale))
	blocked = stale / "blocked-content"
	blocked.write_text("owned", encoding="utf-8")
	identity = stale.lstat()
	owner_record = temp_workspace._ownership_record_path(temp_root, stale)
	temp_workspace._write_ownership_record(temp_root, stale, identity)

	original_entries = temp_workspace._directory_entries_for_removal
	original_remove_file = temp_workspace._remove_file

	def put_root_controls_first(
		directory: Path, expected: os.stat_result
	) -> list[tuple[Path, os.stat_result]] | None:
		entries = original_entries(directory, expected)
		if directory != stale or entries is None:
			return entries
		controls = [
			item for item in entries if item[0].name in {".active.lock", ".quality-gate-workspace"}
		]
		return [*controls, *(item for item in entries if item not in controls)]

	def fail_on_blocked_file(path: Path, expected: os.stat_result) -> None:
		if path == blocked:
			raise PermissionError(errno.EACCES, "simulated child cleanup failure", path)
		original_remove_file(path, expected)

	monkeypatch.setattr(temp_workspace, "_directory_entries_for_removal", put_root_controls_first)
	monkeypatch.setattr(temp_workspace, "_remove_file", fail_on_blocked_file)
	temp_workspace._reclaim_stale_workspace(temp_root, stale)

	assert stale.is_dir()
	assert active_lock.is_file()
	assert marker.is_file()
	assert owner_record.is_file()
	assert temp_workspace._cleanup_record_path(temp_root, stale).is_file()
	assert blocked.read_text(encoding="utf-8") == "owned"

	monkeypatch.setattr(temp_workspace, "_directory_entries_for_removal", original_entries)
	monkeypatch.setattr(temp_workspace, "_remove_file", original_remove_file)
	temp_workspace._reclaim_stale_workspace(temp_root, stale)
	assert not stale.exists()
	assert not owner_record.exists()
	assert not temp_workspace._cleanup_record_path(temp_root, stale).exists()


@pytest.mark.parametrize("failure", ["interruption", "second-unlink"])
@pytest.mark.parametrize("removed_control", [".active.lock", ".quality-gate-workspace"])
def test_stale_reclamation_resumes_after_control_file_removal_fails(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	removed_control: str,
	failure: str,
) -> None:
	temp_root = tmp_path / "temp"
	stale = temp_root / "qg-interrupted-control"
	stale.mkdir(parents=True)
	active_lock = stale / ".active.lock"
	active_lock.write_bytes(b"\0")
	marker = stale / ".quality-gate-workspace"
	marker.write_bytes(temp_workspace._ownership_marker(temp_root, stale))
	identity = stale.lstat()
	owner_record = temp_workspace._ownership_record_path(temp_root, stale)
	cleanup_record = temp_workspace._cleanup_record_path(temp_root, stale)
	temp_workspace._write_ownership_record(temp_root, stale, identity)

	original_entries = temp_workspace._directory_entries_for_removal
	original_remove_file = temp_workspace._remove_file

	def put_root_controls_first(
		directory: Path, expected: os.stat_result
	) -> list[tuple[Path, os.stat_result]] | None:
		entries = original_entries(directory, expected)
		if directory != stale or entries is None:
			return entries
		controls = [
			item for item in entries if item[0].name in {".active.lock", ".quality-gate-workspace"}
		]
		first_control = next(item for item in controls if item[0].name == removed_control)
		other_controls = [item for item in controls if item[0].name != removed_control]
		return [first_control, *other_controls, *(item for item in entries if item not in controls)]

	def fail_after_control(path: Path, expected: os.stat_result) -> None:
		if (
			failure == "second-unlink"
			and path.parent == stale
			and path.name in {".active.lock", ".quality-gate-workspace"}
			and path.name != removed_control
		):
			raise OSError(errno.EIO, "simulated second control failure", path)
		original_remove_file(path, expected)
		if failure == "interruption" and path.name == removed_control:
			raise OSError(errno.EIO, "simulated interruption after control removal", path)

	monkeypatch.setattr(temp_workspace, "_directory_entries_for_removal", put_root_controls_first)
	monkeypatch.setattr(temp_workspace, "_remove_file", fail_after_control)
	temp_workspace._reclaim_stale_workspace(temp_root, stale)

	assert stale.is_dir()
	assert cleanup_record.is_file()
	assert owner_record.is_file()
	assert not (stale / removed_control).exists()
	assert (
		stale / (".quality-gate-workspace" if removed_control == ".active.lock" else ".active.lock")
	).is_file()

	monkeypatch.setattr(temp_workspace, "_directory_entries_for_removal", original_entries)
	monkeypatch.setattr(temp_workspace, "_remove_file", original_remove_file)
	with temporary_workspace(tmp_path) as workspace:
		assert workspace.is_dir()
	assert not stale.exists()
	assert not owner_record.exists()
	assert not cleanup_record.exists()


def test_stale_reclamation_resumes_after_final_directory_removal_fails(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	stale = temp_root / "qg-interrupted-rmdir"
	stale.mkdir(parents=True)
	active_lock = stale / ".active.lock"
	active_lock.write_bytes(b"\0")
	marker = stale / ".quality-gate-workspace"
	marker.write_bytes(temp_workspace._ownership_marker(temp_root, stale))
	identity = stale.lstat()
	owner_record = temp_workspace._ownership_record_path(temp_root, stale)
	cleanup_record = temp_workspace._cleanup_record_path(temp_root, stale)
	temp_workspace._write_ownership_record(temp_root, stale, identity)

	original_remove_directory = temp_workspace._remove_directory

	def fail_final_rmdir(path: Path, *, expected_identity: os.stat_result) -> None:
		if path == stale:
			raise OSError(errno.EIO, "simulated final rmdir failure", path)
		original_remove_directory(path, expected_identity=expected_identity)

	monkeypatch.setattr(temp_workspace, "_remove_directory", fail_final_rmdir)
	temp_workspace._reclaim_stale_workspace(temp_root, stale)

	assert stale.is_dir()
	assert not active_lock.exists()
	assert not marker.exists()
	assert owner_record.is_file()
	assert cleanup_record.is_file()

	monkeypatch.setattr(temp_workspace, "_remove_directory", original_remove_directory)
	with temporary_workspace(tmp_path) as workspace:
		assert workspace.is_dir()
	assert not stale.exists()
	assert not owner_record.exists()
	assert not cleanup_record.exists()


@pytest.mark.parametrize("valid_record", [True, False])
def test_stale_reclamation_removes_only_a_valid_orphan_owner_record(
	tmp_path: Path,
	valid_record: bool,
) -> None:
	temp_root = tmp_path / "temp"
	run_directory = temp_root / "qg-orphan"
	run_directory.mkdir(parents=True)
	identity = run_directory.lstat()
	owner_record = temp_workspace._ownership_record_path(temp_root, run_directory)
	if valid_record:
		temp_workspace._write_ownership_record(temp_root, run_directory, identity)
	else:
		record = json.loads(temp_workspace._ownership_record(temp_root, run_directory, identity))
		record["root"] = os.path.normcase(os.path.abspath(tmp_path / "foreign-root"))
		owner_record.write_text(
			json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
			encoding="utf-8",
		)
	run_directory.rmdir()

	temp_workspace._reclaim_stale_workspace(temp_root, run_directory)

	assert owner_record.exists() is not valid_record


def test_stale_reclamation_preserves_owner_record_without_valid_signature(
	tmp_path: Path,
) -> None:
	temp_root = tmp_path / "temp"
	run_directory = temp_root / "qg-forged-owner"
	run_directory.mkdir(parents=True)
	(run_directory / ".active.lock").write_bytes(b"\0")
	(run_directory / ".quality-gate-workspace").write_bytes(
		temp_workspace._ownership_marker(temp_root, run_directory)
	)
	(run_directory / "owned-content").write_text("owned", encoding="utf-8")
	identity = run_directory.lstat()
	owner_record = temp_workspace._ownership_record_path(temp_root, run_directory)
	forged = json.loads(temp_workspace._ownership_record(temp_root, run_directory, identity))
	forged["signature"] = "0" * 64
	owner_record.write_text(
		json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n",
		encoding="utf-8",
	)

	temp_workspace._reclaim_stale_workspace(temp_root, run_directory)

	assert run_directory.is_dir()
	assert owner_record.is_file()
	assert (run_directory / "owned-content").read_text(encoding="utf-8") == "owned"


@pytest.mark.parametrize("signature", [None, 1, "0" * 63, "A" * 64, "g" * 64, "é" * 64])
def test_valid_signed_record_rejects_malformed_signature_before_comparison(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signature: object
) -> None:
	monkeypatch.setattr(temp_workspace, "_user_state_directory", lambda: tmp_path / "state")
	assert temp_workspace._ownership_key(create=True) is not None
	record = {"format": "test", "signature": signature}

	assert temp_workspace._valid_signed_record(temp_workspace._canonical_json(record)) is None


@pytest.mark.skipif(os.name == "nt", reason="Windows deletion uses file-handle identity")
def test_cleanup_area_recovery_restores_nonempty_quarantined_directory(tmp_path: Path) -> None:
	temp_root = tmp_path / "temp"
	temp_root.mkdir()
	workspace = temp_root / "qg-interrupted"
	workspace.mkdir()
	content = workspace / "late-content"
	content.write_text("preserve", encoding="utf-8")
	workspace_identity = workspace.lstat()
	root_identity = temp_root.lstat()
	area, cleanup_descriptor = temp_workspace._open_cleanup_area(temp_root)
	root_descriptor = os.open(
		temp_root,
		os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
	)
	token = "interrupted"
	quarantine_name = f"entry-{workspace_identity.st_dev:x}-{workspace_identity.st_ino:x}-{token}"
	try:
		journal_name, _journal_identity = temp_workspace._write_quarantine_journal(
			temp_root,
			workspace,
			workspace_identity,
			root_identity,
			"directory",
			token,
			quarantine_name,
			cleanup_descriptor,
		)
		os.rename(
			workspace.name,
			quarantine_name,
			src_dir_fd=root_descriptor,
			dst_dir_fd=cleanup_descriptor,
		)
		os.fsync(root_descriptor)
		os.fsync(cleanup_descriptor)

		temp_workspace._recover_quarantined_entries(temp_root, cleanup_descriptor)

		assert content.read_text(encoding="utf-8") == "preserve"
		assert not (area / quarantine_name).exists()
		assert not (area / journal_name).exists()
	finally:
		os.close(root_descriptor)
		os.close(cleanup_descriptor)


@pytest.mark.skipif(os.name == "nt", reason="Windows deletion uses file-handle identity")
@pytest.mark.parametrize("entry_kind", ["file", "link", "directory"])
def test_removal_preserves_entry_replaced_after_identity_check(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	entry_kind: str,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-race"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	displaced = tmp_path / f"displaced-{entry_kind}"
	foreign_target = tmp_path / "foreign-target"
	foreign_target.write_text("outside", encoding="utf-8")
	if entry_kind == "file":
		victim.write_text("owned", encoding="utf-8")
	elif entry_kind == "link":
		try:
			victim.symlink_to(foreign_target)
		except OSError as error:
			pytest.skip(f"file symlinks are unavailable: {error}")
	else:
		victim.mkdir()
	identity = victim.lstat()
	original_move = temp_workspace._rename_entry_to_cleanup_area
	replaced = False

	def replace_before_move(
		path: Path,
		quarantine_name: str,
		parent_descriptor: int,
		cleanup_descriptor: int,
	) -> None:
		nonlocal replaced
		if path == victim and not replaced:
			path.replace(displaced)
			if entry_kind == "file":
				path.write_text("foreign", encoding="utf-8")
			elif entry_kind == "link":
				path.symlink_to(foreign_target)
			else:
				path.mkdir()
				(path / "foreign.txt").write_text("foreign", encoding="utf-8")
			replaced = True
		original_move(path, quarantine_name, parent_descriptor, cleanup_descriptor)

	monkeypatch.setattr(temp_workspace, "_rename_entry_to_cleanup_area", replace_before_move)
	with pytest.raises(OSError, match="changed"):
		temp_workspace._remove_entry(victim, expected_identity=identity)

	assert replaced
	if entry_kind == "file":
		assert victim.read_text(encoding="utf-8") == "foreign"
		assert displaced.read_text(encoding="utf-8") == "owned"
	elif entry_kind == "link":
		assert victim.is_symlink()
		assert displaced.is_symlink()
		assert foreign_target.read_text(encoding="utf-8") == "outside"
	else:
		assert (victim / "foreign.txt").read_text(encoding="utf-8") == "foreign"
		assert displaced.is_dir()
		assert not list(displaced.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX retry uses directory permissions")
def test_removal_rechecks_identity_after_permission_retry(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-permission-retry"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	displaced = tmp_path / "displaced-original"
	victim.write_text("owned", encoding="utf-8")
	identity = victim.lstat()
	original_rename = os.rename
	replaced = False

	def replace_before_retry(
		source: str | os.PathLike[str],
		destination: str | os.PathLike[str],
		*,
		src_dir_fd: int | None = None,
		dst_dir_fd: int | None = None,
	) -> None:
		nonlocal replaced
		if source == victim.name and not replaced:
			victim.replace(displaced)
			victim.write_text("foreign", encoding="utf-8")
			replaced = True
			raise PermissionError("simulated first rename failure")
		original_rename(
			source,
			destination,
			src_dir_fd=src_dir_fd,
			dst_dir_fd=dst_dir_fd,
		)

	monkeypatch.setattr(temp_workspace.os, "rename", replace_before_retry)
	with pytest.raises(OSError, match="changed"):
		temp_workspace._remove_entry(victim, expected_identity=identity)

	assert replaced
	assert victim.read_text(encoding="utf-8") == "foreign"
	assert displaced.read_text(encoding="utf-8") == "owned"


@pytest.mark.skipif(os.name != "nt", reason="Windows deletion uses file-handle identity")
def test_windows_removal_deletes_the_opened_identity_after_path_replacement(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from ctypes import wintypes

	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-windows-race"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	displaced = tmp_path / "displaced-original"
	victim.write_text("owned", encoding="utf-8")
	identity = victim.lstat()
	kernel32, advapi32 = temp_workspace._windows_security_api()
	original_create_file = cast(Callable[..., object], kernel32.CreateFileW)
	replaced = False

	def open_then_replace(*arguments: object) -> object:
		nonlocal replaced
		handle = original_create_file(*arguments)
		if handle not in (None, wintypes.HANDLE(-1).value):
			victim.replace(displaced)
			victim.write_text("foreign", encoding="utf-8")
			replaced = True
		return handle

	class KernelProxy:
		def __getattr__(self, name: str) -> object:
			if name == "CreateFileW":
				return open_then_replace
			return getattr(kernel32, name)

	monkeypatch.setattr(
		temp_workspace,
		"_windows_security_api",
		lambda: (KernelProxy(), advapi32),
	)
	temp_workspace._remove_by_windows_handle(victim, identity, kind="file")

	assert replaced
	assert victim.read_text(encoding="utf-8") == "foreign"
	assert not displaced.exists()


@pytest.mark.parametrize("replacement_kind", ["file", "link"])
def test_recursive_removal_preserves_a_nested_directory_replaced_before_reclamation(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	replacement_kind: str,
) -> None:
	workspace = tmp_path / "qg-workspace"
	nested = workspace / "nested"
	displaced = tmp_path / "displaced-nested"
	foreign_target = tmp_path / "foreign-target"
	nested.mkdir(parents=True)
	(nested / "owned-content").write_text("owned", encoding="utf-8")
	foreign_target.write_text("preserve", encoding="utf-8")
	original_lstat = Path.lstat
	nested_lstat_calls = 0
	replaced = False

	def replace_when_queued(path: Path) -> os.stat_result:
		nonlocal nested_lstat_calls, replaced
		if path == nested:
			nested_lstat_calls += 1
			if nested_lstat_calls == _SECOND_NESTED_LSTAT_CALL:
				path.replace(displaced)
				if replacement_kind == "file":
					path.write_text("foreign", encoding="utf-8")
				else:
					try:
						path.symlink_to(foreign_target)
					except OSError as error:
						pytest.skip(f"File symlinks are unavailable: {error}")
				replaced = True
		return original_lstat(path)

	monkeypatch.setattr(Path, "lstat", replace_when_queued)
	with pytest.raises(OSError, match="changed"):
		temp_workspace._remove_entry(workspace)

	assert replaced
	assert (displaced / "owned-content").read_text(encoding="utf-8") == "owned"
	if replacement_kind == "file":
		assert nested.read_text(encoding="utf-8") == "foreign"
	else:
		assert nested.is_symlink()
		assert foreign_target.read_text(encoding="utf-8") == "preserve"


def test_temporary_workspace_preserves_foreign_link_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	target = tmp_path / "foreign-target"
	temp_root.mkdir(parents=True)
	target.mkdir()
	(target / "keep.txt").write_text("outside", encoding="utf-8")
	link = temp_root / "qg-foreign-link"
	try:
		os.symlink(target, link, target_is_directory=True)
	except OSError as error:
		pytest.skip(f"directory symlink creation is unavailable: {error}")

	with temporary_workspace(repository):
		assert link.is_symlink()
		assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"

	assert link.is_symlink()
	assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"


def test_repository_temp_link_is_rejected_without_touching_its_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	repository.mkdir()
	target = tmp_path / "foreign-temp"
	target.mkdir()
	(target / "keep.txt").write_text("outside", encoding="utf-8")
	link = repository / "temp"
	try:
		os.symlink(target, link, target_is_directory=True)
	except OSError as error:
		pytest.skip(f"directory symlink creation is unavailable: {error}")

	with pytest.raises(temp_workspace.TemporaryWorkspaceError):
		with temporary_workspace(repository):
			pytest.fail("repository temp link must be rejected")
	assert link.is_symlink()
	assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"


def test_owned_workspace_removal_does_not_follow_a_link_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	target = tmp_path / "foreign-target"
	target.mkdir()
	(target / "keep.txt").write_text("outside", encoding="utf-8")
	link: Path | None = None
	with temporary_workspace(repository) as workspace:
		link = workspace / "foreign-link"
		try:
			os.symlink(target, link, target_is_directory=True)
		except OSError as error:
			pytest.skip(f"directory symlink creation is unavailable: {error}")

	assert link is not None and not link.exists()
	assert (target / "keep.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary only")
def test_workspace_reclamation_preserves_foreign_junction_and_target(tmp_path: Path) -> None:
	repository = tmp_path / "repository"
	temp_root = repository / "temp"
	temp_root.mkdir(parents=True)
	foreign_target = tmp_path / "foreign-target"
	owned_target = tmp_path / "owned-target"
	foreign_target.mkdir()
	owned_target.mkdir()
	(foreign_target / "keep.txt").write_text("foreign", encoding="utf-8")
	(owned_target / "keep.txt").write_text("owned", encoding="utf-8")
	foreign_junction = temp_root / "qg-foreign-junction"
	created = subprocess.run(
		["cmd.exe", "/c", "mklink", "/J", str(foreign_junction), str(foreign_target)],
		capture_output=True,
		check=False,
		timeout=10,
	)
	if created.returncode != 0:
		pytest.skip("junction creation is unavailable")
	owned_junction: Path | None = None
	with temporary_workspace(repository) as workspace:
		owned_junction = workspace / "junction"
		created = subprocess.run(
			["cmd.exe", "/c", "mklink", "/J", str(owned_junction), str(owned_target)],
			capture_output=True,
			check=False,
			timeout=10,
		)
		if created.returncode != 0:
			pytest.skip("junction creation is unavailable")
		metadata = owned_junction.lstat()
		assert getattr(metadata, "st_file_attributes", 0) & getattr(
			stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
		)

	assert owned_junction is not None and not owned_junction.exists()
	assert foreign_junction.exists()
	assert getattr(foreign_junction.lstat(), "st_file_attributes", 0) & getattr(
		stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
	)
	assert (foreign_target / "keep.txt").read_text(encoding="utf-8") == "foreign"
	assert (owned_target / "keep.txt").read_text(encoding="utf-8") == "owned"


@pytest.mark.parametrize(
	("mode", "phase", "arguments"),
	[
		("ruff-write", "write", {}),
		("ruff-validate", "validate", {}),
		("secrets", None, {"secrets": True}),
		("integrity", None, {"integrity": True}),
		("types", None, {"types": True}),
	],
)
def test_public_edit_modes_preserve_foreign_temp_content(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capfd: pytest.CaptureFixture[str],
	mode: str,
	phase: str | None,
	arguments: dict[str, bool],
) -> None:
	project = tmp_path / "project"
	project.mkdir()
	temp_root = project / "temp"
	temp_root.mkdir()
	foreign_file = temp_root / "qg-user-data.bin"
	foreign_file.write_bytes(b"preserve")
	foreign_directory = temp_root / "qg-candidate"
	foreign_directory.mkdir()
	(foreign_directory / "input.py").write_text("preserve", encoding="utf-8")
	legacy_directory = temp_root / ".quality-gate-edit-old"
	legacy_directory.mkdir()
	(legacy_directory / "input.py").write_text("preserve", encoding="utf-8")
	request: dict[str, object] = {
		"schema": 1,
		"root": str(project.resolve()),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
	}
	if phase is not None:
		request["phase"] = phase
	if mode == "types":
		request["baseline"] = {"policy_release": "v4.4.0"}
	stdin = io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()), encoding="utf-8")
	monkeypatch.setattr(edit_cli.sys, "stdin", stdin)
	workspaces: list[Path] = []

	def run_worker(
		*_arguments: object, root: Path, environment: dict[str, str], **_options: object
	) -> tuple[int, bytes, bytes]:
		workspaces.append(root)
		assert root.parent == temp_root
		assert environment["TEMP"] == str(root)
		result = edit_cli._failure(
			"deadline",
			secrets=bool(arguments.get("secrets")),
			types=bool(arguments.get("types")),
			integrity=bool(arguments.get("integrity")),
		)
		return EXIT_UNCHECKED, json.dumps(result).encode(), b""

	monkeypatch.setattr(edit_cli, "run_process", run_worker)
	assert edit_cli.main(**arguments) == EXIT_UNCHECKED
	assert json.loads(capfd.readouterr().out)["status"] == "unchecked"
	assert len(workspaces) == 1 and not workspaces[0].exists()
	assert foreign_file.read_bytes() == b"preserve"
	assert (foreign_directory / "input.py").read_text(encoding="utf-8") == "preserve"
	assert (legacy_directory / "input.py").read_text(encoding="utf-8") == "preserve"


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

	def reject_read_only_root(
		root: Path,
	) -> tuple[Path, Path, BinaryIO, os.stat_result, os.stat_result]:
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
