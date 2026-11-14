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
		path: Path,
		*,
		expected_identity: os.stat_result | None = None,
		deadline: float | None = None,
	) -> None:
		nonlocal replaced
		if path == stale and expected_identity is not None and not replaced:
			path.replace(displaced)
			path.mkdir()
			(path / "foreign-content").write_text("preserve", encoding="utf-8")
			replaced = True
		original_remove(path, expected_identity=expected_identity, deadline=deadline)

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
		directory: Path,
		expected: os.stat_result,
		*,
		deadline: float | None = None,
	) -> list[tuple[Path, os.stat_result]] | None:
		entries = original_entries(directory, expected, deadline=deadline)
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
		directory: Path,
		expected: os.stat_result,
		*,
		deadline: float | None = None,
	) -> list[tuple[Path, os.stat_result]] | None:
		entries = original_entries(directory, expected, deadline=deadline)
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
