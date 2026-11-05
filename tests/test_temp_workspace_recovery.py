from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_temp_workspace import (
	_INTERRUPTED_KEY_EXIT,
	_SINGLE_LINK_COUNT,
	_TRANSITIONAL_HARD_LINK_COUNT,
	_assert_workspace_script_cleaned,
	_run_workspace_script,
)

import quality_gate.temp_workspace as temp_workspace
import quality_gate.workspace_keys as workspace_keys
from quality_gate.temp_workspace import temporary_workspace


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
	monkeypatch.setattr(workspace_keys, "_user_state_directory", lambda: tmp_path / "state")
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
	monkeypatch.setattr(workspace_keys, "_user_state_directory", lambda: tmp_path / "state")
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
	monkeypatch.setattr(workspace_keys, "_user_state_directory", lambda: tmp_path / "state")
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
	monkeypatch.setattr(workspace_keys, "_user_state_directory", lambda: tmp_path / "state")
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

	monkeypatch.setattr(workspace_keys, "_rename_without_replacement", unavailable_rename)
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

	monkeypatch.setattr(workspace_keys, "_rename_without_replacement", unsupported_rename)
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
			"import quality_gate.workspace_keys as workspace_keys",
			f"def stop(*args, **kwargs): os._exit({_INTERRUPTED_KEY_EXIT})",
			"workspace.os.rename = stop"
			if os.name == "nt"
			else "workspace_keys._rename_without_replacement = stop",
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
			"import quality_gate.workspace_keys as workspace_keys",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace_keys._rename_without_replacement = unsupported",
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
