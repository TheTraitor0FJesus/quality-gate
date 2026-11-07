from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_temp_workspace import (
	_INTERRUPTED_AFTER_UNLINK_EXIT,
	_SINGLE_LINK_COUNT,
	_assert_publisher_race_outcome,
	_assert_workspace_script_cleaned,
	_collect_workspace_process,
	_publisher_race_script,
	_recover_while_publisher_paused,
	_run_workspace_script,
	_start_workspace_script,
	_wait_for_workspace_marker,
)

import quality_gate.temp_workspace as temp_workspace
import quality_gate.workspace_keys as workspace_keys
from quality_gate.temp_workspace import temporary_workspace


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
			"import quality_gate.workspace_keys as workspace_keys",
			"state = Path(sys.argv[1])",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace_keys._rename_without_replacement = unsupported",
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
			"import quality_gate.workspace_keys as workspace_keys",
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
			"import quality_gate.workspace_keys as workspace_keys",
			"root, state, entered, release = map(Path, sys.argv[1:])",
			"def unsupported(*args, **kwargs):",
			"    raise OSError(errno.ENOSYS, 'no no-replace rename')",
			"workspace_keys._rename_without_replacement = unsupported",
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
	monkeypatch.setattr(workspace_keys, "_user_state_directory", lambda: state)
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
