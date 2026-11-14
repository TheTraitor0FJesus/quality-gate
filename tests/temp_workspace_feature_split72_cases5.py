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


def test_removal_rescans_tree_after_repeated_nonempty_errors(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-retry"
	victim = workspace / "victim"
	victim.mkdir(parents=True)
	identity = victim.lstat()
	original_remove = temp_workspace._remove_collected_directory
	transient_errors = 8
	attempts = 0

	def fail_first_removals(
		path: Path, expected: os.stat_result, *, deadline: float | None = None
	) -> None:
		nonlocal attempts
		if path == victim and attempts < transient_errors:
			attempts += 1
			raise OSError(errno.ENOTEMPTY, "directory not empty", os.fspath(path))
		original_remove(path, expected, deadline=deadline)

	monkeypatch.setattr(temp_workspace, "_remove_collected_directory", fail_first_removals)

	temp_workspace._remove_entry(victim, expected_identity=identity)

	assert attempts == transient_errors
	assert not victim.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine uses directory descriptors")
def test_removal_rescans_directory_when_entries_keep_appearing_after_quarantine(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	temp_root = tmp_path / "temp"
	workspace = temp_root / "qg-rescan"
	workspace.mkdir(parents=True)
	victim = workspace / "victim"
	victim.mkdir()
	identity = victim.lstat()
	original_move = temp_workspace._rename_entry_to_cleanup_area
	late_entry_count = 8
	injected_entries = 0

	def add_entry_after_quarantine(
		path: Path,
		quarantine_name: str,
		parent_descriptor: int,
		cleanup_descriptor: int,
	) -> None:
		nonlocal injected_entries
		original_move(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		if path != victim or injected_entries == late_entry_count:
			return
		late_entry_name = f"late-entry-{injected_entries}"
		injected_entries += 1
		directory_descriptor = os.open(
			quarantine_name,
			os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
			dir_fd=cleanup_descriptor,
		)
		try:
			file_descriptor = os.open(
				late_entry_name,
				os.O_WRONLY | os.O_CREAT | os.O_EXCL,
				0o600,
				dir_fd=directory_descriptor,
			)
			with os.fdopen(file_descriptor, "wb") as late_entry:
				late_entry.write(b"created during quarantine")
		finally:
			os.close(directory_descriptor)

	monkeypatch.setattr(temp_workspace, "_rename_entry_to_cleanup_area", add_entry_after_quarantine)

	temp_workspace._remove_entry(victim, expected_identity=identity)

	assert injected_entries == late_entry_count
	assert not victim.exists()


def test_temporary_workspace_cleanup_retries_each_directory_independently(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(temp_workspace, "_user_state_directory", lambda: tmp_path / "state")
	original_remove = temp_workspace._remove_collected_directory
	victims: set[Path] = set()
	interrupted: set[Path] = set()

	def interrupt_each_directory_once(
		path: Path, expected: os.stat_result, *, deadline: float | None = None
	) -> None:
		if path in victims and path not in interrupted:
			interrupted.add(path)
			raise OSError(errno.ENOTEMPTY, "directory not empty", os.fspath(path))
		original_remove(path, expected, deadline=deadline)

	monkeypatch.setattr(
		temp_workspace, "_remove_collected_directory", interrupt_each_directory_once
	)

	with temporary_workspace(tmp_path / "repository") as workspace:
		for index in range(temp_workspace._DIRECTORY_REMOVAL_MAX_RESCANS + 8):
			victim = workspace / f"victim-{index}"
			victim.mkdir()
			victims.add(victim)

	assert interrupted == victims
	assert not workspace.exists()


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
