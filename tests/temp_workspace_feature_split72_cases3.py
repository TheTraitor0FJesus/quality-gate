"""Feature test partition."""

from __future__ import annotations

from temp_workspace_feature_split72_support import (
	_INTERRUPTED_AFTER_UNLINK_EXIT as _INTERRUPTED_AFTER_UNLINK_EXIT,
)
from temp_workspace_feature_split72_support import (
	_INTERRUPTED_CLEANUP_EXIT as _INTERRUPTED_CLEANUP_EXIT,
)
from temp_workspace_feature_split72_support import (
	_INTERRUPTED_KEY_EXIT as _INTERRUPTED_KEY_EXIT,
)
from temp_workspace_feature_split72_support import (
	_SECOND_NESTED_LSTAT_CALL as _SECOND_NESTED_LSTAT_CALL,
)
from temp_workspace_feature_split72_support import (
	_SINGLE_LINK_COUNT as _SINGLE_LINK_COUNT,
)
from temp_workspace_feature_split72_support import (
	_TRANSITIONAL_HARD_LINK_COUNT as _TRANSITIONAL_HARD_LINK_COUNT,
)
from temp_workspace_feature_split72_support import (
	EXIT_UNCHECKED as EXIT_UNCHECKED,
)
from temp_workspace_feature_split72_support import (
	SYSTEM_TEMP_DIRECTORY as SYSTEM_TEMP_DIRECTORY,
)
from temp_workspace_feature_split72_support import (
	BinaryIO as BinaryIO,
)
from temp_workspace_feature_split72_support import (
	Callable as Callable,
)
from temp_workspace_feature_split72_support import (
	EditError as EditError,
)
from temp_workspace_feature_split72_support import (
	GateOperation as GateOperation,
)
from temp_workspace_feature_split72_support import (
	Path as Path,
)
from temp_workspace_feature_split72_support import (
	SimpleNamespace as SimpleNamespace,
)
from temp_workspace_feature_split72_support import (
	TemporaryWorkspaceError as TemporaryWorkspaceError,
)
from temp_workspace_feature_split72_support import (
	_apply_publisher_race_final_action as _apply_publisher_race_final_action,
)
from temp_workspace_feature_split72_support import (
	_assert_publisher_race_outcome as _assert_publisher_race_outcome,
)
from temp_workspace_feature_split72_support import (
	_assert_workspace_script_cleaned as _assert_workspace_script_cleaned,
)
from temp_workspace_feature_split72_support import (
	_collect_workspace_process as _collect_workspace_process,
)
from temp_workspace_feature_split72_support import (
	_publisher_race_script as _publisher_race_script,
)
from temp_workspace_feature_split72_support import (
	_recover_while_publisher_paused as _recover_while_publisher_paused,
)
from temp_workspace_feature_split72_support import (
	_recovery_reader_script as _recovery_reader_script,
)
from temp_workspace_feature_split72_support import (
	_run_workspace_script as _run_workspace_script,
)
from temp_workspace_feature_split72_support import (
	_start_workspace_script as _start_workspace_script,
)
from temp_workspace_feature_split72_support import (
	_wait_for_workspace_marker as _wait_for_workspace_marker,
)
from temp_workspace_feature_split72_support import (
	_workspace_parents as _workspace_parents,
)
from temp_workspace_feature_split72_support import (
	cast as cast,
)
from temp_workspace_feature_split72_support import (
	cleanup_handed_off_workspace as cleanup_handed_off_workspace,
)
from temp_workspace_feature_split72_support import (
	distribution as distribution,
)
from temp_workspace_feature_split72_support import (
	edit_cli as edit_cli,
)
from temp_workspace_feature_split72_support import (
	errno as errno,
)
from temp_workspace_feature_split72_support import (
	io as io,
)
from temp_workspace_feature_split72_support import (
	json as json,
)
from temp_workspace_feature_split72_support import (
	os as os,
)
from temp_workspace_feature_split72_support import (
	pytest as pytest,
)
from temp_workspace_feature_split72_support import (
	stat as stat,
)
from temp_workspace_feature_split72_support import (
	subprocess as subprocess,
)
from temp_workspace_feature_split72_support import (
	sys as sys,
)
from temp_workspace_feature_split72_support import (
	temp_workspace as temp_workspace,
)
from temp_workspace_feature_split72_support import (
	tempfile as tempfile,
)
from temp_workspace_feature_split72_support import (
	temporary_workspace as temporary_workspace,
)
from temp_workspace_feature_split72_support import (
	threading as threading,
)
from temp_workspace_feature_split72_support import (
	time as time,
)


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
