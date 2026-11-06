from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_temp_workspace import (
	_INTERRUPTED_CLEANUP_EXIT,
)

import quality_gate.temp_workspace as temp_workspace
import quality_gate.workspace_keys as workspace_keys
import quality_gate.workspace_quarantine as workspace_quarantine
import quality_gate.workspace_reclaim as workspace_reclaim
import quality_gate.workspace_records as workspace_records
import quality_gate.workspace_tree as workspace_tree
from quality_gate.temp_workspace import temporary_workspace


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
			"import quality_gate.workspace_keys as workspace_keys",
			"real = workspace.os.rename"
			if os.name == "nt"
			else "real = workspace_keys._rename_without_replacement",
			"def stop(source, destination, *args, **kwargs):",
			"    if Path(destination).name.startswith('.quality-gate-cleanup-record-'):",
			f"        os._exit({_INTERRUPTED_CLEANUP_EXIT})",
			"    return real(source, destination, *args, **kwargs)",
			"workspace.os.rename = stop"
			if os.name == "nt"
			else "workspace_keys._rename_without_replacement = stop",
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

	monkeypatch.setattr(workspace_reclaim, "_remove_entry", replace_before_remove)
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

	original_entries = workspace_tree._directory_entries_for_removal
	original_remove_file = workspace_tree._remove_file

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

	monkeypatch.setattr(workspace_tree, "_directory_entries_for_removal", put_root_controls_first)
	monkeypatch.setattr(workspace_tree, "_remove_file", fail_on_blocked_file)
	temp_workspace._reclaim_stale_workspace(temp_root, stale)

	assert stale.is_dir()
	assert active_lock.is_file()
	assert marker.is_file()
	assert owner_record.is_file()
	assert temp_workspace._cleanup_record_path(temp_root, stale).is_file()
	assert blocked.read_text(encoding="utf-8") == "owned"

	monkeypatch.setattr(workspace_tree, "_directory_entries_for_removal", original_entries)
	monkeypatch.setattr(workspace_tree, "_remove_file", original_remove_file)
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

	original_entries = workspace_tree._directory_entries_for_removal
	original_remove_file = workspace_tree._remove_file

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

	monkeypatch.setattr(workspace_tree, "_directory_entries_for_removal", put_root_controls_first)
	monkeypatch.setattr(workspace_tree, "_remove_file", fail_after_control)
	temp_workspace._reclaim_stale_workspace(temp_root, stale)

	assert stale.is_dir()
	assert cleanup_record.is_file()
	assert owner_record.is_file()
	assert not (stale / removed_control).exists()
	assert (
		stale / (".quality-gate-workspace" if removed_control == ".active.lock" else ".active.lock")
	).is_file()

	monkeypatch.setattr(workspace_tree, "_directory_entries_for_removal", original_entries)
	monkeypatch.setattr(workspace_tree, "_remove_file", original_remove_file)
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

	original_remove_directory = workspace_tree._remove_directory

	def fail_final_rmdir(path: Path, *, expected_identity: os.stat_result) -> None:
		if path == stale:
			raise OSError(errno.EIO, "simulated final rmdir failure", path)
		original_remove_directory(path, expected_identity=expected_identity)

	monkeypatch.setattr(workspace_tree, "_remove_directory", fail_final_rmdir)
	temp_workspace._reclaim_stale_workspace(temp_root, stale)

	assert stale.is_dir()
	assert not active_lock.exists()
	assert not marker.exists()
	assert owner_record.is_file()
	assert cleanup_record.is_file()

	monkeypatch.setattr(workspace_tree, "_remove_directory", original_remove_directory)
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
		record = json.loads(workspace_records._ownership_record(temp_root, run_directory, identity))
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
	forged = json.loads(workspace_records._ownership_record(temp_root, run_directory, identity))
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
	monkeypatch.setattr(workspace_keys, "_user_state_directory", lambda: tmp_path / "state")
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
		journal_name, _journal_identity = workspace_quarantine._write_quarantine_journal(
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
